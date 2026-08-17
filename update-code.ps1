# Обновление всего, что касается кода: репозитории, граф, векторный индекс.
#
# Запуск:
#   .\update-code.ps1
#
# По расписанию (раз в час):
#   schtasks /create /tn "LOCAL-AI code sync" /tr "powershell -File C:\Users\darti\Desktop\LOCAL-AI\update-code.ps1" /sc hourly
#
# Две части намеренно идут одной командой: граф и векторный индекс должны
# обновляться вместе. Если обновлять только граф, поиск начнёт отдавать
# устаревшие строки файлов, и это заметят не сразу.

# НЕ "Stop": docker и python пишут информационные сообщения в stderr, а при
# Stop PowerShell считает это фатальной ошибкой и обрывает скрипт на ровном
# месте. Успех проверяем по коду возврата.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

Write-Host "=== 1/2. Репозитории и граф ===" -ForegroundColor Cyan
docker compose exec -T code-graph /app/sync.sh
if ($LASTEXITCODE -ne 0) {
    Write-Host "Синхронизация репозиториев не удалась." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== 2/2. Векторный индекс кода ===" -ForegroundColor Cyan
docker compose exec -T kb python -m kb.code_index /data/repos
if ($LASTEXITCODE -ne 0) {
    Write-Host "Индексация кода не удалась." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Готово. Граф и поиск по коду обновлены." -ForegroundColor Green
