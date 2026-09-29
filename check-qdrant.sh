#!/usr/bin/env bash
# Диагностика Qdrant: упал ли, почему, хватает ли памяти и диска, в каком
# состоянии коллекции. Только чтение — ничего не меняет.
#
#     ./check-qdrant.sh
#     ./check-qdrant.sh > ~/qdrant-check.txt 2>&1     сохранить, чтобы переслать
#     ./check-qdrant.sh 12                           журнал за 12 часов (по умолчанию 6)
#
# Повод: индексация релиза CB18.5 обрывалась на upsert с «Server
# disconnected without sending a response» — это Qdrant, чаще всего нехватка
# памяти. Разделы:
#   1. контейнер    убит ли по памяти (OOMKilled), сколько раз перезапускался
#   2. память/диск  сервер целиком и сам Qdrant
#   3. Qdrant       отвечает ли, версия, коллекции: точки, статус, где векторы
#   4. журнал       ошибки Qdrant за последние часы
#   5. ядро         убивала ли система процессы из-за памяти (нужен sudo)
#   6. кто пишет    идёт ли сейчас индексация

set -u
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env 2>/dev/null; set +a; }

QDRANT="http://localhost:${QDRANT_PORT:-6333}"
HOURS="${1:-6}"

section() { echo; echo "=== $* ==="; }

section "1. Контейнер qdrant"
if ! docker inspect qdrant >/dev/null 2>&1; then
    echo "  контейнера qdrant нет"
else
    docker inspect qdrant --format '  статус: {{.State.Status}}, запущен (UTC): {{.State.StartedAt}}
  убит по памяти (OOMKilled): {{.State.OOMKilled}}, перезапусков: {{.RestartCount}}, код выхода: {{.State.ExitCode}}
  лимит памяти контейнера: {{if .HostConfig.Memory}}{{.HostConfig.Memory}} байт{{else}}нет (вся память сервера){{end}}'
    echo "  сейчас (UTC): $(date -u +%Y-%m-%dT%H:%M:%S)"
fi

section "2. Память и диск"
free -h | sed 's/^/  /'
echo
docker stats --no-stream --format '  {{.Name}}: память {{.MemUsage}} ({{.MemPerc}}), процессор {{.CPUPerc}}' \
    qdrant kb cb-graph code-graph 2>/dev/null
echo
DIR="${QDRANT_DIR:-./data/qdrant}"
df -h "$DIR" 2>/dev/null | sed 's/^/  /'
echo "  занято базой Qdrant: $(du -sh "$DIR" 2>/dev/null | cut -f1) ($DIR)"

section "3. Qdrant изнутри"
if ! curl -s -m 5 "$QDRANT/readyz" >/dev/null; then
    echo "  НЕ ОТВЕЧАЕТ на $QDRANT — упал или ещё загружает коллекции (см. раздел 4)"
else
    curl -s -m 10 "$QDRANT/" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  версия:", d.get("version","?"))' 2>/dev/null
    for c in $(curl -s -m 10 "$QDRANT/collections" | python3 -c 'import sys,json; print(" ".join(c["name"] for c in json.load(sys.stdin)["result"]["collections"]))' 2>/dev/null); do
        curl -s -m 30 "$QDRANT/collections/$c" | python3 -c '
import sys, json
name = sys.argv[1]
r = json.load(sys.stdin)["result"]
cfg = r["config"]["params"]["vectors"]
vec = next(iter(cfg.values())) if isinstance(cfg, dict) and "size" not in cfg else cfg
q = r["config"].get("quantization_config") or (vec.get("quantization_config") if isinstance(vec, dict) else None)
points, status, segs = r.get("points_count"), r.get("status"), r.get("segments_count")
opt = r.get("optimizer_status")
on_disk = vec.get("on_disk", False) if isinstance(vec, dict) else "?"
ram = bool(q) and bool((q.get("scalar") or {}).get("always_ram"))
squeeze = ("да, копия в памяти" if ram else "да") if q else "нет"
print("  %s: точек %s, статус %s, сегментов %s, оптимизатор %s" % (name, points, status, segs, opt))
print("      векторы на диске: %s, сжатие: %s" % (on_disk, squeeze))
' "$c" 2>/dev/null || echo "  $c: не прочитать"
    done
fi

section "4. Журнал Qdrant за $HOURS ч (ошибки и загрузка)"
docker logs --since "${HOURS}h" qdrant 2>&1 \
    | grep -i -E "error|panic|warn|kill|memory|oom|loading|recover|starting|shutdown|signal" \
    | grep -v -i "actix_web::middleware::logger" | tail -40 | sed 's/^/  /'
echo "  (последние строки журнала целиком:)"
docker logs --tail 8 qdrant 2>&1 | sed 's/^/  | /'

section "5. Убийства из-за памяти (ядро)"
if sudo -n true 2>/dev/null; then
    sudo dmesg -T 2>/dev/null | grep -i -E "out of memory|oom-kill|killed process" | tail -8 | sed 's/^/  /'
    echo "  (выше пусто — ядро никого не убивало)"
else
    echo "  нужен sudo — выполнить вручную:"
    echo "  sudo dmesg -T | grep -i -E 'out of memory|oom-kill|killed process' | tail -8"
fi

section "6. Кто сейчас пишет в базу"
pgrep -af "update-cb|code_index|doc_index|jira_index|dojo_index|cron.sh" | grep -v pgrep | sed 's/^/  /'
echo "  (выше пусто — никто не пишет)"
