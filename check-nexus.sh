#!/usr/bin/env bash
# Разведка Nexus перед сборкой образов: какие репозитории есть, отвечают ли
# они, нужна ли авторизация и лежит ли там нужный нам Debian.
#
#     ./check-nexus.sh https://nexus.company.local
#
# Логин спрашивается интерактивно и в аргументы не выносится: там он попал бы
# и в историю оболочки, и в список процессов. Сначала всё пробуется анонимно —
# если хватает анонимного доступа, учётные данные не понадобятся и в сборке.
#
# Скрипт только читает и ничего не меняет.

set -u

NEXUS="${1:-}"
if [ -z "$NEXUS" ]; then
    echo "Укажите адрес Nexus: ./check-nexus.sh https://nexus.company.local" >&2
    exit 1
fi
NEXUS="${NEXUS%/}"

# Нам нужен Debian той версии, на которой собран python:3.11-slim
SUITE="${SUITE:-trixie}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; NC=$'\033[0m'
ok()   { echo "  ${GREEN}[ok]${NC}   $*"; }
warn() { echo "  ${YELLOW}[!]${NC}    $*"; }
bad()  { echo "  ${RED}[нет]${NC}  $*"; }
step() { echo; echo "=== $* ==="; }

command -v curl >/dev/null || { echo "нужен curl" >&2; exit 1; }

# Учётные данные кладём в файл с правами 600 и отдаём curl через --config:
# в отличие от -u они не видны в ps, пока идёт запрос
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
CURLRC="$TMP/curlrc"; : > "$CURLRC"; chmod 600 "$CURLRC"
AUTH=0

# $1 - путь, дальше опционально "auth". Печатает код ответа
code() {
    local path="$1" mode="${2:-anon}" out
    local args=(-s -o /dev/null -L --max-time 20 -w '%{http_code}')
    [ "$mode" = "auth" ] && args+=(--config "$CURLRC")
    # curl при обрыве связи и сам печатает 000, но выходит с ненулевым кодом:
    # добавлять своё значение через || нельзя, иначе получится "000000"
    out=$(curl "${args[@]}" "${NEXUS}${path}" 2>/dev/null)
    echo "${out:-000}"
}

body() {
    local path="$1" args=(-s -L --max-time 30)
    [ "$AUTH" = "1" ] && args+=(--config "$CURLRC")
    curl "${args[@]}" "${NEXUS}${path}" 2>/dev/null
}

# ---------- 1. связь ----------
step "1/4. Связь с Nexus"

c=$(code /service/rest/v1/status)
case "$c" in
    200) ok "Nexus отвечает: $NEXUS" ;;
    000) bad "нет соединения с $NEXUS — проверьте адрес, DNS и доступность порта"; exit 1 ;;
    *)   warn "статус вернул $c — возможно, закрыт даже он; продолжаем" ;;
esac

# ---------- 2. авторизация ----------
step "2/4. Доступ к списку репозиториев"

c=$(code /service/rest/v1/repositories)
if [ "$c" = "200" ]; then
    ok "список репозиториев открыт анонимно — логин для сборки не нужен"
else
    warn "анонимно список недоступен (код $c), нужен логин"
    printf "  логин: "; read -r NEXUS_USER
    printf "  пароль: "; stty -echo 2>/dev/null; read -r NEXUS_PASS; stty echo 2>/dev/null; echo
    printf 'user = "%s:%s"\n' "$NEXUS_USER" "$NEXUS_PASS" > "$CURLRC"
    unset NEXUS_PASS
    c=$(code /service/rest/v1/repositories auth)
    if [ "$c" = "200" ]; then
        AUTH=1
        ok "логин принят"
        warn "тогда и сборке образов понадобятся эти же учётные данные"
    else
        bad "с логином тоже $c — проверьте пару или права учётной записи"
        exit 1
    fi
fi

# ---------- 3. что вообще есть ----------
step "3/4. Репозитории"

JSON=$(body /service/rest/v1/repositories)
if command -v python3 >/dev/null; then
    LIST=$(printf '%s' "$JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for r in data:
    print("%s\t%s\t%s\t%s" % (r.get("format",""), r.get("type",""), r.get("name",""), r.get("url","")))
' 2>/dev/null)
else
    warn "нет python3 — разберу список грубо, без форматов"
    LIST=$(printf '%s' "$JSON" | tr ',' '\n' | grep -oE '"name":"[^"]+"' | cut -d'"' -f4 | sed 's/^/?\t?\t/;s/$/\t/')
fi

if [ -z "$LIST" ]; then
    bad "список репозиториев не разобран"
    exit 1
fi

for fmt in apt pypi docker; do
    found=$(printf '%s\n' "$LIST" | awk -F'\t' -v f="$fmt" '$1==f {print "    " $2 "  " $3 "  " $4}')
    if [ -n "$found" ]; then
        ok "формат $fmt:"
        printf '%s\n' "$found"
    else
        warn "репозиториев формата $fmt не найдено"
    fi
done

# ---------- 4. пригодность для сборки ----------
step "4/4. Годятся ли они нам"

APT_REPOS=$(printf '%s\n' "$LIST" | awk -F'\t' '$1=="apt" {print $4}')
if [ -z "$APT_REPOS" ]; then
    bad "apt-репозиториев нет — образ code-graph собрать не выйдет"
else
    for url in $APT_REPOS; do
        base="${url%/}"
        path="${base#"$NEXUS"}"
        for s in "$SUITE" "${SUITE}-security"; do
            m="anon"; [ "$AUTH" = "1" ] && m="auth"
            c=$(code "${path}/dists/${s}/InRelease" "$m")
            if [ "$c" = "200" ]; then
                ok "$base -> $s есть"
            else
                warn "$base -> $s нет (код $c)"
            fi
        done
    done
    echo
    echo "  Нужны ДВА адреса с ответом 200: один на ${SUITE}, другой на ${SUITE}-security."
    echo "  Их и пропишем в code/Dockerfile как APT_URI и APT_SECURITY_URI."
fi

PYPI=$(printf '%s\n' "$LIST" | awk -F'\t' '$1=="pypi" {print $4; exit}')
if [ -n "$PYPI" ]; then
    base="${PYPI%/}"; path="${base#"$NEXUS"}"
    m="anon"; [ "$AUTH" = "1" ] && m="auth"
    c=$(code "${path}/simple/" "$m")
    [ "$c" = "200" ] && ok "PyPI: ${base}/simple/ отвечает" || warn "PyPI: ${base}/simple/ вернул $c"
fi

DOCKER=$(printf '%s\n' "$LIST" | awk -F'\t' '$1=="docker" {print $4; exit}')
if [ -n "$DOCKER" ]; then
    base="${DOCKER%/}"; path="${base#"$NEXUS"}"
    m="anon"; [ "$AUTH" = "1" ] && m="auth"
    c=$(code "${path}/v2/" "$m")
    # 401 - нормальный ответ реестра без токена: соединение есть
    case "$c" in
        200|401) ok "Docker: ${base}/v2/ отвечает ($c)" ;;
        *)       warn "Docker: ${base}/v2/ вернул $c" ;;
    esac
    echo "  Учтите: docker-репозиторий обычно слушает СВОЙ порт, а не общий."
    echo "  Его видно в Nexus у репозитория, поле HTTP port."
fi

echo
echo "Готово. Пришлите этот вывод — по нему пропишем адреса в code/Dockerfile."
