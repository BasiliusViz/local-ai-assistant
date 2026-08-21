#!/usr/bin/env bash
# Обновление всего, что касается кода: репозитории, граф, векторный индекс.
#
#     ./update-code.sh
#
# По расписанию (раз в час), crontab -e:
#     0 * * * * cd /srv/local-ai && ./update-code.sh >> /var/log/local-ai-sync.log 2>&1
#
# Обе части намеренно в одной команде: если обновлять только граф, поиск
# начнёт отдавать устаревшие строки файлов, и заметят это не сразу.

set -u
cd "$(dirname "$0")"

echo "=== 1/2. Репозитории и граф ==="
if ! docker compose exec -T code-graph /app/sync.sh; then
    echo "Синхронизация репозиториев не удалась." >&2
    exit 1
fi

echo
echo "=== 2/2. Векторный индекс кода ==="
# /data, а не /data/repos: когда CODE_REPOS пустая и проекты лежат прямо в
# CODE_DIR, каталог repos/ не создаётся вовсе, и жёсткий путь давал
# "Не каталог: /data/repos". code_index сам спускается в repos/, если она есть
if ! docker compose exec -T kb python -m kb.code_index /data; then
    echo "Индексация кода не удалась." >&2
    exit 1
fi

echo
echo "Готово: граф и поиск по коду обновлены."
