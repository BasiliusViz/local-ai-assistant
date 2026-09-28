#!/usr/bin/env bash
# Код релиза CB18.5: скачать, построить граф, проиндексировать поиск.
#
#     ./update-cb.sh            всё целиком
#     ./update-cb.sh --check    только проверить доступ к репозиториям
#     ./update-cb.sh --dry-run  только показать, что скачается
#     ./update-cb.sh --download только скачать (блоками), граф и индекс не трогать
#     ./update-cb.sh --download --only core   один репозиторий по части имени
#     ./update-cb.sh --orphans  скачанные, но убранные из списка (их надо убрать из CB_DIR)
#     ./update-cb.sh --resume   доделать оборвавшуюся индексацию, не начиная сначала
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
        --resume)
            # Индексация оборвалась — доделать только её: скачивание и граф
            # уже прошли, записанные куски пропускаются (их id не меняются)
            echo "=== Продолжение индексации релиза (коллекция code_cb) ==="
            docker compose exec -T kb python -m kb.code_index /cb --collection code_cb --resume
            exit $?
            ;;
        --orphans)
            # Скачанные, но убранные из списка: sync.py их не удаляет, а
            # граф и индекс строятся по каталогу, не по списку
            CODE_DIR="$CB_DIR" CODE_GIT_REPOS="" CODE_GIT_REPOS_FILE="$LIST" python3 - <<'EOF'
import os, sys
from pathlib import Path
sys.path.insert(0, "repos")
import sync
sync.load_env()
root = Path(os.environ["CODE_DIR"])
wanted = {r.dirname for r in sync.parse_list(sync.repo_list(), sync.load_providers())}
extra = sorted(p.name for p in root.iterdir()
               if p.is_dir() and not p.name.startswith(".") and p.name != "graph"
               and p.name not in wanted)
print(f"В {root}, но не в списке ({len(extra)}):")
for name in extra:
    print(f"  {name}")
if extra:
    print("Попадут в граф и индекс, пока лежат там. Убрать, сохранив:")
    print(f"  mkdir -p {root}-excluded && mv " + " ".join(str(root / n) for n in extra) + f" {root}-excluded/")
EOF
            exit $?
            ;;
        --download)
            # Только скачать, без графа и индекса: удобно качать блоками
            # (закомментировать часть repos/cb.txt), граф и индекс — один раз в конце
            args=()
            for a in "$@"; do [ "$a" = --download ] || args+=("$a"); done
            sync ${args[@]+"${args[@]}"}
            rc=$?
            echo
            echo "Место: $(du -sh "$CB_DIR" | cut -f1) в $CB_DIR, свободно $(df -h "$CB_DIR" | awk 'NR==2 {print $4}')"
            exit "$rc"
            ;;
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
