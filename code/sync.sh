#!/bin/sh
# Клонирует или обновляет репозитории и перестраивает граф кода.
#
# Запуск вручную:  docker compose exec code-graph /app/sync.sh
# По расписанию:   тот же вызов из планировщика раз в час
#
# Репозитории задаются переменной CODE_REPOS через пробел или запятую:
#   CODE_REPOS="https://git.company.local/team/service-a.git https://..."
# Приватные - через токен в URL или смонтированный ssh-ключ.

set -e

GRAPH_DIR="${GRAPH_DIR:-/data/graph}"
mkdir -p "$GRAPH_DIR"

# Репозитории могут лежать прямо в /data (просто показали каталог с кодом)
# либо в /data/repos (туда их кладёт клонирование). Поддерживаем оба:
# если repos/ нет и клонировать нечего - работаем с тем, что есть в /data
REPOS_DIR="${REPOS_DIR:-/data/repos}"
if [ ! -d "$REPOS_DIR" ] && [ -z "${CODE_REPOS:-}" ]; then
    REPOS_DIR="/data"
fi
mkdir -p "$REPOS_DIR" 2>/dev/null || true
echo "Каталог с репозиториями: $REPOS_DIR"

if [ -z "${CODE_REPOS:-}" ]; then
    echo "CODE_REPOS не задана - клонирование пропускаем, строим граф по тому,"
    echo "что уже лежит в каталоге."
    if [ -z "$(ls -A "$REPOS_DIR" 2>/dev/null)" ]; then
        echo "Каталог пуст. Положите туда код или укажите CODE_REPOS в .env."
        exit 0
    fi
fi

# git в образе может отсутствовать: он ставится только при INSTALL_GIT=yes,
# а по умолчанию не нужен - код обычно просто лежит в CODE_DIR. Без этой
# проверки в логах остаётся невнятное "git: not found" от каждого вызова,
# причём скрипт идёт дальше: вызовы завёрнуты в конвейер с sed, и set -e
# на них не срабатывает
if [ -n "${CODE_REPOS:-}" ] && ! command -v git >/dev/null 2>&1; then
    echo "CODE_REPOS задана, но git в образе нет - клонирование пропускаем."
    echo "Чтобы клонировать, пересоберите образ с INSTALL_GIT=yes в .env."
    echo "Граф будет построен по тому, что уже лежит в каталоге."
    CODE_REPOS=""
fi

[ -n "${CODE_REPOS:-}" ] && echo "=== Синхронизация репозиториев ==="
for repo in $(echo "${CODE_REPOS:-}" | tr ',' ' '); do
    name=$(basename "$repo" .git)
    target="$REPOS_DIR/$name"

    if [ -d "$target/.git" ]; then
        echo "--- $name: обновление"
        # Жёстко на удалённое состояние: локальных правок тут быть не должно,
        # а расхождение веток остановило бы синхронизацию навсегда
        git -C "$target" fetch --depth 1 origin 2>&1 | sed 's/^/    /'
        branch=$(git -C "$target" rev-parse --abbrev-ref origin/HEAD 2>/dev/null | sed 's|origin/||')
        branch="${branch:-HEAD}"
        git -C "$target" reset --hard "origin/$branch" 2>&1 | sed 's/^/    /'
    else
        echo "--- $name: клонирование"
        # depth 1: история не нужна, нужен снимок кода. Экономит место и время
        git clone --depth 1 "$repo" "$target" 2>&1 | sed 's/^/    /'
    fi
done

echo
echo "=== Построение графа ==="
graphs=""
for dir in "$REPOS_DIR"/*/; do
    [ -d "$dir" ] || continue
    name=$(basename "$dir")
    # Свой же каталог с результатом за репозиторий не считаем: когда код
    # лежит прямо в /data, graph/ оказывается рядом с проектами
    [ "$dir" = "$GRAPH_DIR/" ] && continue
    case "$name" in graph|.git) continue ;; esac
    echo "--- $name"
    # update, а не extract: только AST, без LLM и без сети
    graphify update "$dir" 2>&1 | grep -E "Rebuilt|error|Error" | sed 's/^/    /' || true
    if [ -f "$dir/graphify-out/graph.json" ]; then
        graphs="$graphs $dir/graphify-out/graph.json"
    fi
done

echo
echo "=== Сборка общего графа ==="
count=$(echo "$graphs" | wc -w)
if [ "$count" -eq 0 ]; then
    echo "Ни одного графа не построено."
    exit 1
elif [ "$count" -eq 1 ]; then
    # Один репозиторий - merge не нужен
    cp $graphs "$GRAPH_DIR/graph.json"
    echo "Граф: $(basename $graphs) -> $GRAPH_DIR/graph.json"
else
    # Несколько репозиториев сливаются в один граф: связи между сервисами
    # видны только так
    graphify merge-graphs $graphs --out "$GRAPH_DIR/graph.json" 2>&1 | sed 's/^/    /'
fi

echo
echo "Готово. Узлов: $(grep -o '"id"' "$GRAPH_DIR/graph.json" | wc -l) (приблизительно)"
