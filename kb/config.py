"""Настройки поиска по базе знаний. Всё переопределяется переменными окружения."""

import os
from pathlib import Path


def _load_env_file() -> None:
    """Подтянуть .env из корня проекта, если скрипт запущен вне контейнера.

    В контейнере переменные приходят от docker-compose, и файла там нет — эта
    функция просто ничего не найдёт. А при запуске руками с рабочей машины
    (kb/report.py, kb/selftest и прочие) читать настройки больше неоткуда.

    Уже заданные переменные окружения не перетираются: они важнее файла.
    """
    for folder in (Path(__file__).resolve().parent.parent, Path.cwd()):
        env_file = folder / ".env"
        if not env_file.exists():
            continue
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return


_load_env_file()



def _env(name: str, fallback: str, default: str) -> str:
    """Переменная с префиксом KB_, иначе та же без префикса, иначе умолчание.

    Внутри контейнера всё приходит как KB_*: их подставляет docker-compose,
    отделяя настройки поиска от настроек остального стека. А в .env те же
    значения лежат под именами без префикса (OLLAMA_URL, GEN_MODEL) — и когда
    скрипт запускают руками с рабочей машины, KB_* просто нет.

    Без этого запаса kb/report.py и подобные молча брали умолчание
    http://localhost:11434 и стучались в собственную машину вместо сервера
    с моделью.
    """
    value = os.getenv(name)
    if value is not None and value.strip():
        return value
    value = os.getenv(fallback)
    if value is not None and value.strip():
        return value
    return default


# Ollama. Обращаемся по OpenAI-совместимому /v1, а не /api/*:
# впереди переезд на vLLM, и так менять придётся только базовый URL.
OLLAMA_URL = _env("KB_OLLAMA_URL", "OLLAMA_URL", "http://localhost:11434/v1")
EMBED_MODEL = _env("KB_EMBED_MODEL", "EMBED_MODEL", "bge-m3")
EMBED_DIM = 1024

# Ключ доступа к Ollama. У самой Ollama аутентификации нет, но в проде она
# обычно стоит за прокси или за OpenAI-совместимым шлюзом (vLLM, LiteLLM),
# который ключ требует. Пусто - заголовок не отправляется вовсе.
OLLAMA_API_KEY = _env("KB_OLLAMA_API_KEY", "OLLAMA_API_KEY", "").strip()
# Разные шлюзы ждут ключ по-разному: Bearer - стандарт OpenAI-совместимых,
# x-api-key встречается у прокси-серверов
OLLAMA_AUTH_HEADER = _env("KB_OLLAMA_AUTH_HEADER", "OLLAMA_AUTH_HEADER", "Authorization").strip()
# Пустой префикс — осмысленное значение (для x-api-key), поэтому здесь
# нельзя использовать _env: он считает пустую строку отсутствием
OLLAMA_AUTH_PREFIX = os.getenv(
    "KB_OLLAMA_AUTH_PREFIX", os.getenv("OLLAMA_AUTH_PREFIX", "Bearer ")
)


def auth_headers() -> dict[str, str]:
    """Заголовки авторизации к Ollama. Пустой словарь, если ключа нет."""
    if not OLLAMA_API_KEY:
        return {}
    return {OLLAMA_AUTH_HEADER: f"{OLLAMA_AUTH_PREFIX}{OLLAMA_API_KEY}"}


# Какое API дёргать: OpenAI-совместимое (/v1/embeddings, /v1/chat/completions)
# или родное для Ollama (/api/embed, /api/chat). Шлюзы иногда пробрасывают
# только одно из двух. По умолчанию OpenAI-совместимое — задел на переезд
# с Ollama на vLLM, где родного API нет вовсе.
OLLAMA_API = _env("KB_OLLAMA_API", "OLLAMA_API", "openai").strip().lower()

# База без /v1 — нужна для родных путей
OLLAMA_BASE = OLLAMA_URL[:-3] if OLLAMA_URL.endswith("/v1") else OLLAMA_URL
OLLAMA_BASE = OLLAMA_BASE.rstrip("/")


def embeddings_url() -> str:
    if OLLAMA_API == "native":
        return f"{OLLAMA_BASE}/api/embed"
    return f"{OLLAMA_URL}/embeddings"


def chat_url() -> str:
    if OLLAMA_API == "native":
        return f"{OLLAMA_BASE}/api/chat"
    return f"{OLLAMA_URL}/chat/completions"

# Qdrant
QDRANT_URL = _env("KB_QDRANT_URL", "QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("KB_COLLECTION", "knowledge")
DENSE_VECTOR = "dense"

# Поиск. Сколько фрагментов отдаём модели по умолчанию и максимум.
# Фрагмент документации — до 1500 символов, то есть примерно 500 токенов:
# восемь штук это уже около 4000 токенов промпта. Больше поднимать опасно,
# пока окно контекста узкое — обрывы ответов начинаются именно отсюда.
DEFAULT_TOP_K = 8
MAX_TOP_K = 25

# Порог релевантности. Dense-поиск возвращает результат ВСЕГДА, даже на
# «рецепт борща» — без отсечки модель получит мусор и начнёт по нему отвечать.
# Замерено на testdocs (bge-m3, cosine): релевантные 0.57-0.65,
# посторонние 0.27-0.42. 0.48 — середина зазора.
# ПЕРЕПРОВЕРИТЬ после заливки реальных данных: на большом корпусе разделение
# всегда хуже, чем на трёх файлах.
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.48"))

# Реранкинг (kb/reranker.py). Векторный поиск достаёт CANDIDATES кандидатов
# без порога — на этом этапе важно не потерять верный ответ, его место в
# списке неважно. Дальше модель переупорядочивает и режет по RERANK_MIN.
# Выключен намеренно, включается переменной KB_RERANK=1.
# На testdocs (14 чанков) пользы не даёт: реранкер отбирает лучших из 20
# кандидатов, отобранных из тысяч, а тут кандидаты - вся база, и все они
# про одно и то же. Замер: 1 вопрос выиграл, 1 проиграл - это шум.
# Плюс на CPU он стоит ~9 секунд на запрос (600 мс на пару).
# Вернуться к нему на реальных данных, померив по эталонным вопросам.
# Расширение запроса (kb/expander.py): модель переформулирует вопрос в
# терминах документации, ищем по всем вариантам и сливаем по RRF.
# Лечит разрыв в словаре («отложить работу» -> stash), с которым не
# справляется ни один ранжировщик: нужный чанк просто не доезжает до него.
EXPAND = os.getenv("KB_EXPAND", "1") == "1"
EXPAND_MODEL = _env("KB_EXPAND_MODEL", "GEN_MODEL", "qwen3:8b")
EXPAND_VARIANTS = int(os.getenv("KB_EXPAND_VARIANTS", "2"))
EXPAND_TIMEOUT = 120.0
# Расширять не всегда, а только когда обычный поиск не уверен: переформулировка
# стоит 2-3 секунды на вызов модели. Если первый же поиск дал скор выше этого
# порога - ответ и так найден, второй заход не нужен.
# Замер на 15 вопросах: провалы дали 0.539 и 0.606, успехи от 0.558 до 0.738 -
# диапазоны ПЕРЕКРЫВАЮТСЯ, чистого порога не существует. Поэтому берём выше
# худшего провала: расширяем примерно половину вопросов, лёгкие проходят
# быстрым путём.
# ВНИМАНИЕ: подобрано на 15 вопросах, это мало. Перепроверить на большем наборе
# и на реальных данных - порог наверняка уедет.
EXPAND_IF_BELOW = float(os.getenv("KB_EXPAND_IF_BELOW", "0.62"))
# RRF: вес документа = сумма 1/(k + позиция) по всем спискам. Складываем
# ранги, а не скоры — скоры разных запросов несравнимы. k=60 - стандарт
RRF_K = 60

RERANK = os.getenv("KB_RERANK", "0") == "1"
RERANKER_URL = os.getenv("KB_RERANKER_URL", "http://localhost:8081")
CANDIDATES = int(os.getenv("KB_CANDIDATES", "20"))
# Сырые оценки cross-encoder'а: релевантное уходит в плюс, нерелевантное
# в глубокий минус (замер: +5..+7 против -10..-11). Отсюда порог по нулю
RERANK_MIN = float(os.getenv("KB_RERANK_MIN", "0"))
RERANK_TIMEOUT = 120.0

# Чанк отдаётся целиком в обоих режимах. Обрезка в concise была ошибкой:
# экономила копейки контекста, а модель копировала обрубок в ответ.
# Режимы различаются только полнотой метаданных.

HTTP_TIMEOUT = 60.0
