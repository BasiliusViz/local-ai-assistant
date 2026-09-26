#!/usr/bin/env bash
# Код релиза CB18.5: скачать, построить граф, проиндексировать поиск.
#
#     ./update-cb.sh            всё целиком
#     ./update-cb.sh --check    только проверить доступ к репозиториям
#     ./update-cb.sh --dry-run  только показать, что скачается
#
# Релиз статичный: по расписанию не обновляется, только этой командой.
# Список репозиториев — repos/cb.txt (пример: repos/cb.example.txt),
# каталог клонов — CB_DIR в .env. Токены те же, что для repos/sync.py.
#
# Запускать от ТОГО ЖЕ пользователя, кому принадлежат клоны в CB_DIR
# (как repos/cron.sh), и из-под него должен работать docker compose.
#
# Отдельно от обычного кода: поиск — коллекция code_cb (инструмент
# cb_search), граф — контейнер cb-graph (инструменты cb_*). См. guides/PLAN-CB.md

set -u
cd "$(dirname "$0")"

LIST=repos/cb.txt
if [ ! -f "$LIST" ]; then
    echo "Нет $LIST. Скопируйте пример и впишите репозитории релиза:" >&2
    echo "    cp repos/cb.example.txt $LIST" >&2
    exit 1
fi

# CB_DIR — из окружения или из .env
if [ -z "${CB_DIR:-}" ] && [ -f .env ]; then
    # как читает compose: допускаем export впереди и комментарий после
    CB_DIR=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}CB_DIR=//p' .env | tail -1 \
        | sed 's/[[:space:]]#.*//; s/[[:space:]]*$//' | tr -d "\"'\r")
fi
if [ -z "${CB_DIR:-}" ]; then
    echo "CB_DIR не задан: впишите в .env, например CB_DIR=/srv/local-ai/cb" >&2
    exit 1
fi
mkdir -p "$CB_DIR"

# CODE_GIT_REPOS обнуляем явно: sync.py склеивает его с файлом списка, и
# без этого обычный код из .env уехал бы в каталог релиза
sync() {
    CODE_DIR="$CB_DIR" CODE_GIT_REPOS="" CODE_GIT_REPOS_FILE="$LIST" \
        python3 repos/sync.py "$@"
}

for arg in "$@"; do
    case "$arg" in
        --check|--dry-run) sync "$@"; exit $? ;;
    esac
done

echo "=== 1/3. Репозитории релиза -> $CB_DIR ==="
if ! sync "$@"; then
    echo "Скачались не все репозитории — продолжаю с тем, что есть." >&2
    rc=1
fi

echo
echo "=== 2/3. Граф релиза (cb-graph) ==="
# Контейнер при старте граф не строит (code/cb-entry.sh) — строим здесь
docker compose up -d cb-graph
if ! docker compose exec -T cb-graph /app/sync.sh; then
    echo "Граф релиза не построен." >&2
    exit 1
fi
# Graphify читает graph.json при старте — перезапуск, чтобы подхватил новый
docker compose restart cb-graph

echo
echo "=== 3/3. Поиск по релизу (коллекция code_cb) ==="
# --recreate: релиз индексируется целиком, и удалённые файлы не должны
# остаться в выдаче
if ! docker compose exec -T kb python -m kb.code_index /cb --collection code_cb --recreate; then
    echo "Индексация релиза не удалась." >&2
    exit 1
fi

echo
echo "Готово: в Continue «посмотри в релизе, где …» и «кто в релизе вызывает …»."
exit "${rc:-0}"
