"""MCP-сервер поиска по внутренней базе знаний.

Запуск (streamable-http, порт 8010):
    .venv\\Scripts\\python.exe -m kb.server

Только чтение: писать в базу может индексатор, но не модель. Иначе
ассистент начнёт «запоминать» собственные галлюцинации в корпоративную вики.
"""

import logging
import os
import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from kb import code_retriever, config
from kb.embedder import EmbedError
from kb.retriever import SearchError, known_values, search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("kb")

HOST = os.getenv("KB_HOST", "0.0.0.0")
PORT = int(os.getenv("KB_PORT", "8010"))

mcp = MCPServer(
    "knowledge-base",
    instructions=(
        "Поиск по внутренней документации компании на русском языке. "
        "Используй для вопросов о внутренних сервисах, регламентах и процессах."
    ),
)


@mcp.tool(
    # readOnlyHint: инструмент только читает, ничего не меняет —
    # клиент может не спрашивать подтверждения на каждый вызов
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def kb_search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    source_filter: str | None = None,
    space_filter: str | None = None,
    response_format: str = "concise",
) -> dict:
    """Ищет ответ во внутренней базе знаний компании.

    В базе лежит внутренняя документация на русском языке: регламенты выкатки
    сервисов, требования безопасности, инструкции по онбордингу, описания
    процессов. Это закрытая информация, которой нет в интернете.

    Вызывай этот инструмент, когда вопрос касается того, как что-то устроено
    или делается ИМЕННО В ЭТОЙ КОМПАНИИ: внутренние сервисы, регламенты,
    процессы, доступы, требования, принятые практики. Если по первому ответу
    картина неполная — сделай ещё несколько узких запросов вместо одного
    широкого.

    НЕ вызывай для общих вопросов о программировании, публичных технологиях
    и всего, что не относится к внутренней кухне компании: там база бесполезна.

    НЕ ВЫЗЫВАЙ ДЛЯ ВОПРОСОВ ПРО ИСХОДНЫЙ КОД. «Где в коде...», «в каком файле
    реализовано», «куда править», «как написана эта функция» — это к
    инструменту code_search, он ищет по самому коду. Здесь лежат только
    тексты документации, кода в них нет.

    Формулируй query как обычный вопрос на естественном языке, а не набор
    ключевых слов: поиск семантический, ему нужен смысл, а не термины.

    Args:
        query: вопрос на естественном языке, например
            "как откатить деплой платёжного шлюза"
        top_k: сколько фрагментов вернуть, 1-20. По умолчанию 5
        source_filter: ограничить источником (например "local", "confluence").
            Без него ищет везде
        space_filter: ограничить пространством или разделом. Без него ищет везде
        response_format: "concise" (по умолчанию) — обрезанный текст и ссылка,
            экономит контекст. "detailed" — полный текст фрагмента, дата
            обновления, релевантность. Начинай с concise, переходи на detailed,
            только если краткого не хватило

    Returns:
        found: сколько фрагментов найдено, results: список фрагментов. У каждого
        фрагмента есть text (содержимое), document (имя документа) и location
        (путь или ссылка).

        ОБЯЗАТЕЛЬНО заверши ответ строкой "Источник: <document> (<location>)",
        перечислив все документы, которые использовал. Ответ по внутренней
        документации без ссылки бесполезен — его нельзя проверить.
    """
    detailed = response_format == "detailed"

    try:
        result = search(query, top_k=top_k, source=source_filter, space=space_filter)
    except SearchError as e:
        return {"error": str(e)}
    except EmbedError as e:
        return {"error": str(e)}

    if not result.hits:
        if result.dropped:
            # Нашлось, но всё ниже порога релевантности: вопрос не про
            # внутреннюю кухню компании. Прямо говорим модели не выдумывать.
            return {
                "found": 0,
                "results": [],
                "hint": (
                    "В базе знаний нет ничего по этому вопросу — похоже, он не "
                    "про внутреннюю документацию компании. Отвечай своими "
                    "знаниями и не ссылайся на базу."
                ),
            }
        hint = "В базе ничего похожего нет."
        if source_filter or space_filter:
            hint += " Попробуй без фильтров: возможно, документ в другом разделе."
        else:
            hint += " Попробуй переформулировать вопрос другими словами."
        return {"found": 0, "results": [], "hint": hint}

    # Готовая строка цитирования на верхнем уровне: если оставить пути только
    # внутри фрагментов, модель ленится их собирать и теряет ссылку.
    # Так ей остаётся скопировать готовое.
    seen: dict[str, str] = {}
    for h in result.hits:
        # Прямые слеши: обратные при передаче через JSON удваиваются
        # и в ответе модели вылезает C:\\Users\\... Для http-ссылок
        # (Confluence) замена ничего не меняет
        seen.setdefault(h.source_id, h.url.replace("\\", "/"))
    citation = "Источник: " + "; ".join(f"{doc} ({loc})" for doc, loc in seen.items())

    return {
        "found": len(result.hits),
        "results": [h.as_dict(detailed=detailed) for h in result.hits],
        "citation": citation,
        "citation_instruction": (
            "Заверши ответ строкой источников. Возьми её из поля citation, но "
            "оставь только те документы, из которых действительно взял ответ — "
            "лишние вычеркни. Путь в скобках сохраняй целиком, не сокращай до "
            "имени файла. Это обычная строка текста: НЕ оборачивай её в блок "
            "кода, не ставь обратные кавычки и не помечай как код."
        ),
    }


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def code_search(
    query: str,
    top_k: int = 5,
    repo_filter: str | None = None,
    response_format: str = "concise",
) -> dict:
    """ПОИСК ПО ИСХОДНОМУ КОДУ: где что реализовано и куда править.

    ЭТОТ инструмент, а не kb_search, отвечает на любые вопросы со словами
    «в коде», «в каком файле», «куда править», «как реализовано», «есть ли
    функция». kb_search ищет по текстам документации, кода там нет.

    Отвечает на вопросы «где у нас сделано X», «есть ли уже функция для Y»,
    «в каком файле менять, чтобы добавить Z». Возвращает файл, номер строки и
    сам фрагмент кода — то есть конкретное место, откуда начинать работу.

    Ищет ПО СМЫСЛУ, а не по имени: описывай задачу словами, как объяснил бы
    коллеге («ограничение частоты запросов к API», «повторные попытки при
    сетевых ошибках»), а не пытайся угадать имя функции.

    Вызывай, когда вопрос про исходный код: где реализовано, где поправить,
    как что-то сделано в нашем коде.

    НЕ вызывай для вопросов про регламенты и процессы — для них есть
    kb_search. И не вызывай для общих вопросов о языке или библиотеках.

    Если нужно узнать, КТО ВЫЗЫВАЕТ найденную функцию и что сломается при
    изменении — после этого инструмента используй инструменты графа кода
    (get_neighbors, query_graph).

    Args:
        query: описание того, что ищешь, обычными словами
        top_k: сколько фрагментов вернуть, 1-20. По умолчанию 5
        repo_filter: ограничить одним репозиторием. Без него ищет по всем
        response_format: "concise" (по умолчанию) — фрагмент кода обрезан,
            "detailed" — код целиком

    Returns:
        found и results: для каждого найденного — location вида
        репозиторий/файл.py:42, символ, сигнатура и код.
        В ответе ОБЯЗАТЕЛЬНО указывай location: без него человек не найдёт
        место в коде.
    """
    if not code_retriever.available():
        return {
            "error": (
                "Индекс кода не создан. Он строится командой "
                "python -m kb.code_index <каталог с репозиториями>"
            )
        }

    if repo_filter:
        known = code_retriever.repos()
        if known and repo_filter not in known:
            return {
                "error": (
                    f"Репозитория '{repo_filter}' в индексе нет. "
                    f"Доступные: {', '.join(known)}."
                )
            }

    try:
        hits = code_retriever.search(query, top_k=top_k, repo=repo_filter)
    except Exception as e:
        return {"error": f"Поиск по коду не удался: {e}"}

    if not hits:
        return {
            "found": 0,
            "results": [],
            "hint": (
                "В индексе кода ничего похожего нет. Возможно, этот репозиторий "
                "не проиндексирован, либо стоит описать задачу другими словами."
            ),
        }

    detailed = response_format == "detailed"
    return {
        "found": len(hits),
        "results": [h.as_dict(full_code=detailed) for h in hits],
        "citation_instruction": (
            "Для каждого фрагмента указывай его location (файл и строку) — "
            "это ответ на вопрос «куда идти править»."
        ),
    }


def main() -> None:
    sources = known_values("source") or ["(база пуста)"]
    log.info("База знаний: коллекция %s, источники: %s", config.COLLECTION, sources)
    log.info("MCP streamable-http на http://%s:%s/mcp", HOST, PORT)
    # stateless_http: без него сервер выдаёт session-id и требует его в каждом
    # запросе, а Continue его не сохраняет — отсюда "Session not found" после
    # любого перезапуска сервера. Поиск и так не имеет состояния между вызовами.
    # json_response: обычный JSON вместо потока SSE, клиенты переваривают надёжнее.
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
