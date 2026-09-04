"""MCP-сервер уязвимостей DefectDojo.

Запуск (streamable-http, порт 8012):
    python -m kb.dojo_server

Отдельный сервер, а не инструмент рядом с поиском по документации. Причина
не в удобстве, а в доступе: аутентификации внутри нет, поэтому единственный
рычаг разграничения — порт. :8010 с документацией, кодом и задачами можно
открыть всей команде, а :8012 со списком уязвимостей — только AppSec и
сборочному агенту, правилом на брандмауэре.

Это же соответствует общей практике «один сервер — одна работа»: сервер
строится вокруг одной системы, а не собирает всё подряд. Отдельно советуют
делить по ролям пользователей — здесь ровно этот случай.

На выбор инструмента моделью разделение НЕ влияет: клиент всё равно
показывает ей все инструменты одним списком, независимо от того, с какого
они сервера. Разводить источники нужно данными и описаниями, и это уже
сделано — kb_search находки DefectDojo не видит.

Данные берутся из общего индекса в Qdrant (`kb/dojo_index.py`), поэтому
серверу нужны те же настройки Qdrant и эмбеддера, что и kb.

Только чтение: менять статусы находок из чата нельзя.
"""

import logging
import os
import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from kb import dojo, dojo_retriever
from kb.embedder import EmbedError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("dojo")

HOST = os.getenv("DOJO_HOST", "0.0.0.0")
PORT = int(os.getenv("DOJO_MCP_PORT", "8012"))

mcp = MCPServer(
    "defectdojo",
    instructions=(
        "Уязвимости продуктов из DefectDojo: что нашли сканеры, что открыто, "
        "что приняли как риск. Вызывается только при слове «dojo» или "
        "«defectdojo» в сообщении."
    ),
)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
def dojo_findings(
    product: str,
    status: str = "open",
    severity: str | None = None,
    query: str | None = None,
    limit: int = 25,
    response_format: str = "concise",
) -> dict:
    """УЯЗВИМОСТИ ПРОДУКТА из DefectDojo: что нашли сканеры и что не закрыто.

    ВЫЗЫВАЙ ТОЛЬКО ЕСЛИ В СООБЩЕНИИ ЕСТЬ СЛОВО «defectdojo» ИЛИ «dojo»
    («дефектдоджо», «додж»). Вопрос про уязвимости сам по себе не повод: про
    требования и регламенты безопасности отвечает kb_search, а тут лежат
    находки сканеров по конкретным продуктам.

    Продукт, состояние и уровень — ЭТО ФИЛЬТРЫ, отдельные аргументы. В query
    кладут только тему («инъекции», «устаревшие зависимости»), и только если
    она в вопросе есть. «Критичные по abinf» темы не содержит — query не нужен.

    Примеры:
      «dojo что по продукту abinf»        -> product="abinf"
      «dojo критичные по abinf»           -> product="abinf", severity="критичные"
      «dojo что приняли как риск в abinf» -> product="abinf", status="принятые"
      «dojo что по инъекциям в abinf»     -> product="abinf", query="инъекции"

    Сводка по уровням возвращается ВСЕГДА, даже если спросили про один: «три
    критичных» без общей картины вводит в заблуждение — непонятно, три из трёх
    это или три из сорока. Начинай ответ со сводки, потом сами находки.

    ДАННЫЕ — СНИМОК ИНДЕКСА, а не живой DefectDojo. Находки меняются каждый
    день, поэтому если речь о количестве открытых, добавляй, что это по данным
    последней выгрузки, и предлагай проверить в самом DefectDojo.

    Args:
        product: название продукта, можно неполно: "abinf"
        status: "open" (по умолчанию), "принятые"/"accepted", "ложные",
            "закрытые"/"fixed" или "all" — все состояния
        severity: уровень — критичный, высокий, средний, низкий,
            информационный. Без него вернутся все
        query: тема, если она есть в вопросе. Ищет по описаниям находок и
            рекомендациям по устранению
        limit: сколько находок показать, по умолчанию 25
        response_format: "concise" (по умолчанию) или "detailed" — со сканером,
            CWE, датой и текстом находки

    Returns:
        product, summary (счётчики по уровням), applied_filters и findings —
        список находок со ссылками. В ответе ОБЯЗАТЕЛЬНО приводи ссылки: без
        них человеку некуда идти разбираться.
    """
    try:
        result = dojo_retriever.search(
            product=product,
            status=status,
            severity=severity,
            query=query,
            limit=limit,
        )
    except dojo_retriever.DojoSearchError as e:
        return {"error": str(e)}
    except dojo.DojoError as e:
        return {"error": str(e)}
    except EmbedError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Поиск по находкам не удался: {e}"}

    detailed = response_format == "detailed"
    hits = result["hits"]
    return {
        "product": result["product"],
        "summary": result["summary"],
        "applied_filters": result["applied_filters"],
        "found": len(hits),
        "findings": [h.as_dict(detailed=detailed) for h in hits],
        "citation_instruction": (
            "Начни со сводки по уровням, потом перечисли находки со ссылками. "
            "Данные из индекса, а не из живого DefectDojo: если речь о "
            "количестве открытых, оговори это и предложи свериться."
        ),
    }


def main() -> None:
    if dojo_retriever.available():
        log.info("Находки DefectDojo: продукты %s", dojo_retriever.values("product"))
    else:
        log.warning(
            "Находки не проиндексированы — инструмент будет отвечать ошибкой. "
            "Выгрузка и индексация: python -m kb.dojo_index"
        )
    log.info("MCP streamable-http на http://%s:%s/mcp", HOST, PORT)
    # Настройки те же, что у kb: без stateless_http клиент получает session-id
    # и теряет его при перезапуске сервера, а json_response переваривается
    # клиентами надёжнее, чем поток SSE
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
