#!/usr/bin/env bash
# Обновление всего, что касается кода: репозитории, граф, векторный индекс.
#
#     ./update-code.sh               # граф, поиск по коду, индекс готовых описаний
#     ./update-code.sh --summarize   # и перед индексом — дописать описания моделью
#
# --summarize ставит ночной repos/cron.sh: summarize.py зовёт модель только там,
# где код изменился (state.json), но после крупных изменений это часы, а
# модель одна на всех — днём руками без флага.
#
# По расписанию (раз в час), crontab -e:
#     0 * * * * cd /srv/local-ai && ./update-code.sh >> /var/log/local-ai-sync.log 2>&1
#
# Обе части намеренно в одной команде: если обновлять только граф, поиск
# начнёт отдавать устаревшие строки файлов, и заметят это не сразу.

set -u
cd "$(dirname "$0")"

SUMMARIZE=0
for arg in "$@"; do
    case "$arg" in
        --summarize) SUMMARIZE=1 ;;
        *) echo "Неизвестный параметр: $arg (есть только --summarize)" >&2; exit 2 ;;
    esac
done

echo "=== 1/3. Репозитории и граф ==="
if ! docker compose exec -T code-graph /app/sync.sh; then
    echo "Синхронизация репозиториев не удалась." >&2
    exit 1
fi

echo
echo "=== 2/3. Векторный индекс кода ==="
# /data, а не /data/repos: когда CODE_REPOS пустая и проекты лежат прямо в
# CODE_DIR, каталог repos/ не создаётся вовсе, и жёсткий путь давал
# "Не каталог: /data/repos". code_index сам спускается в repos/, если она есть
if ! docker compose exec -T kb python -m kb.code_index /data; then
    echo "Индексация кода не удалась." >&2
    exit 1
fi

echo
echo "=== 3/3. Описания кода от модели (code/summarize.py) ==="
rc=0
if [ "$SUMMARIZE" = "1" ]; then
    # На хосте: нужны клоны и графы в CODE_DIR, пишет в CODE_DIR/.summaries.
    # Сбой (модель не отвечает) не мешает проиндексировать то, что уже готово:
    # повторный запуск продолжит с места остановки
    if ! python3 code/summarize.py run; then
        echo "Описания дописаны не все — индексирую готовые." >&2
        rc=1
    fi
fi
# Индекс — только изменившиеся README. /data только на чтение, поэтому
# состояние индекса — в /docs/.state
if docker compose exec -T kb test -d /data/.summaries; then
    if ! MSYS_NO_PATHCONV=1 docker compose exec -T kb python -m kb.doc_index /data/.summaries \
            --source code-summaries --only README.md --state-dir /docs/.state; then
        echo "Индексация описаний не удалась." >&2
        rc=1
    fi
else
    echo "Описаний нет (CODE_DIR/.summaries) — пропускаю."
fi

echo
echo "Готово: граф и поиск по коду обновлены."
exit "$rc"
