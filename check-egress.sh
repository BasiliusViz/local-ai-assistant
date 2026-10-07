#!/usr/bin/env bash
# Проверка: может ли сервер ОТПРАВИТЬ что-то в интернет.
#
#     ./check-egress.sh                # пробы наружу + настройки + текущие соединения
#     ./check-egress.sh --watch 300    # плюс 5 минут смотреть, кто соединяется наружу
#     ./check-egress.sh --quiet        # тихо: без проб, только настройки и соединения
#
# С --quiet скрипт НИЧЕГО НЕ ОТПРАВЛЯЕТ: читает маршруты, правила
# брандмауэра сервера, прокси, DNS и смотрит уже существующие соединения.
# Этого хватает, чтобы увидеть, что сервер сам по себе наружу не ограничен
# или что кто-то уже ходит наружу. Но что пропустит брандмауэр контура дальше
# по сети, без пробы не узнать — это вопрос к сетевикам, либо запуск без --quiet.
#
# По умолчанию шлёт пробы: соединения без данных на 1.1.1.1, 8.8.8.8 и др., DNS-запрос
# про example.com на 8.8.8.8, POST со словом "egress-check" на example.com. Ваших
# данных в них нет, НО попытки выхода (даже заблокированные) брандмауэр и IDS
# обычно пишут в журнал — может прийти вопрос от безопасников. Лучше
# предупредить их заранее или попросить, чтобы проверили они.
#
# Важно понимать: «скачивать можно, отправлять нельзя» бывает только через
# посредника (зеркало Nexus, прокси с белым списком). Если сервер ходит в
# интернет напрямую, скачивание и отправка — одно и то же соединение.
#
# Ничего не меняет на сервере.
# Код выхода: 0 — наружу не уйти, 1 — найден путь наружу, 2 — ошибка запуска.

set -u
cd "$(dirname "$0")"

WATCH=0
ACTIVE=1
SEEN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --watch) WATCH="${2:-300}"; shift ;;
        --quiet) ACTIVE=0 ;;
        --active) ACTIVE=1 ;;
        *) echo "Неизвестный ключ: $1" >&2; exit 2 ;;
    esac
    shift
done

command -v python3 >/dev/null || { echo "Нужен python3" >&2; exit 2; }

LEAK=0
WARN=0
red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
yel()  { printf '\033[33m%s\033[0m\n' "$*"; }

# Пробы — одна программа на Python (только stdlib): одинаково работает на
# хосте и в контейнере, где нет ни curl, ни dig. Строки вывода:
#   OPEN|BLOCK|INFO <проба> <подробности>
PROBE=$(cat <<'PY'
import os, socket, ssl, struct, sys, urllib.parse, http.client

T = 4
def out(s, name, d=""): print(f"{s}\t{name}\t{d}", flush=True)

def tcp(host, port):
    try:
        socket.create_connection((host, port), T).close(); return True, "соединение установлено"
    except Exception as e:
        return False, type(e).__name__ + ": " + str(e)[:80]

# 1. Прямой TCP на адреса (без DNS) и на имена (с DNS)
for host, port in [("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443), ("1.1.1.1", 80),
                   ("example.com", 443), ("github.com", 443), ("pypi.org", 443),
                   ("telemetry.qdrant.io", 443)]:
    ok, d = tcp(host, port)
    out("OPEN" if ok else "BLOCK", f"tcp {host}:{port}", d)

# 2. DNS: резолвит ли местный DNS внешние имена (канал утечки через DNS-запросы)
try:
    ip = socket.gethostbyname("example.com"); out("INFO", "dns-local example.com", "резолвится: " + ip)
except Exception as e:
    out("INFO", "dns-local example.com", "не резолвится")

# 3. DNS напрямую на внешний сервер по UDP
q = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
for srv in ("8.8.8.8", "1.1.1.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(T)
    try:
        s.sendto(q, (srv, 53)); s.recvfrom(512); out("OPEN", f"udp-dns {srv}:53", "ответ получен")
    except Exception as e:
        out("BLOCK", f"udp-dns {srv}:53", type(e).__name__)
    finally:
        s.close()

# 4. Прямой POST (мимо прокси): если дошёл ответ — отправка наружу возможна.
# Сертификат не проверяем намеренно: важно лишь, дошёл ли запрос, а TLS-перехват
# на выходе контура иначе дал бы ложное «закрыто». Передаётся только "egress-check"
try:
    c = http.client.HTTPSConnection("example.com", 443, timeout=T,
                                    context=ssl._create_unverified_context())
    c.request("POST", "/", body=b"egress-check"); r = c.getresponse()
    out("OPEN", "post-direct https://example.com", f"HTTP {r.status}")
except Exception as e:
    out("BLOCK", "post-direct https://example.com", type(e).__name__)

# 5. Через прокси из окружения
px = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or \
     os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
if not px:
    out("INFO", "proxy", "в окружении нет")
else:
    u = urllib.parse.urlsplit(px if "://" in px else "http://" + px)
    out("INFO", "proxy", f"{u.hostname}:{u.port or 3128}")
    for target in ("1.1.1.1:443", "example.com:443"):
        try:
            c = http.client.HTTPConnection(u.hostname, u.port or 3128, timeout=T)
            c.request("CONNECT", target, headers={"Host": target}); r = c.getresponse()
            out("OPEN" if r.status == 200 else "BLOCK", f"proxy-connect {target}", f"HTTP {r.status}")
        except Exception as e:
            out("BLOCK", f"proxy-connect {target}", type(e).__name__)
    try:
        c = http.client.HTTPConnection(u.hostname, u.port or 3128, timeout=T)
        c.request("POST", "http://example.com/", body=b"egress-check"); r = c.getresponse()
        # Запрет прокси — 403/407 или 5xx; любой другой ответ (даже 405 от
        # самого example.com) значит, что POST до сайта дошёл
        out("BLOCK" if r.status in (403, 407) or r.status >= 500 else "OPEN",
            "proxy-post http://example.com", f"HTTP {r.status} {r.getheader('Server') or ''}")
    except Exception as e:
        out("BLOCK", "proxy-post http://example.com", type(e).__name__)
PY
)

report() {   # $1 — где запускали; stdin — вывод проб
    while IFS=$'\t' read -r st name det; do
        case "$st" in
            OPEN)  red   "  НАРУЖУ ОТКРЫТО  $name — $det"; LEAK=1 ;;
            BLOCK) grn   "  закрыто         $name — $det" ;;
            INFO)
                if [ "$name" = "dns-local example.com" ] && [ "${det#резолвится}" != "$det" ]; then
                    yel "  внимание        $name — $det (через DNS-запросы можно вынести данные)"
                    WARN=1
                else
                    echo "  инфо            $name — $det"
                fi ;;
        esac
    done
}

echo "=== 1. Схема выхода в интернет ==="
PX=$(env | grep -i '_proxy=' | sed 's/:[^:@/]*@/:***@/; s/^/  окружение: /')
echo "${PX:-  прокси в окружении нет}"
for f in /etc/systemd/system/docker.service.d/*.conf "$HOME/.docker/config.json" /etc/docker/daemon.json \
         /etc/apt/apt.conf /etc/apt/apt.conf.d/* /etc/pip.conf "$HOME/.config/pip/pip.conf" "$HOME/.pip/pip.conf"; do
    [ -r "$f" ] || continue
    grep -iE 'proxy|registry-mirrors|index-url|https?://' "$f" 2>/dev/null \
        | sed 's/:[^:@/]*@/:***@/' | sed "s|^|  $f: |"
done
[ -r .env ] && grep -E '^(PIP_INDEX_URL|OLLAMA_URL|CONFLUENCE_URL|JIRA_URL|DOJO_URL)=' .env \
    | sed 's/^/  .env: /'

echo
echo "=== 2. Маршруты и брандмауэр сервера (без отправки) ==="
ROUTE=$(ip route show default 2>/dev/null)
if ! command -v ip >/dev/null; then
    yel "  нет команды ip — маршруты не проверить"; WARN=1
elif [ -n "$ROUTE" ]; then
    echo "  маршрут по умолчанию: $ROUTE"
    echo "  (пакеты наружу сервер отправит; дойдут ли — решает брандмауэр контура)"
else
    grn "  маршрута по умолчанию нет — наружу сервер пакеты не отправит"
fi
if [ "$(id -u)" = 0 ]; then
    # FORWARD не смотрим: Docker сам ставит там политику DROP и свои DROP для
    # изоляции сетей — это не запрет выхода наружу. Исходящие самого сервера —
    # цепочка OUTPUT (и ufw), контейнеров — DOCKER-USER
    fw_show() {   # $1 — для кого, $2 — правила
        if [ -n "$2" ]; then
            echo "  $1: есть ограничивающие правила (запрещают ли всё — смотрите сами):"
            printf '%s\n' "$2" | sed 's/^/    /'
        else
            yel "  $1: на сервере не ограничены"; WARN=1
        fi
    }
    fw_show "исходящие сервера (iptables OUTPUT)" \
        "$(iptables -S OUTPUT 2>/dev/null | grep -E -- '-P OUTPUT DROP|-j (DROP|REJECT)')"
    fw_show "исходящие контейнеров (iptables DOCKER-USER)" \
        "$(iptables -S DOCKER-USER 2>/dev/null | grep -E -- '-j (DROP|REJECT)')"
    if command -v ufw >/dev/null; then
        ufw status verbose 2>/dev/null | grep -i '^Default' | sed 's/^/  ufw: /'
    fi
else
    yel "  правила брандмауэра видны только под root (sudo ./check-egress.sh)"
fi
[ -r /etc/resolv.conf ] && grep -E '^nameserver' /etc/resolv.conf | sed 's/^/  DNS: /'

echo
echo "=== 3. Пробы наружу ==="
if [ $ACTIVE = 1 ]; then
echo "  -- с хоста --"
report host < <(PYTHONIOENCODING=utf-8 python3 -c "$PROBE")
if command -v ping >/dev/null; then
    if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then
        red "  НАРУЖУ ОТКРЫТО  ping 1.1.1.1 — ответ (ICMP тоже канал)"; LEAK=1
    else
        grn "  закрыто         ping 1.1.1.1"
    fi
fi

echo "  -- изнутри контейнера kb (так выходят все контейнеры стека) --"
if docker compose ps --status running kb 2>/dev/null | grep -q kb; then
    report kb < <(docker compose exec -T -e PYTHONIOENCODING=utf-8 kb python -c "$PROBE")
else
    yel "  kb не запущен — пропускаю"; WARN=1
fi
else
    echo "  пропущено: --quiet, наружу ничего не отправляется"
fi

echo
echo "=== 4. Кто сейчас соединён с внешними адресами ==="
# 255.255.255.255 и 224-239.x (multicast), ff.. в IPv6 — рассылка по своей
# сети (DHCP, обнаружение служб): маршрутизатор их наружу не пропускает
PRIVATE='^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|\[?::1|\[?f[cdef]|0\.0\.0\.0|255\.255\.255\.255|2(2[4-9]|3[0-9])\.|\*)'
snapshot() {
    ss -Htunp state established 2>/dev/null | awk '{print $5, $6}' \
        | sed 's/^::ffff://' | grep -vE "$PRIVATE" | sort -u
    if [ "$(id -u)" = 0 ] && command -v conntrack >/dev/null; then
        # Соединения контейнеров (их не видно в ss хоста — они в своих namespace)
        conntrack -L 2>/dev/null | grep -oE 'dst=[0-9.]+ sport=[0-9]+ dport=[0-9]+' \
            | awk '!seen[$1]++ {sub("dst=",""); print $1, "(через NAT, контейнер?)", $3}' \
            | grep -vE "$PRIVATE"
    fi
}
if command -v ss >/dev/null; then
    [ "$(id -u)" = 0 ] || yel "  без root не видно процессов и соединений контейнеров (sudo ./check-egress.sh)"
    SEEN=1
    NOW=$(snapshot)
    if [ "$WATCH" -gt 0 ] 2>/dev/null; then
        echo "  Слушаю $WATCH с (раз в 2 с; новые внешние соединения — сразу, ход — раз в 30 с)..."
        end=$((SECONDS + WATCH))
        next=$((SECONDS + 30))
        while [ $SECONDS -lt $end ]; do
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                printf '%s\n' "$NOW" | grep -qxF -- "$line" && continue
                red "    новое: $line"
                NOW=$(printf '%s\n%s' "$NOW" "$line")
            done <<< "$(snapshot)"
            if [ $SECONDS -ge $next ]; then
                echo "    ...осталось $((end - SECONDS)) с"
                next=$((SECONDS + 30))
            fi
            sleep 2
        done
        NOW=$(printf '%s\n' "$NOW" | sort -u)
    fi
    NOW=$(printf '%s\n' "$NOW" | sed '/^$/d')
    if [ -n "$NOW" ]; then
        red "  Есть соединения с внешними адресами (адрес:порт собеседника, процесс):"
        printf '%s\n' "$NOW" | sed 's/^/    /'
        echo "  Если это внутренние адреса вашей сети (не все сети контура — 10/172/192)"
        echo "  или входящие подключения пользователей — это не утечка."
        LEAK=1
    else
        grn "  Соединений с внешними адресами нет"
    fi
else
    yel "  нет ss — пропускаю"; WARN=1
fi

echo
echo "=== 5. Наш стек ==="
if docker compose ps --status running qdrant 2>/dev/null | grep -q qdrant; then
    if docker compose exec -T qdrant env 2>/dev/null | grep -q '^QDRANT__TELEMETRY_DISABLED=true'; then
        grn "  телеметрия Qdrant выключена"
    else
        red "  телеметрия Qdrant ВКЛЮЧЕНА — git pull && docker compose up -d qdrant"; LEAK=1
    fi
fi

echo
if [ $LEAK = 1 ]; then
    red "ИТОГ: найден путь наружу или соединение с внешним адресом (красные строки выше)."
    echo "Закрывать на брандмауэре контура: исходящие с сервера — только к внутренним адресам"
    echo "(модель, Confluence, Jira, Dojo, Nexus, DNS контура)."
    exit 1
fi
if [ $ACTIVE = 0 ]; then
    [ $SEEN = 1 ] && grn "Внешних соединений сейчас нет." || yel "Текущие соединения не проверены (нет ss)."
    yel "ИТОГ (без отправки): закрыт ли выход на"
    yel "брандмауэре контура, отсюда не видно — спросите сетевиков или запустите без --quiet."
elif [ $WARN = 1 ]; then
    yel "ИТОГ: пробы наружу не прошли, но есть замечания (жёлтые строки выше)."
else
    grn "ИТОГ: пробы наружу не прошли — прямого выхода нет."
fi
exit 0
