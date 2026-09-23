<#
.SYNOPSIS
  Настройка Continue в VS Code для локального ассистента (Windows).

  Что делает:
    1. ставит расширение Continue (из Marketplace или из .vsix);
    2. отключает телеметрию Continue в настройках VS Code;
    3. собирает ~/.continue/config.yaml из continue-config.example.yaml,
       подставляя адреса, токен и модель (старый конфиг сохраняется рядом);
    4. проверяет связь с моделью и с MCP-серверами.

  Чего НЕ делает (вручную, раздел 3 в guides/USER-GUIDE.md): перезапуск VS Code,
  режим Agent и политика инструментов Automatic — Continue хранит её у себя
  внутри, а не в файле.

.EXAMPLE
  .\setup-continue.ps1 -OllamaUrl http://gpu-01.corp:11434 -ServerUrl http://ai-kb.corp -Vsix D:\soft\continue.vsix

  Токен не передавать в командной строке — он останется в истории. Без
  -Token скрипт спросит его сам, ввод скрыт. Или -VaultPath, если есть
  vault CLI и выполнен vault login.

  Если политика запрещает скрипты:
    powershell -ExecutionPolicy Bypass -File .\setup-continue.ps1 -OllamaUrl ... -ServerUrl ...
#>
param(
    [Parameter(Mandatory = $true)][string]$OllamaUrl,
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [string]$Token,
    [string]$VaultPath,
    [string]$VaultField = "token",
    [switch]$NoToken,
    [string]$Vsix,
    [string]$Model,
    [int]$ContextLength = 0,
    [switch]$NoDojo,
    [switch]$SkipExtension,
    [string]$Template
)

# $PSScriptRoot is empty inside param() defaults in PowerShell 5.1
if (-not $Template) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Template = Join-Path $here "continue-config.example.yaml"
}

# No $ErrorActionPreference = "Stop": native tools write info to stderr and
# PowerShell 5.1 would abort on it. Success is checked explicitly.

$script:Fails = 0
function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]    $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:Fails++ }
function Step($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }

function Write-Utf8NoBom($path, $text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false)))
}

function Normalize-Url($url, $defaultPort) {
    $u = $url.Trim().TrimEnd('/')
    if ($u -notmatch '://') { $u = "http://$u" }
    if ($defaultPort -and $u -notmatch ':\d+$') { $u = "${u}:$defaultPort" }
    return $u
}

$OllamaUrl = Normalize-Url $OllamaUrl 11434
$ServerUrl = Normalize-Url $ServerUrl $null
# ServerUrl is host only: ports 8010/8011/8012 come from the template
$ServerUrl = $ServerUrl -replace ':\d+$', ''

# ---------------------------------------------------------------- token
Step "Токен"
if ($NoToken) {
    Warn "режим без токена: заголовок x-api-key в конфиг не пишется"
} else {
    if (-not $Token -and $VaultPath) {
        if (Get-Command vault -ErrorAction SilentlyContinue) {
            $Token = (& vault kv get "-field=$VaultField" $VaultPath 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $Token) { Ok "взят из Vault: $VaultPath" }
            else { Warn "из Vault прочитать не удалось (vault login выполнен? VAULT_ADDR задан?)"; $Token = $null }
        } else {
            Warn "vault CLI не найден — введите токен вручную"
        }
    }
    if (-not $Token) {
        $sec = Read-Host "Токен из Vault (ввод скрыт)" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $Token = $Token.Trim()
    if (-not $Token) { Bad "токен пустой"; exit 1 }
    # Cyrillic or spaces in a header break the request deep inside the client
    if ($Token -match '[^\x21-\x7E]') { Bad "в токене пробелы или не-латиница — скопирован не целиком или не тот"; exit 1 }
    Ok "токен принят ($($Token.Length) символов)"
}

# ---------------------------------------------------------------- extension
Step "Расширение Continue"
function Find-Code {
    $c = Get-Command code -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @(
        "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
        "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

if ($SkipExtension) {
    Warn "пропущено (-SkipExtension)"
} else {
    $code = Find-Code
    if (-not $code) {
        Bad "VS Code не найден (нет команды code). Установите VS Code или добавьте его в PATH"
    } else {
        $installed = & $code --list-extensions 2>$null | Where-Object { $_ -ieq "continue.continue" }
        if ($installed -and -not $Vsix) {
            Ok "уже установлено"
        } else {
            if ($Vsix) {
                if (-not (Test-Path $Vsix)) { Bad "файл не найден: $Vsix" }
                else { & $code --install-extension (Resolve-Path $Vsix).Path --force | Out-Null }
            } else {
                & $code --install-extension Continue.continue | Out-Null
            }
            $installed = & $code --list-extensions 2>$null | Where-Object { $_ -ieq "continue.continue" }
            if ($installed) { Ok "установлено" }
            elseif (-not $Vsix) { Bad "не установилось. Marketplace закрыт? Укажите файл: -Vsix путь\continue.vsix" }
            else { Bad "не установилось из $Vsix" }
        }
    }
}

# ---------------------------------------------------------------- telemetry
Step "Телеметрия Continue"
$settings = Join-Path $env:APPDATA "Code\User\settings.json"
$line = '"continue.telemetryEnabled": false'
if (-not (Test-Path $settings)) {
    New-Item -ItemType Directory -Force (Split-Path $settings) | Out-Null
    Write-Utf8NoBom $settings "{`n  $line`n}`n"
    Ok "выключена (создан settings.json)"
} else {
    $text = [IO.File]::ReadAllText($settings)
    if ($text -match '"continue\.telemetryEnabled"\s*:\s*false') {
        Ok "уже выключена"
    } else {
        if ($text -match '"continue\.telemetryEnabled"\s*:\s*true') {
            $new = $text -replace '("continue\.telemetryEnabled"\s*:\s*)true', '${1}false'
        } else {
            # settings.json is JSON with comments: insert text, do not re-serialize
            $i = $text.IndexOf('{')
            $rest = if ($i -ge 0) { $text.Substring($i + 1) } else { $null }
            if ($null -eq $rest) { $new = $null }
            else {
                $sep = if ($rest.Trim().StartsWith('}')) { '' } else { ',' }
                $new = $text.Substring(0, $i + 1) + "`n  $line$sep" + $rest
            }
        }
        if ($new) {
            Copy-Item $settings "$settings.bak" -Force
            Write-Utf8NoBom $settings $new
            Ok "выключена (копия прежних настроек: settings.json.bak)"
        } else {
            Bad "не разобрал $settings — выключите вручную: Ctrl+, -> continue telemetry"
        }
    }
}

# ---------------------------------------------------------------- config
Step "Конфиг Continue"
if (-not (Test-Path $Template)) {
    Bad "нет образца $Template (положите continue-config.example.yaml рядом со скриптом или укажите -Template)"
    exit 1
}
$lines = Get-Content -LiteralPath $Template -Encoding UTF8 | Where-Object { $_ -notmatch '^\s*#' }
$cfg = ($lines -join "`n")
$cfg = [regex]::Replace($cfg, "`n{3,}", "`n`n").Trim() + "`n"

$cfg = $cfg.Replace('http://АДРЕС-OLLAMA:11434', $OllamaUrl)
$cfg = $cfg.Replace('http://АДРЕС-СЕРВЕРА', $ServerUrl)

if ($NoToken) {
    $cfg = [regex]::Replace($cfg, '(?m)^\s*(requestOptions|headers):\s*\n', '')
    $cfg = [regex]::Replace($cfg, '(?m)^\s*x-api-key: ТОКЕН\s*\n', '')
} else {
    # single-quoted YAML scalar: only the quote itself needs escaping
    $cfg = $cfg.Replace('x-api-key: ТОКЕН', "x-api-key: '" + $Token.Replace("'", "''") + "'")
}

if ($Model) {
    # first model in the template is the chat model
    $cfg = ([regex]'(?m)^(\s+- name:\s*).+$').Replace($cfg, "`${1}$Model", 1)
    $cfg = ([regex]'(?m)^(\s+model:\s*)\S+').Replace($cfg, "`${1}$Model", 1)
}
if ($ContextLength -gt 0) {
    $cfg = [regex]::Replace($cfg, '(?m)^(\s+contextLength:\s*)\d+', "`${1}$ContextLength")
}
if ($NoDojo) {
    $cfg = [regex]::Replace($cfg, '(?m)^  - name: defectdojo\n(    .*\n)+\n?', '')
}

if ($cfg -match 'АДРЕС-|ТОКЕН') {
    Bad "в конфиге остались незаполненные места (АДРЕС-/ТОКЕН) — образец изменился, сообщите администратору"
    exit 1
}

$cfgDir = Join-Path $env:USERPROFILE ".continue"
$cfgPath = Join-Path $cfgDir "config.yaml"
New-Item -ItemType Directory -Force $cfgDir | Out-Null
if (Test-Path $cfgPath) {
    $bak = "$cfgPath.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Copy-Item $cfgPath $bak
    Ok "прежний конфиг сохранён: $bak"
}
Write-Utf8NoBom $cfgPath $cfg
Ok "записан: $cfgPath"
$chatModel = ([regex]'(?m)^\s+model:\s*(\S+)').Match($cfg).Groups[1].Value
Ok "модель $chatModel на $OllamaUrl, поиск на $ServerUrl"

# ---------------------------------------------------------------- checks
Step "Связь с моделью"
$h = @{}
if (-not $NoToken) { $h["x-api-key"] = $Token }
try {
    $r = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -Headers $h -TimeoutSec 20
    $names = @($r.models | ForEach-Object { $_.name })
    Ok "сервер отвечает, моделей: $($names.Count)"
    if ($names -notcontains $chatModel -and $names -notcontains "${chatModel}:latest") {
        Warn "модели $chatModel нет в списке сервера: $($names -join ', ')"
    }
} catch {
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    if ($status -eq 401 -or $status -eq 403) { Bad "доступ запрещён ($status) — неверный токен" }
    elseif ($status -eq 404) { Warn "шлюз не пропускает /api/tags (404) — не страшно, проверьте вопросом в чате" }
    else { Bad "нет связи с $OllamaUrl — $($_.Exception.Message)" }
}

Step "Связь с MCP-серверами"
# string keys: an int index on [ordered] means position, not key
$servers = [ordered]@{ "8010" = "kb_search"; "8011" = "get_neighbors" }
if (-not $NoDojo) { $servers["8012"] = "dojo_findings" }
$body = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
$mh = @{ Accept = "application/json, text/event-stream" }
foreach ($p in $servers.Keys) {
    try {
        $r = Invoke-RestMethod -Uri "${ServerUrl}:$p/mcp" -Method Post -ContentType "application/json" -Headers $mh -Body $body -TimeoutSec 20
        $tools = @($r.result.tools | ForEach-Object { $_.name })
        if ($tools -contains $servers[$p]) { Ok "${p}: $($tools -join ', ')" }
        else { Bad "${p}: отвечает, но нет $($servers[$p]) — $($tools -join ', ')" }
    } catch {
        $msg = "${p}: не отвечает — $($_.Exception.Message)"
        if ($p -eq 8012) { Warn "$msg (доступ к DefectDojo открыт не всем; не нужен — запустите с -NoDojo)" }
        else { Bad $msg }
    }
}

# ---------------------------------------------------------------- summary
Write-Host ""
if ($script:Fails -eq 0) {
    Write-Host "Готово. Осталось вручную (guides/USER-GUIDE.md, раздел 3):" -ForegroundColor Green
} else {
    Write-Host "Ошибок: $($script:Fails). Исправьте и запустите скрипт ещё раз. Дальше вручную:" -ForegroundColor Red
}
Write-Host "  1. Закрыть VS Code полностью и открыть снова"
Write-Host "  2. Панель Continue -> ассистент Local Assistant -> режим Agent"
Write-Host "  3. Значок инструментов в строке ввода -> knowledge-base, code-graph, defectdojo -> Automatic"
Write-Host "  4. Проверочные вопросы из раздела 4"
exit $script:Fails
