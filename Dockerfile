# MCP-сервер поиска по базе знаний (kb/).
# Держит инструмент kb_search, к которому подключается Continue.

FROM python:3.11-slim

WORKDIR /app

# Индекс PyPI. Пустое значение — идём на pypi.org напрямую. Адрес внутреннего
# зеркала задаётся в .env, оттуда его прокидывает docker-compose.yml: в
# репозитории ему не место, у каждой площадки он свой.
# Хвост /simple обязателен: без него pip получит 404
ARG PIP_INDEX_URL=""
# Заполнять, только если у зеркала сертификат внутреннего УЦ: тогда pip
# перестанет проверять TLS для этого хоста
ARG PIP_TRUSTED_HOST=""

# Зависимости отдельным слоем: правки кода не тянут переустановку пакетов
COPY requirements.txt .
RUN pip install --no-cache-dir \
    ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
    ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
    -r requirements.txt

COPY kb/ ./kb/
# Выгрузка Confluence живёт здесь же: на сервере не нужен Python, всё
# запускается через docker compose exec kb
COPY confluence/ ./confluence/

# Внутри контейнера localhost - это сам контейнер, а не хост.
# Соседние сервисы доступны по именам из compose.
ENV KB_OLLAMA_URL=http://ollama:11434/v1 \
    KB_QDRANT_URL=http://qdrant:6333 \
    KB_RERANKER_URL=http://reranker:80 \
    KB_HOST=0.0.0.0 \
    KB_PORT=8010 \
    PYTHONUNBUFFERED=1

EXPOSE 8010

CMD ["python", "-m", "kb.server"]
