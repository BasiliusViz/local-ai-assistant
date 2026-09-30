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

# Что Graphify пропускает, в формате .gitignore — те же исключения, что у
# поиска (SKIP_DIRS и тесты в kb/code_index.py). У Graphify свой список
# короче: node_modules/build/target он пропускает, а vendor, тесты и
# сгенерированный код — нет. На релизе CB18.5 граф ОДНОГО Go-репозитория с
# vendor/ вышел 555 МБ (лимит чтения Graphify — 512), склеенный не влез бы
# в память. Файл кладётся в корень каждого репозитория; Graphify выкидывает
# по нему и то, что попало в граф раньше. Свой .graphifyignore репозитория
# (без нашей пометки) не трогаем
IGNORE_MARK="# LOCAL-AI: исключения для графа (code/sync.sh), файл перезаписывается"
write_graphifyignore() {
    f="$1/.graphifyignore"
    if [ -f "$f" ] && ! grep -qF "$IGNORE_MARK" "$f"; then
        echo "    [!] в репозитории свой .graphifyignore — оставляю его как есть"
        return
    fi
    cat > "$f" <<EOF
$IGNORE_MARK
vendor/
third_party/
migrations/
tests/
test/
testing/
e2e/
fixtures/
__tests__/
__mocks__/
generated/
__generated__/
coverage/
Pods/
obj/
.gradle/
*_test.go
test_*.py
*_test.py
conftest.py
*Test.java
*Tests.java
*IT.java
*Test.kt
*Tests.kt
*Spec.scala
*Test.cs
*Tests.cs
*.spec.ts
*.spec.js
*.test.ts
*.test.js
*.min.js
*.min.css
*.pb.go
*_pb2.py
*_pb2_grpc.py
*.generated.*
*.g.dart
*.designer.cs
bundle.js
EOF
}

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
    write_graphifyignore "$dir"
    # update, а не extract: только AST, без LLM и без сети
    graphify update "$dir" 2>&1 | grep -E "Rebuilt|error|Error" | sed 's/^/    /' || true
    if [ -f "$dir/graphify-out/graph.json" ]; then
        graphs="$graphs $dir/graphify-out/graph.json"
    fi
done

count=$(echo "$graphs" | wc -w)

# Граф релиза CB18.5 (cb-graph, GRAPH_MERGE=no): без склейки. 120 графов
# вместе — 3.4 ГБ, merge-graphs держит их в памяти целиком и падает. Вместо
# этого каждый graph.json потоково ложится в одну базу SQLite, её читает
# graph_server.py. Неизменённые репозитории пропускаются
if [ "${GRAPH_MERGE:-yes}" = "no" ]; then
    echo
    echo "=== Загрузка графов в базу (без склейки) ==="
    if [ "$count" -eq 0 ]; then
        echo "Ни одного графа не построено."
        exit 1
    fi
    python /app/graph_store.py build "$REPOS_DIR" --db "$GRAPH_DIR/graph.sqlite" --jenkins
    exit $?
fi

echo
echo "=== Сборка общего графа ==="
if [ "$count" -eq 0 ]; then
    echo "Ни одного графа не построено."
    exit 1
elif [ "$count" -eq 1 ]; then
    # Один репозиторий - merge не нужен
    cp $graphs "$GRAPH_DIR/graph.json"
    echo "Граф: $(basename $graphs) -> $GRAPH_DIR/graph.json"
else
    # Несколько репозиториев сливаются в один граф: связи между сервисами
    # видны только так.
    # Через временный файл и с проверкой кода возврата: раньше вывод шёл в
    # sed, код склейки терялся (set -e на конвейер не срабатывает), и упавшая
    # склейка — на релизе из 120 репозиториев ей не хватило памяти — давала
    # «Готово» без graph.json. Теперь прежний граф остаётся, а скрипт падает
    merge_log="$GRAPH_DIR/merge.log"
    if graphify merge-graphs $graphs --out "$GRAPH_DIR/graph.json.new" > "$merge_log" 2>&1 \
            && [ -s "$GRAPH_DIR/graph.json.new" ]; then
        sed 's/^/    /' "$merge_log"
        mv "$GRAPH_DIR/graph.json.new" "$GRAPH_DIR/graph.json"
    else
        rc=$?
        sed 's/^/    /' "$merge_log"
        rm -f "$GRAPH_DIR/graph.json.new"
        echo "Склейка графов не удалась (код $rc)."
        [ "$rc" = 137 ] && echo "137 — процесс убит, почти всегда нехватка памяти: free -h."
        exit 1
    fi
fi

echo
echo "=== Связи Jenkins ==="
# Graphify не видит общую библиотеку Jenkins (vars/*.groovy) и не знает, что
# шаг называется по имени файла. Дописываем связи сами, уже в слитый граф:
# пайплайн и шаг обычно в разных репозиториях. Сбой здесь не роняет граф
python /app/jenkins_graph.py --graph "$GRAPH_DIR/graph.json" --repos "$REPOS_DIR"     || echo "    [!] связи Jenkins не добавлены - граф остался без них"

echo
if [ ! -s "$GRAPH_DIR/graph.json" ]; then
    echo "Графа $GRAPH_DIR/graph.json нет — сборка не удалась, см. выше."
    exit 1
fi
echo "Готово: $GRAPH_DIR/graph.json, $(du -h "$GRAPH_DIR/graph.json" | cut -f1)"
