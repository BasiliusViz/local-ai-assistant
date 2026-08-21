# Граф кода: тянет репозитории из git, строит граф через Graphify
# и отдаёт его по MCP.

FROM python:3.11-slim

# Зеркала для сетей без доступа к deb.debian.org и pypi.org. Пустые по
# умолчанию - тогда идём напрямую. Задаются в .env, compose прокидывает их
# сюда как build-args. APT_MIRROR - только хост (например mirror.yandex.ru):
# подменяется deb.debian.org в обоих источниках, включая -security
# Значение по умолчанию - зеркало Яндекса: deb.debian.org недоступен из России.
# Пустая строка вернёт обращение напрямую, любое другое зеркало - подставить сюда
ARG APT_MIRROR="mirror.yandex.ru"
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

# Debian trixie держит источники в формате deb822, а не в sources.list
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# [mcp] обязателен: без него HTTP-транспорт не поднимется (нужны
# mcp + starlette + uvicorn), а stdio нам не подходит - сервер в контейнере
RUN pip install --no-cache-dir \
    ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
    ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
    "graphifyy[mcp]"

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
