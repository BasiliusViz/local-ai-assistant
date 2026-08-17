#!/usr/bin/env bash
# Развёртывание с нуля: поднимает стек, скачивает модели, создаёт коллекции,
# индексирует данные. Проверяет каждый шаг и останавливается на первой ошибке,
# объясняя, что делать.
#
#     ./deploy.sh
#
# Повторный запуск безопасен: контейнеры не пересоздаются зря, модели не
# качаются заново, индексация обновляет точки, а не дублирует их.

set -u

cd "$(dirname "$0")"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; NC=$'\033[0m'
ok()   { echo "  ${GREEN}[ok]${NC}   $*"; }
warn() { echo "  ${YELLOW}[!]${NC}    $*"; }
fail() { echo "  ${RED}[нет]${NC}  $*"; exit 1; }
step() { echo; echo "=== $* ==="; }

compose() { docker compose "$@"; }

# ---------- 1. окружение ----------
step "1/6. Проверка окружения"

command -v docker >/dev/null || fail "docker не установлен"
docker info >/dev/null 2>&1 || fail "демон Docker не отвечает. Запущен ли он? Хватает ли прав (группа docker)?"
ok "docker работает"

docker compose version >/dev/null 2>&1 || fail "нет плагина docker compose (нужен compose v2)"
ok "docker compose на месте"

if [ ! -f .env ]; then
    cp .env.example .env
    warn ".env создан из образца — проверьте пути и модели, затем запустите снова"
    exit 1
fi
# shellcheck disable=SC1091
set -a; . ./.env; set +a
ok ".env прочитан"

# GPU не обязателен, но без него всё будет очень медленно — предупреждаем сразу
if docker info 2>/dev/null | grep -qi nvidia; then
    ok "GPU виден Docker"
else
    warn "GPU не виден Docker. На CPU модели работают в десятки раз медленнее."
    warn "Нужен nvidia-container-toolkit; проверка: docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi"
fi

for dir in "${MODELS_DIR}" "${QDRANT_DIR}" "${CODE_DIR}" "${DOCS_DIR}"; do
    mkdir -p "$dir" 2>/dev/null || fail "не удалось создать каталог $dir"
done
ok "каталоги данных готовы"

# ---------- 2. контейнеры ----------
step "2/6. Запуск контейнеров"

# Локальная Ollama живёт в отдельном файле: в проде её нет, модель внешняя
OLLAMA_BASE="${OLLAMA_URL:-http://localhost:${OLLAMA_PORT:-11434}/v1}"
OLLAMA_BASE="${OLLAMA_BASE%/v1}"
OLLAMA_BASE="${OLLAMA_BASE%/}"
case "$OLLAMA_BASE" in
    *localhost*|*127.0.0.1*|*//ollama*) OLLAMA_LOCAL=1 ;;
    *) OLLAMA_LOCAL=0 ;;
esac

if [ "$OLLAMA_LOCAL" = "1" ]; then
    ok "Ollama локальная — будет поднята здесь"
    docker compose -f docker-compose.yml -f docker-compose.local-llm.yml up -d --build \
        || fail "не удалось поднять стек. Смотрите: docker compose logs"
else
    ok "Ollama внешняя: $OLLAMA_BASE (GPU на этой машине не нужен)"
    compose up -d --build || fail "не удалось поднять стек. Смотрите: docker compose logs"
fi
ok "контейнеры запущены"

echo "  ожидание готовности Qdrant..."
for i in $(seq 1 60); do
    q=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${QDRANT_PORT:-6333}/readyz" 2>/dev/null)
    [ "$q" = "200" ] && break
    sleep 3
done
[ "$q" = "200" ] || fail "Qdrant не отвечает (порт ${QDRANT_PORT:-6333})"
ok "Qdrant отвечает"

# ---------- 3. модели ----------
step "3/6. Модели"

# Модели проверяем ПО СЕТИ: контейнера Ollama здесь может не быть вовсе
AUTH_ARGS=""
if [ -n "${OLLAMA_API_KEY:-}" ]; then
    hdr="${OLLAMA_AUTH_HEADER:-Authorization}"
    pfx="${OLLAMA_AUTH_PREFIX-Bearer }"
    AUTH_ARGS="-H"
    AUTH_VALUE="${hdr}: ${pfx}${OLLAMA_API_KEY}"
fi

tags=$(curl -s ${AUTH_ARGS:+"$AUTH_ARGS" "$AUTH_VALUE"} "${OLLAMA_BASE}/api/tags" 2>/dev/null)
[ -n "$tags" ] || fail "Ollama недоступна по адресу ${OLLAMA_BASE}"

for model in "${GEN_MODEL}" "${EMBED_MODEL}"; do
    if echo "$tags" | grep -q "${model%%:*}"; then
        ok "$model на месте"
    elif [ "$OLLAMA_LOCAL" = "1" ]; then
        echo "  скачивание $model (это может занять минуты)..."
        docker exec ollama ollama pull "$model" || fail "не удалось скачать $model"
        ok "$model загружена"
    else
        # На чужую машину модель не скачать — только попросить владельца
        fail "$model нет на сервере ${OLLAMA_BASE}. Загрузите её там: ollama pull $model"
    fi
done

# Размерность вектора обязана совпасть с коллекцией, иначе поиск молча врёт
dim=$(curl -s -X POST "${OLLAMA_BASE}/v1/embeddings" \
    -H 'Content-Type: application/json' \
    ${AUTH_ARGS:+"$AUTH_ARGS" "$AUTH_VALUE"} \
    -d "{\"model\":\"${EMBED_MODEL}\",\"input\":[\"тест\"]}" \
    | grep -o '"embedding":\[[^]]*' | tr ',' '\n' | wc -l)
if [ "$dim" -gt 100 ]; then
    ok "эмбеддер отвечает, размерность вектора: $dim"
else
    fail "эмбеддер не вернул вектор по адресу ${OLLAMA_BASE}"
fi

# ---------- 4. документы ----------
step "4/6. Индексация документов"

docs_count=$(find "${DOCS_DIR}" -type f \( -name '*.md' -o -name '*.txt' \) 2>/dev/null | wc -l)
if [ "$docs_count" -eq 0 ]; then
    warn "в ${DOCS_DIR} нет файлов .md/.txt — поиск по документам будет пустым"
    warn "положите документы туда и запустите: docker compose exec kb python -m kb.doc_index /docs --source local"
else
    # Каждый подкаталог — отдельный источник. Иначе одни и те же документы,
    # разложенные по папкам, попадут под одной меткой, а при повторном запуске
    # с другой меткой продублируются. Плюс по источнику можно фильтровать поиск
    subdirs=$(find "${DOCS_DIR}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
    if [ -n "$subdirs" ]; then
        for dir in $subdirs; do
            name=$(basename "$dir")
            compose exec -T kb python -m kb.doc_index "/docs/$name" --source "$name" \
                || fail "индексация $name не удалась"
            ok "источник $name проиндексирован"
        done
    else
        compose exec -T kb python -m kb.doc_index /docs --source local \
            || fail "индексация документов не удалась"
        ok "документы проиндексированы ($docs_count файлов)"
    fi
fi

# ---------- 5. код ----------
step "5/6. Код"

if [ -z "${CODE_REPOS:-}" ]; then
    warn "CODE_REPOS пуста — поиск по коду и граф отключены"
    warn "укажите репозитории в .env и запустите ./update-code.sh"
else
    compose exec -T code-graph /app/sync.sh || fail "синхронизация репозиториев не удалась"
    compose exec -T kb python -m kb.code_index /data/repos || fail "индексация кода не удалась"
    ok "репозитории склонированы, граф построен, код проиндексирован"
fi

# ---------- 6. проверка ----------
step "6/6. Проверка"

./healthcheck.sh || fail "проверка не пройдена, см. вывод выше"

echo
echo "${GREEN}Развёртывание завершено.${NC}"
echo
echo "Подключение в VS Code (Continue), ~/.continue/config.yaml:"
echo
echo "  mcpServers:"
echo "    - name: knowledge-base"
echo "      type: streamable-http"
echo "      url: http://<адрес-сервера>:${KB_PORT:-8010}/mcp"
echo "    - name: code-graph"
echo "      type: streamable-http"
echo "      url: http://<адрес-сервера>:${CODE_GRAPH_PORT:-8011}/mcp"
echo
echo "Обновление данных: ./update-code.sh (код), docker compose exec kb python -m kb.doc_index /docs --source local (документы)"
