# Проверка развёрнутой системы на Windows. Аналог healthcheck.sh.
#
#     .\healthcheck.ps1
#
# Код возврата 0 — всё в порядке, 1 — есть неисправности.
# Проверяет не «порт открыт», а что поиск реально что-то находит: сервис
# может отвечать двухсоткой и при этом возвращать пустоту из-за пустой базы.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$problems = 0
function Ok($text)   { Write-Host "  [ok]    $text" -ForegroundColor Green }
function Bad($text)  { Write-Host "  [нет]   $text" -ForegroundColor Red; $script:problems++ }
function Warn($text) { Write-Host "  [!]     $text" -ForegroundColor Yellow }

$cfg = @{}
if (Test-Path ".env") {
    Get-Content ".env" | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
        $name, $value = $_ -split "=", 2
        $cfg[$name.Trim()] = $value.Trim()
    }
}
$ollamaPort = if ($cfg.OLLAMA_PORT) { $cfg.OLLAMA_PORT } else { "11434" }
$qdrantPort = if ($cfg.QDRANT_PORT) { $cfg.QDRANT_PORT } else { "6333" }
$graphPort  = if ($cfg.CODE_GRAPH_PORT) { $cfg.CODE_GRAPH_PORT } else { "8011" }
$genModel   = if ($cfg.GEN_MODEL) { $cfg.GEN_MODEL } else { "qwen3:8b" }
$embedModel = if ($cfg.EMBED_MODEL) { $cfg.EMBED_MODEL } else { "bge-m3" }

Write-Host "=== Контейнеры ==="
foreach ($name in @("ollama", "qdrant", "kb", "code-graph")) {
    $state = docker inspect -f "{{.State.Status}}" $name 2>$null
    if ($LASTEXITCODE -ne 0) { Bad "$name не создан" }
    elseif ($state -eq "running") { Ok "$name работает" }
    else { Bad "$name в состоянии $state (docker logs $name)" }
}

Write-Host ""
Write-Host "=== Сервисы ==="
$code = curl.exe -s -o NUL -w "%{http_code}" "http://localhost:$ollamaPort/api/tags" 2>$null
if ($code -eq "200") { Ok "Ollama :$ollamaPort" } else { Bad "Ollama :$ollamaPort отвечает $code" }

$code = curl.exe -s -o NUL -w "%{http_code}" "http://localhost:$qdrantPort/readyz" 2>$null
if ($code -eq "200") { Ok "Qdrant :$qdrantPort" } else { Bad "Qdrant :$qdrantPort отвечает $code" }

Write-Host ""
Write-Host "=== Модели ==="
$installed = docker exec ollama ollama list 2>$null
foreach ($model in @($genModel, $embedModel)) {
    $short = ($model -split ":")[0]
    if ($installed -match [regex]::Escape($short)) { Ok $model }
    else { Bad "$model не загружена (docker exec ollama ollama pull $model)" }
}

# Обе модели должны помещаться в память ОДНОВРЕМЕННО, иначе каждый запрос
# оплачивает перезагрузку с диска — самая частая причина «всё тормозит»
$loadedRaw = docker exec ollama ollama ps 2>$null
$loaded = ($loadedRaw | Select-Object -Skip 1 | Where-Object { $_.Trim() }).Count
if ($loaded -ge 2) { Ok "в памяти моделей: $loaded" }
else { Warn "в памяти моделей: $loaded. После простоя это нормально; если так во время работы — проверьте OLLAMA_MAX_LOADED_MODELS и объём видеопамяти" }

Write-Host ""
Write-Host "=== Данные ==="
foreach ($coll in @("knowledge", "code")) {
    try {
        $info = Invoke-RestMethod -Uri "http://localhost:$qdrantPort/collections/$coll" -TimeoutSec 30
        $count = $info.result.points_count
        if ($count -gt 0) { Ok "коллекция ${coll}: $count чанков" }
        else { Warn "коллекция $coll пуста — данные не проиндексированы" }
    } catch {
        Warn "коллекции $coll нет — она создастся при первой индексации"
    }
}

# Через Invoke-WebRequest, а не curl.exe: PowerShell съедает кавычки при
# передаче JSON нативной программе, и запрос уходит битым
try {
    $r = Invoke-WebRequest -Method Post -Uri "http://localhost:$graphPort/mcp" `
        -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' `
        -ContentType "application/json" `
        -Headers @{ "Accept" = "application/json, text/event-stream" } `
        -TimeoutSec 30 -UseBasicParsing
    if ($r.StatusCode -eq 200) { Ok "граф кода :$graphPort отвечает" }
    else { Warn "граф кода :$graphPort отвечает $($r.StatusCode)" }
} catch {
    Warn "граф кода :$graphPort не отвечает (нормально, если CODE_REPOS пуста)"
}

Write-Host ""
Write-Host "=== Живой поиск ==="
$search = docker exec kb python -c @"
import json
from kb.retriever import search
try:
    hits = search('как это работает', top_k=3)
    print(json.dumps({'ok': True, 'found': len(hits.hits)}))
except Exception as e:
    print(json.dumps({'ok': False, 'error': str(e)[:120]}))
"@ 2>$null | Select-Object -Last 1

if ($search -match '"ok": true') {
    $found = [regex]::Match($search, '"found": (\d+)').Groups[1].Value
    if ([int]$found -gt 0) { Ok "поиск по документам работает (найдено: $found)" }
    else { Warn "поиск отработал, но ничего не нашёл — проверьте, что документы проиндексированы" }
} else {
    Bad "поиск по документам сломан: $search"
}

Write-Host ""
if ($problems -eq 0) {
    Write-Host "Всё в порядке." -ForegroundColor Green
    exit 0
}
Write-Host "Неисправностей: $problems" -ForegroundColor Red
exit 1
