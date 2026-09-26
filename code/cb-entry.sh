#!/bin/sh
# Запуск графа релиза CB18.5 (контейнер cb-graph): тот же Graphify, что у
# code-graph, но за прослойкой mcp_prefix.py, которая отдаёт его инструменты
# как cb_get_neighbors, cb_query_graph... Иначе имена совпали бы с общим
# графом, и Continue их путает.
#
#   Graphify  127.0.0.1:8011 (только внутри контейнера)
#   прослойка 0.0.0.0:8013   (наружу, к ней подключается Continue)
#
# В отличие от code-graph граф при старте НЕ строится: релиз статичный и
# большой, пересборка после каждой перезагрузки сервера заняла бы долго и
# столкнулась бы с ./update-cb.sh, который строит граф сам (docker compose
# exec). Графа ещё нет — прослойка всё равно живёт и отвечает понятной ошибкой.

GRAPH_DIR="${GRAPH_DIR:-/data/graph}"

if [ -f "$GRAPH_DIR/graph.json" ]; then
    python -m graphify.serve --graph "$GRAPH_DIR/graph.json" --transport http \
        --host 127.0.0.1 --port 8011 --stateless --json-response &
else
    echo "[!] $GRAPH_DIR/graph.json нет - граф релиза не поднят, запустите ./update-cb.sh"
fi

exec python /app/mcp_prefix.py --port "${MCP_PORT:-8013}" \
    --upstream http://127.0.0.1:8011/mcp --prefix cb_ --label "РЕЛИЗ CB18.5" --name cb-graph
