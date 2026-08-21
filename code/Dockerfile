# Граф кода: тянет репозитории из git, строит граф через Graphify
# и отдаёт его по MCP.

FROM python:3.11-slim

# Откуда брать пакеты Debian: deb.debian.org снаружи недоступен, идём через
# внутренний Nexus. Адреса задаются ЦЕЛИКОМ, а не одним хостом — у Nexus путь
# вида /repository/<имя>, одной подменой хоста такой адрес не собрать.
#
# Источников в образе ДВА, и security — обязательный: если перенаправить
# только основной, apt-get update всё равно полезет наружу за -security и
# упадёт. В Nexus это РАЗНЫЕ репозитории, а не один с двумя путями.
#
# Проверено ./check-nexus.sh: trixie отдаёт apt-docker-debian (200),
# trixie-security — debian-security (200). Основной взаимозаменяем с
# debian-repo, тот тоже отвечает 200
ARG APT_URI="https://nexus.rnd.aip.ooo/repository/apt-docker-debian"
ARG APT_SECURITY_URI="https://nexus.rnd.aip.ooo/repository/debian-security"

# В базовом образе у основного источника прописано "trixie trixie-updates", но
# зеркала часто проксируют только сам trixie, и тогда apt-get update падает:
# "trixie-updates Release does not have a Release file". Отсюда список suites
# отдельным аргументом — лишний можно убрать, не трогая остальное.
# На пакеты это не влияет: trixie-updates добирает точечные обновления между
# минорными выпусками, база целиком лежит в trixie
ARG APT_SUITES="trixie"
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

# Debian trixie держит источники в формате deb822, а не в sources.list.
# Порядок замен важен: -security идёт первой, иначе более короткий шаблон
# съест её префикс и security-адрес окажется битым
RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|$APT_SECURITY_URI|g" \
        -e "s|http://deb.debian.org/debian|$APT_URI|g" \
        -e "s|^Suites: trixie trixie-updates$|Suites: $APT_SUITES|" \
        /etc/apt/sources.list.d/debian.sources \
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
