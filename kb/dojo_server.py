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

from kb import dojo, dojo_compare, dojo_retriever
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
    product: str = "",
    status: str = "open",
    severity: str | None = None,
    query: str | None = None,
    limit: int = 25,
    response_format: str = "concise",
    engagement: str = "",
    compare_with: str = "",
) -> dict:
    """УЯЗВИМОСТИ ПРОДУКТОВ из DefectDojo: что нашли сканеры и что не закрыто.

    ВЫЗЫВАЙ ТОЛЬКО ЕСЛИ В СООБЩЕНИИ ЕСТЬ СЛОВО «defectdojo» ИЛИ «dojo»
    («дефектдоджо», «додж»). Вопрос про уязвимости сам по себе не повод: про
    требования и регламенты безопасности отвечает kb_search, а тут лежат
    находки сканеров по конкретным продуктам.

    Продукт, состояние и уровень — ЭТО ФИЛЬТРЫ, отдельные аргументы. В query
    кладут только тему («инъекции», «устаревшие зависимости»), и только если
    она в вопросе есть. «Критичные по abinf» темы не содержит — query не нужен.

    ПРОДУКТ — ТОЛЬКО ЕСЛИ ОН НАЗВАН В ВОПРОСЕ. Не назван — оставь product
    пустым: поиск пойдёт по всем продуктам сразу и вернёт таблицу by_product
    (сколько находок каждого уровня у каждого продукта). Не придумывай
    продукт и не бери его из прошлых сообщений, если спрашивают «по всем»,
    «вообще», «у нас».

    Примеры:
      «dojo что по продукту abinf»        -> product="abinf"
      «dojo критичные по abinf»           -> product="abinf", severity="критичные"
      «dojo что приняли как риск в abinf» -> product="abinf", status="принятые"
      «dojo что по инъекциям в abinf»     -> product="abinf", query="инъекции"
      «dojo подготовь документ по abinf»  -> product="abinf",
                                             response_format="report"
      «dojo общая картина»                -> без аргументов
      «dojo какие продукты есть»          -> без аргументов, ответ — by_product
      «dojo где у нас критичные»          -> severity="критичные"
      «dojo где у нас log4j»              -> query="log4j"

    ВЕТКИ (engagement). У продукта engagement'ы называются «ветка_продукт»
    (main_abinf, feature-x_abinf); ветку называй коротко, как в вопросе.
    Нужен product. Это живой запрос в DefectDojo, не индекс; query с ним не
    работает, status и severity — работают.
      «dojo какие ветки есть в abinf»           -> product="abinf", engagement="*"
      «dojo что в ветке feature-x в abinf»      -> product="abinf",
                                                   engagement="feature-x"
      «dojo сравни ветки main и feature-x в abinf»
                                                -> product="abinf", engagement="main",
                                                   compare_with="feature-x"
    Сравнение возвращает три группы: only_in_second (появилось во второй
    ветке), only_in_first (есть в первой, во второй нет — исправлено или не
    найдено), in_both. У каждой сводка по уровням. Отвечай по этим группам и
    в этом порядке: сначала новое — ради него обычно и сравнивают.

    Про режим "report". По нему пишется документ: сводка по уровням, затем
    по каждой находке — в чём проблема, чем грозит, что предлагает сканер,
    где в коде и ссылка на проверку. Бери текст ИЗ ПОЛЕЙ description,
    impact и mitigation, а не из своих знаний: там написано то, что нашёл
    конкретный сканер в конкретном месте. Ответ длинный, поэтому при
    просьбе сделать документ разумно сузить отбор — например только
    критичные и высокие.

    Сводка по уровням возвращается ВСЕГДА, даже если спросили про один: «три
    критичных» без общей картины вводит в заблуждение — непонятно, три из трёх
    это или три из сорока. Начинай ответ со сводки, потом сами находки.

    ДАННЫЕ — СНИМОК ИНДЕКСА, а не живой DefectDojo. Находки меняются каждый
    день, поэтому если речь о количестве открытых, добавляй, что это по данным
    последней выгрузки, и предлагай проверить в самом DefectDojo.

    Args:
        product: название продукта, можно неполно: "abinf". Пусто — все
            продукты
        status: "open" (по умолчанию), "принятые"/"accepted", "ложные",
            "закрытые"/"fixed" или "all" — все состояния
        severity: уровень — критичный, высокий, средний, низкий,
            информационный. Без него вернутся все
        query: тема, если она есть в вопросе. Ищет по описаниям находок и
            рекомендациям по устранению
        limit: сколько находок показать, по умолчанию 25
        response_format: "concise" (по умолчанию) — номер, заголовок, уровень,
            статус, ссылка. "detailed" — плюс сканер, CWE, дата и найденный
            фрагмент. "report" — всё для документа: описание проблемы, чем
            грозит и что предлагает сканер для исправления

    Returns:
        product, summary (счётчики по уровням), applied_filters и findings —
        список находок со ссылками. Без продукта ещё by_product — таблица по
        продуктам, и у каждой находки указан её продукт. В ответе ОБЯЗАТЕЛЬНО приводи ссылки: без
        них человеку некуда идти разбираться.
    """
    if engagement.strip() or compare_with.strip():
        return engagement_mode(product, engagement, compare_with, status, severity, limit)

    try:
        result = dojo_retriever.search(
            product=product,
            status=status,
            severity=severity,
            query=query,
            limit=limit,
            report=response_format == "report",
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
    report = response_format == "report"
    hits = result["hits"]
    everywhere = "by_product" in result
    out = {
        "product": result["product"],
        "summary": result["summary"],
        "applied_filters": result["applied_filters"],
    }
    if everywhere:
        out["by_product"] = result["by_product"]
    out["found"] = len(hits)
    out["findings"] = [
        h.as_dict(detailed=detailed, report=report, with_product=everywhere)
        for h in hits
    ]
    out["citation_instruction"] = (
        (
            "Начни с общей сводки по уровням, затем таблица by_product — "
            "продукт и число находок каждого уровня, — затем находки со "
            "ссылками, у каждой указан продукт. "
            if everywhere
            else "Начни со сводки по уровням, потом перечисли находки со ссылками. "
        )
        + "Данные из индекса, а не из живого DefectDojo: если речь о "
        "количестве открытых, оговори это и предложи свериться."
    )
    return out


def engagement_mode(
    product: str,
    engagement: str,
    compare_with: str,
    status: str | None,
    severity: str | None,
    limit: int,
) -> dict:
    """Ветки продукта: список, одна ветка или сравнение двух — живым запросом."""
    if not dojo.configured():
        return {
            "error": "Ветки смотрятся живым запросом в DefectDojo, а у сервера "
            "не заданы DOJO_URL и DOJO_TOKEN."
        }
    if dojo_retriever.wants_all(product):
        return {
            "error": "Для веток нужен продукт: engagement'ы принадлежат "
            "продукту. Спроси, какой продукт имеется в виду."
        }
    first, second = engagement.strip(), compare_with.strip()
    if not first:
        first, second = second, ""
    try:
        state = (
            dojo_retriever.normalize_status(status)
            if status and status != "all"
            else None
        )
        level = dojo.normalize_severity(severity) if severity else None
        if _norm(first) in dojo_compare.ALL:
            return dojo_compare.list_engagements(product)
        if second:
            result = dojo_compare.compare(product, first, second, state, level, limit)
            result["citation_instruction"] = (
                "Ответь по трём группам: сначала only_in_second — что появилось "
                "во второй ветке, затем only_in_first — что было в первой и во "
                "второй не найдено, затем in_both. Для каждой — сводка по "
                "уровням и находки со ссылками. Данные живые, из DefectDojo."
            )
            return result
        return dojo_compare.one(product, first, state, level, limit)
    except (dojo.DojoError, dojo_retriever.DojoSearchError) as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Запрос к DefectDojo не удался: {e}"}


def _norm(text: str) -> str:
    return text.strip().casefold()


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
