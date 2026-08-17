#!/usr/bin/env bash
# Проверка развёрнутой системы: сервисы, модели, данные, живой поиск.
# Печатает список с отметками — сразу видно, что не работает.
#
#     ./healthcheck.sh
#
# Код возврата 0 — всё в порядке, 1 — есть неисправности.
# Годится для мониторинга и для проверки после обновлений.

set -u
cd "$(dirname "$0")"

[ -f .env ] && { set -a; . ./.env; set +a; }

OLLAMA_PORT="${OLLAMA_PORT:-11434}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
KB_PORT="${KB_PORT:-8010}"
CODE_GRAPH_PORT="${CODE_GRAPH_PORT:-8011}"
GEN_MODEL="${GEN_MODEL:-qwen3:8b}"
EMBED_MODEL="${EMBED_MODEL:-bge-m3}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; NC=$'\033[0m'
problems=0

ok()   { echo "  ${GREEN}[ok]${NC}    $*"; }
bad()  { echo "  ${RED}[нет]${NC}   $*"; problems=$((problems + 1)); }
warn() { echo "  ${YELLOW}[!]${NC}     $*"; }

# Ollama бывает внешней (на машине с GPU) — тогда её контейнера здесь нет,
# и это нормально. Определяем по адресу из .env
OLLAMA_BASE="${OLLAMA_URL:-http://localhost:${OLLAMA_PORT}/v1}"
OLLAMA_BASE="${OLLAMA_BASE%/v1}"
OLLAMA_BASE="${OLLAMA_BASE%/}"
case "$OLLAMA_BASE" in
    *localhost*|*127.0.0.1*|*//ollama*) OLLAMA_LOCAL=1; where="локальная" ;;
    *) OLLAMA_LOCAL=0; where="внешняя" ;;
esac

echo "=== Контейнеры ==="
names="qdrant kb code-graph"
[ "$OLLAMA_LOCAL" = "1" ] && names="ollama $names"
for name in $names; do
    state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)
    case "$state" in
        running) ok "$name работает" ;;
        "")      bad "$name не создан" ;;
        *)       bad "$name в состоянии $state (docker logs $name)" ;;
    esac
done

echo
echo "=== Сервисы ==="
code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${QDRANT_PORT}/readyz")
[ "$code" = "200" ] && ok "Qdrant :${QDRANT_PORT}" || bad "Qdrant :${QDRANT_PORT} отвечает $code"

# Модели проверяем ПО СЕТИ, а не через docker exec: контейнера может не быть
AUTH=""
if [ -n "${OLLAMA_API_KEY:-}" ]; then
    AUTH="${OLLAMA_AUTH_HEADER:-Authorization}: ${OLLAMA_AUTH_PREFIX-Bearer }${OLLAMA_API_KEY}"
fi
models=$(curl -s ${AUTH:+-H "$AUTH"} "${OLLAMA_BASE}/api/tags" 2>/dev/null)
if [ -n "$models" ]; then
    ok "Ollama (${where}): ${OLLAMA_BASE}"
else
    bad "Ollama (${where}) недоступна по адресу ${OLLAMA_BASE}"
fi

echo
echo "=== Модели ==="
for model in "${GEN_MODEL}" "${EMBED_MODEL}"; do
    if echo "$models" | grep -q "${model%%:*}"; then
        ok "$model"
    elif [ -n "$models" ]; then
        bad "$model не найдена на сервере Ollama"
    else
        bad "$model — не удалось получить список моделей"
    fi
done

# Обе модели должны помещаться в память ОДНОВРЕМЕННО, иначе каждый запрос
# оплачивает перезагрузку с диска — самая частая причина «всё тормозит».
# Проверяем только у локальной: на чужой машине это не наша забота
if [ "$OLLAMA_LOCAL" = "1" ]; then
    loaded=$(docker exec ollama ollama ps 2>/dev/null | tail -n +2 | grep -c . || true)
    if [ "${loaded:-0}" -ge 2 ]; then
        ok "в памяти моделей: $loaded"
    else
        warn "в памяти моделей: ${loaded:-0}. После простоя это нормально; если так во время работы — проверьте OLLAMA_MAX_LOADED_MODELS и объём GPU"
    fi
fi

echo
echo "=== Данные ==="
for coll in knowledge code; do
    count=$(curl -s "http://localhost:${QDRANT_PORT}/collections/${coll}" \
        | grep -o '"points_count":[0-9]*' | head -1 | cut -d: -f2)
    if [ -n "${count:-}" ] && [ "$count" -gt 0 ]; then
        ok "коллекция ${coll}: ${count} чанков"
    elif [ -n "${count:-}" ]; then
        warn "коллекция ${coll} пуста — данные не проиндексированы"
    else
        warn "коллекции ${coll} нет — она создастся при первой индексации"
    fi
done

graph_nodes=$(curl -s -X POST "http://localhost:${CODE_GRAPH_PORT}/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null | head -c 200)
[ -n "$graph_nodes" ] && ok "граф кода :${CODE_GRAPH_PORT} отвечает" || warn "граф кода :${CODE_GRAPH_PORT} не отвечает (нормально, если CODE_REPOS пуста)"

echo
echo "=== Живой поиск ==="
# Проверяем не «порт открыт», а что поиск реально что-то находит:
# сервис может отвечать 200 и при этом возвращать пустоту из-за пустой базы
result=$(docker exec kb python -c "
import json
from kb.retriever import search
try:
    hits = search('как это работает', top_k=3)
    print(json.dumps({'ok': True, 'found': len(hits.hits)}))
except Exception as e:
    print(json.dumps({'ok': False, 'error': str(e)[:120]}))
" 2>/dev/null | tail -1)

if echo "$result" | grep -q '"ok": true'; then
    found=$(echo "$result" | grep -o '"found": [0-9]*' | cut -d' ' -f2)
    [ "${found:-0}" -gt 0 ] && ok "поиск по документам работает (найдено: $found)" \
        || warn "поиск отработал, но ничего не нашёл — проверьте, что документы проиндексированы"
else
    bad "поиск по документам сломан: $(echo "$result" | head -c 160)"
fi

echo
if [ "$problems" -eq 0 ]; then
    echo "${GREEN}Всё в порядке.${NC}"
    exit 0
fi
echo "${RED}Неисправностей: ${problems}${NC}"
exit 1
