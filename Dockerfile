# MCP-сервер поиска по базе знаний (kb/).
# Держит инструмент kb_search, к которому подключается Continue.

FROM python:3.11-slim

WORKDIR /app

# Зависимости отдельным слоем: правки кода не тянут переустановку пакетов
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kb/ ./kb/

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
