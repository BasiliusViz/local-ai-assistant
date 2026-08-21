# Развёртывание на Windows. Точный аналог deploy.sh: поднимает стек,
# проверяет доступность модели, создаёт коллекции, индексирует данные.
#
#     .\deploy.ps1
#
# Повторный запуск безопасен: индексация обновляет точки, а не дублирует.

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

foreach ($key in @("QDRANT_DIR", "CODE_DIR", "DOCS_DIR")) {
    if ($cfg[$key]) {
        New-Item -ItemType Directory -Path $cfg[$key] -Force | Out-Null
    }
}
Ok "каталоги данных готовы"

# Модель всегда внешняя: этот стек её не разворачивает и не настраивает.
# Видеокарта здесь не нужна — qdrant, kb и code-graph считают на процессоре
$ollamaUrl = if ($cfg.OLLAMA_URL) { $cfg.OLLAMA_URL } else { "" }
if (-not $ollamaUrl) { Fail "не задан OLLAMA_URL — адрес сервера с моделью" }
$ollamaBase = $ollamaUrl -replace "/v1/?$", ""
# host.docker.internal понимают только контейнеры; скрипт работает на хосте,
# для него это localhost. Актуально, когда модель крутится на этой же машине
$ollamaProbe = $ollamaBase -replace "host\.docker\.internal", "localhost"
Ok "модель: $ollamaBase"

# ---------- 2. контейнеры ----------
Step "2/6. Запуск контейнеров"

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail "не удалось поднять стек. Смотрите: docker compose logs" }
Ok "контейнеры запущены"

Write-Host "  ожидание готовности сервисов..."
$ready = $false
foreach ($i in 1..60) {
    $q = curl.exe -s -o NUL -w "%{http_code}" "http://localhost:$qdrantPort/readyz" 2>$null
    if ($q -eq "200") { $ready = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $ready) { Fail "Qdrant не поднялся за 3 минуты. Смотрите: docker compose logs qdrant" }
Ok "Qdrant отвечает"

# ---------- 3. модели ----------
Step "3/6. Модели"

# Заголовки авторизации: внешняя Ollama обычно за шлюзом с токеном
$headers = @{}
if ($cfg.OLLAMA_API_KEY) {
    $hdr = if ($cfg.OLLAMA_AUTH_HEADER) { $cfg.OLLAMA_AUTH_HEADER } else { "Authorization" }
    $pfx = if ($null -ne $cfg.OLLAMA_AUTH_PREFIX) { $cfg.OLLAMA_AUTH_PREFIX } else { "Bearer " }
    $headers[$hdr] = "$pfx$($cfg.OLLAMA_API_KEY)"
}

try {
    $tags = Invoke-RestMethod -Uri "$ollamaProbe/api/tags" -Headers $headers -TimeoutSec 60
    $installed = $tags.models.name -join " "
} catch {
    Fail "Ollama недоступна по адресу $ollamaBase : $($_.Exception.Message)"
}

foreach ($model in @($genModel, $embedModel)) {
    $short = ($model -split ":")[0]
    if ($installed -match [regex]::Escape($short)) {
        Ok "$model на месте"
    } else {
        # Скачать модель на чужой сервер мы не можем — только сообщить
        Fail "на сервере $ollamaBase нет модели $model. Она должна быть там развёрнута"
    }
}

# Размерность вектора обязана совпасть с коллекцией, иначе поиск молча врёт
try {
    $body = [Text.Encoding]::UTF8.GetBytes("{`"model`":`"$embedModel`",`"input`":[`"проверка`"]}")
    $resp = Invoke-RestMethod -Method Post -Uri "$ollamaProbe/v1/embeddings" `
        -ContentType "application/json" -Headers $headers -Body $body -TimeoutSec 600
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

# Пустая CODE_REPOS не означает "кода нет": репозитории можно просто положить
# в CODE_DIR, sync.sh эту раскладку умеет. Пропускаем шаг, только когда брать
# действительно нечего, иначе стек разворачивается молча без поиска по коду
$codeEmpty = -not (Get-ChildItem -Force -ErrorAction SilentlyContinue $cfg.CODE_DIR)
if ((-not $cfg.CODE_REPOS) -and $codeEmpty) {
    Warn "CODE_REPOS пуста и $($cfg.CODE_DIR) пуст — поиск по коду и граф отключены"
    Warn "положите код в $($cfg.CODE_DIR) либо укажите CODE_REPOS в .env, затем .\update-code.ps1"
} else {
    docker compose exec -T code-graph /app/sync.sh
    if ($LASTEXITCODE -ne 0) { Fail "синхронизация репозиториев не удалась" }
    # /data, а не /data/repos: каталог repos/ появляется только при клонировании
    # по CODE_REPOS. code_index сам спускается в repos/, если она есть
    docker compose exec -T kb python -m kb.code_index /data
    if ($LASTEXITCODE -ne 0) { Fail "индексация кода не удалась" }
    Ok "граф построен, код проиндексирован"
}

# ---------- 6. проверка ----------
Step "6/6. Проверка"

& "$PSScriptRoot\healthcheck.ps1"
if ($LASTEXITCODE -ne 0) { Fail "проверка не пройдена, см. вывод выше" }

Write-Host ""
Write-Host "Развёртывание завершено." -ForegroundColor Green
Write-Host ""
Write-Host "Подключение в VS Code (Continue):"
Write-Host "  скопируйте continue-config.example.yaml в ~/.continue/config.yaml"
Write-Host "  и замените в нём адреса и токен:"
Write-Host ""
Write-Host "    АДРЕС-СЕРВЕРА  -> эта машина, порты $kbPort и $graphPort"
Write-Host "    АДРЕС-OLLAMA   -> $ollamaBase"
Write-Host "    ТОКЕН          -> ключ шлюза, в поле requestOptions.headers"
Write-Host ""
Write-Host "Токен задаётся ТОЛЬКО через requestOptions.headers: apiKey уходит как"
Write-Host '"Authorization: Bearer", а шлюз с x-api-key такой запрос отобьёт.'
Write-Host "Правила ответов уже внутри примера конфига."
Write-Host "Инструменты работают только в режиме Agent."
