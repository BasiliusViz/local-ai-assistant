# Граф кода: тянет репозитории из git, строит граф через Graphify
# и отдаёт его по MCP.

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# [mcp] обязателен: без него HTTP-транспорт не поднимется (нужны
# mcp + starlette + uvicorn), а stdio нам не подходит - сервер в контейнере
RUN pip install --no-cache-dir "graphifyy[mcp]"

WORKDIR /app
COPY sync.sh /app/sync.sh
RUN chmod +x /app/sync.sh

# Репозитории и граф лежат в примонтированном томе, а не в образе:
# пересборка образа не должна означать повторное клонирование
ENV REPOS_DIR=/data/repos \
    GRAPH_DIR=/data/graph \
    MCP_PORT=8011 \
    PYTHONUNBUFFERED=1

EXPOSE 8011

# Граф строится при старте, дальше обновляется по расписанию снаружи:
#   docker compose exec code-graph /app/sync.sh
CMD ["/bin/sh", "-c", "/app/sync.sh && exec python -m graphify.serve --graph $GRAPH_DIR/graph.json --transport http --host 0.0.0.0 --port $MCP_PORT --stateless --json-response"]
