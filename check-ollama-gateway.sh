#!/usr/bin/env bash
# Какие пути API Ollama пропускает шлюз — перед установкой Open WebUI.
#
#     ./check-ollama-gateway.sh
#
# Open WebUI ходит в Ollama не только за ответом модели: список моделей
# (/api/tags), версия (/api/version), загруженные модели (/api/ps), описание
# модели (/api/show). Если шлюз пускает только /api/chat и /api/embed,
# список моделей в Open WebUI будет пустым.
#
# Только чтение: модель не загружается, видеокарта не трогается, ничего не
# меняется. /api/chat и /api/embed не проверяются — ими уже пользуются
# Continue и kb. Адрес и ключ — из .env (OLLAMA_URL, OLLAMA_API_KEY,
# OLLAMA_AUTH_HEADER, OLLAMA_AUTH_PREFIX); ключ на экран не выводится.
#
# Код выхода: 0 — всё нужное открыто, 1 — что-то закрыто, 2 — ошибка запуска.

set -u
cd "$(dirname "$0")"

[ -f .env ] || { echo "Нет .env рядом со скриптом" >&2; exit 2; }
command -v curl >/dev/null || { echo "Нужен curl" >&2; exit 2; }
set -a
. ./.env
set +a

BASE="${OLLAMA_URL:-}"
BASE="${BASE%/}"
BASE="${BASE%/v1}"
[ -n "$BASE" ] || { echo "OLLAMA_URL пуст в .env" >&2; exit 2; }
HEADER="${OLLAMA_AUTH_HEADER:-Authorization}: ${OLLAMA_AUTH_PREFIX-Bearer }${OLLAMA_API_KEY:-}"
MODEL="${GEN_MODEL:-}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }

echo "Шлюз: $BASE (заголовок ${OLLAMA_AUTH_HEADER:-Authorization})"
echo

FAIL=0
probe() {   # $1 — метод, $2 — путь, $3 — тело (для POST), $4 — зачем
    local out code body
    if [ "$1" = POST ]; then
        out=$(curl -s -m 15 -w '\n%{http_code}' -H "$HEADER" -H 'Content-Type: application/json' \
            -X POST -d "$3" "$BASE$2" 2>/dev/null)
    else
        out=$(curl -s -m 15 -w '\n%{http_code}' -H "$HEADER" "$BASE$2" 2>/dev/null)
    fi
    code=${out##*$'\n'}
    body=${out%$'\n'*}
    body=$(printf '%s' "$body" | tr -d '\n' | cut -c1-120)
    if [ "$code" = 200 ]; then
        grn "  открыто   $1 $2 — $4"
        echo "            $body"
    else
        case "$code" in
            000) why="нет связи (адрес, сеть, таймаут)" ;;
            401|403) why="доступ запрещён (путь закрыт или ключ не принят)" ;;
            404) why="не найдено (путь закрыт шлюзом)" ;;
            *) why="ответ $code" ;;
        esac
        red "  ЗАКРЫТО   $1 $2 — $4: $why"
        [ -n "$body" ] && echo "            $body"
        FAIL=1
    fi
}

probe GET /api/version "" "версия Ollama"
probe GET /api/tags "" "список моделей (без него Open WebUI пуст)"
probe GET /api/ps "" "какие модели сейчас в памяти"
if [ -n "$MODEL" ]; then
    probe POST /api/show "{\"model\": \"$MODEL\"}" "описание модели $MODEL"
else
    echo "  пропуск   POST /api/show — GEN_MODEL не задан в .env"
fi

echo
if [ "$FAIL" = 0 ]; then
    grn "ИТОГ: всё нужное Open WebUI шлюз пропускает."
else
    red "ИТОГ: шлюз закрывает часть путей (выше). Обязателен /api/tags;"
    red "      без /api/version, /api/ps, /api/show Open WebUI работает, но с ошибками в журнале."
fi
exit "$FAIL"
