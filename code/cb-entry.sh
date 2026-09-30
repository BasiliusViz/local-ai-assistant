#!/bin/sh
# Запуск графа релиза CB18.5 (контейнер cb-graph): свой MCP-сервер
# graph_server.py поверх базы SQLite, а не Graphify. Граф релиза в память
# Graphify не влезает (120 репозиториев, 3.4 ГБ графов) — см. graph_store.py.
# Инструменты сразу с префиксом: cb_get_neighbors, cb_query_graph... Иначе
# имена совпали бы с общим графом code-graph, и Continue их путает.
#
# База строится НЕ при старте, а в ./update-cb.sh (docker compose exec
# cb-graph /app/sync.sh): релиз статичный и большой. Сервер сам замечает
# новую базу, перезапуск не нужен. Базы ещё нет — сервер всё равно живёт и
# отвечает понятной ошибкой.

GRAPH_DIR="${GRAPH_DIR:-/data/graph}"

exec python /app/graph_server.py --db "$GRAPH_DIR/graph.sqlite" --port "${MCP_PORT:-8013}" \
    --prefix cb_ --label "РЕЛИЗ CB18.5" --name cb-graph
