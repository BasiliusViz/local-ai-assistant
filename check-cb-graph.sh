#!/usr/bin/env bash
# Полная проверка графа релиза CB18.5 одной командой: отчёт по базе
# (code/graph_report.py внутри cb-graph) и общая сверка релиза (check-cb.py).
# Только чтение. Вывод дублируется в cb-graph-report.log — его и присылать.
#
#     ./check-cb-graph.sh
#
# Разбор крупнейших репозиториев проходит по всем их связям — несколько минут.

cd "$(dirname "$0")" || exit 1
LOG=cb-graph-report.log

{
    echo "Проверка графа релиза, $(date '+%Y-%m-%d %H:%M')"
    if ! docker compose exec -T cb-graph test -f /app/graph_report.py; then
        echo "[!!] в контейнере cb-graph нет graph_report.py: git pull, затем"
        echo "     docker compose up -d --build cb-graph"
        exit 1
    fi
    docker compose exec -T cb-graph python /app/graph_report.py
    echo
    echo "=== 6. Сверка релиза (check-cb.py) ==="
    python3 check-cb.py
} 2>&1 | tee "$LOG"

echo
echo "Сохранено в $LOG"
