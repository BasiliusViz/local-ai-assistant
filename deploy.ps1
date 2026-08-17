# Развёртывание на Windows. Точный аналог deploy.sh: поднимает стек,
# скачивает модели, создаёт коллекции, индексирует данные и проверяет каждый шаг.
#
#     .\deploy.ps1
#
# Повторный запуск безопасен: модели не качаются заново, индексация обновляет
# точки, а не дублирует.

# НЕ "Stop": docker и python пишут информационные сообщения в stderr, и при
# Stop PowerShell обрывает скрипт на ровном месте. Успех проверяем по $LASTEXITCODE.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Step($text) { Write-Host ""; Write-Host "=== $text ===" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "  [ok]   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]    $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "  [нет]  $text" -ForegroundColor Red; exit 1 }

# ---------- 1. окружение ----------
Step "1/6. Проверка окружения"

$null = docker version --format "{{.Server.Version}}" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker не отвечает. Запустите Docker Desktop и повторите."
}
Ok "Docker работает"

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Warn ".env создан из образца — проверьте пути и модели, затем запустите снова"
    exit 1
}

# Читаем .env: значения понадобятся для проверок портов и путей
$cfg = @{}
Get-Content ".env" | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
    $name, $value = $_ -split "=", 2
    $cfg[$name.Trim()] = $value.Trim()
}
Ok ".env прочитан"

$ollamaPort = if ($cfg.OLLAMA_PORT) { $cfg.OLLAMA_PORT } else { "11434" }
$qdrantPort = if ($cfg.QDRANT_PORT) { $cfg.QDRANT_PORT } else { "6333" }
$kbPort     = if ($cfg.KB_PORT)     { $cfg.KB_PORT }     else { "8010" }
$graphPort  = if ($cfg.CODE_GRAPH_PORT) { $cfg.CODE_GRAPH_PORT } else { "8011" }
$genModel   = if ($cfg.GEN_MODEL)   { $cfg.GEN_MODEL }   else { "qwen3:8b" }
$embedModel = if ($cfg.EMBED_MODEL) { $cfg.EMBED_MODEL } else { "bge-m3" }

foreach ($key in @("MODELS_DIR", "QDRANT_DIR", "CODE_DIR", "DOCS_DIR")) {
    if ($cfg[$key]) {
        New-Item -ItemType Directory -Path $cfg[$key] -Force | Out-Null
    }
}
Ok "каталоги данных готовы"

# GPU не обязателен, но без него ответы идут минутами вместо секунд
$gpu = docker info 2>$null | Select-String -Pattern "nvidia" -Quiet
if ($gpu) { Ok "GPU виден Docker" }
else { Warn "GPU не виден Docker. На процессоре модели работают в десятки раз медленнее" }

# ---------- 2. контейнеры ----------
Step "2/6. Запуск контейнеров"

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail "не удалось поднять стек. Смотрите: docker compose logs" }
Ok "контейнеры запущены"

Write-Host "  ожидание готовности сервисов..."
$ready = $false
foreach ($i in 1..60) {
    $q = curl.exe -s -o NUL -w "%{http_code}" "http://localhost:$qdrantPort/readyz" 2>$null
    $o = curl.exe -s -o NUL -w "%{http_code}" "http://localhost:$ollamaPort/api/tags" 2>$null
    if ($q -eq "200" -and $o -eq "200") { $ready = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $ready) { Fail "Qdrant или Ollama не поднялись за 3 минуты. Смотрите: docker compose logs" }
Ok "Qdrant и Ollama отвечают"

# ---------- 3. модели ----------
Step "3/6. Модели"

$installed = docker exec ollama ollama list 2>$null
foreach ($model in @($genModel, $embedModel)) {
    $short = ($model -split ":")[0]
    if ($installed -match [regex]::Escape($short)) {
        Ok "$model уже загружена"
    } else {
        Write-Host "  скачивание $model (это может занять минуты)..."
        docker exec ollama ollama pull $model
        if ($LASTEXITCODE -ne 0) { Fail "не удалось скачать $model" }
        Ok "$model загружена"
    }
}

# Размерность вектора обязана совпасть с коллекцией, иначе поиск молча врёт
try {
    $body = [Text.Encoding]::UTF8.GetBytes("{`"model`":`"$embedModel`",`"input`":[`"проверка`"]}")
    $resp = Invoke-RestMethod -Method Post -Uri "http://localhost:$ollamaPort/v1/embeddings" `
        -ContentType "application/json" -Body $body -TimeoutSec 600
    Ok "эмбеддер отвечает, размерность вектора: $($resp.data[0].embedding.Count)"
} catch {
    Fail "эмбеддер не вернул вектор: $($_.Exception.Message)"
}

# ---------- 4. документы ----------
Step "4/6. Индексация документов"

$docsDir = $cfg.DOCS_DIR
$docCount = 0
if ($docsDir -and (Test-Path $docsDir)) {
    $docCount = (Get-ChildItem $docsDir -Recurse -File -Include *.md, *.txt -EA SilentlyContinue).Count
}
if ($docCount -eq 0) {
    Warn "в $docsDir нет файлов .md/.txt — поиск по документам будет пустым"
    Warn "положите документы туда и запустите: docker compose exec kb python -m kb.doc_index /docs --source local"
} else {
    # Каждый подкаталог — отдельный источник. Иначе одни и те же документы,
    # разложенные по папкам, попадут в базу под одной меткой, а при повторном
    # запуске с другой меткой продублируются. Плюс по источнику потом можно
    # фильтровать поиск: только вики, только выгрузка Confluence
    $subdirs = Get-ChildItem $docsDir -Directory -EA SilentlyContinue
    if ($subdirs) {
        foreach ($dir in $subdirs) {
            docker compose exec -T kb python -m kb.doc_index "/docs/$($dir.Name)" --source $dir.Name
            if ($LASTEXITCODE -ne 0) { Fail "индексация $($dir.Name) не удалась" }
            Ok "источник $($dir.Name) проиндексирован"
        }
    } else {
        docker compose exec -T kb python -m kb.doc_index /docs --source local
        if ($LASTEXITCODE -ne 0) { Fail "индексация документов не удалась" }
        Ok "документы проиндексированы ($docCount файлов)"
    }
}

# ---------- 5. код ----------
Step "5/6. Код"

if (-not $cfg.CODE_REPOS) {
    Warn "CODE_REPOS пуста — поиск по коду и граф отключены"
    Warn "укажите репозитории в .env и запустите .\update-code.ps1"
} else {
    docker compose exec -T code-graph /app/sync.sh
    if ($LASTEXITCODE -ne 0) { Fail "синхронизация репозиториев не удалась" }
    docker compose exec -T kb python -m kb.code_index /data/repos
    if ($LASTEXITCODE -ne 0) { Fail "индексация кода не удалась" }
    Ok "репозитории склонированы, граф построен, код проиндексирован"
}

# ---------- 6. проверка ----------
Step "6/6. Проверка"

& "$PSScriptRoot\healthcheck.ps1"
if ($LASTEXITCODE -ne 0) { Fail "проверка не пройдена, см. вывод выше" }

Write-Host ""
Write-Host "Развёртывание завершено." -ForegroundColor Green
Write-Host ""
Write-Host "Подключение в VS Code (Continue), ~/.continue/config.yaml:"
Write-Host ""
Write-Host "  mcpServers:"
Write-Host "    - name: knowledge-base"
Write-Host "      type: streamable-http"
Write-Host "      url: http://localhost:$kbPort/mcp"
Write-Host "    - name: code-graph"
Write-Host "      type: streamable-http"
Write-Host "      url: http://localhost:$graphPort/mcp"
Write-Host ""
Write-Host "Правила ответов: скопируйте содержимое continue-rules.yaml в тот же файл."
Write-Host "Инструменты работают только в режиме Agent."
