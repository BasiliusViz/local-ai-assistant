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

from kb import dojo, dojo_retriever, release_notes
from kb import dojo_compare as compare_mod
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


RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

# Что модель кладёт в необязательное поле, когда фильтра нет. Замер показал:
# qwen3 передаёт "null" строкой, и уровень «null» давал ошибку вместо ответа,
# а query "null" ушёл бы в смысловой поиск
BLANK = {"", "null", "none", "nil", "undefined", "*", "all", "any", "все", "всё", "любой", "любые"}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return None if value.strip().casefold() in BLANK else value.strip()

# Описания короткие намеренно. Раньше всё было одним инструментом с восемью
# полями и полутора экранами оговорок, и qwen3.6:35b находила его, заполняла
# знакомые поля (продукт, статус) — и не догадывалась про engagement. Одна
# задача — один инструмент с понятным именем: выбрать dojo_compare на
# «сравни» модели проще, чем вспомнить комбинацию полей.


@mcp.tool(annotations=RO)
def dojo_findings(
    product: str = "",
    severity: str | None = None,
    status: str = "open",
    query: str | None = None,
    limit: int = 25,
    response_format: str = "concise",
) -> dict:
    """Находки DefectDojo по продукту или по всем: что нашли сканеры, сколько
    каких уровней. Только при слове «dojo»/«defectdojo» в сообщении.

    НЕ для engagement'ов: список — dojo_engagements, сравнение двух —
    dojo_compare, release notes — dojo_release_notes.

    product — только если назван в вопросе; не назван — пусто, ответ по всем
    продуктам (таблица by_product). severity и status — отдельные поля, в
    query только тема («инъекции», «log4j»), если она есть в вопросе.
    Примеры: «dojo критичные в abinf» -> product="abinf", severity="критичные";
    «dojo общая картина» -> без аргументов; «dojo где у нас log4j» -> query="log4j";
    «dojo подготовь документ по abinf» -> product="abinf", response_format="report".

    Данные — индекс, обновляется раз в два часа. Начинай ответ со сводки по
    уровням, у каждой находки давай ссылку.

    Args:
        product: продукт, можно неполно: "abinf". Пусто — все продукты
        severity: критичный, высокий, средний, низкий, информационный
        status: "open" (по умолчанию), "принятые", "ложные", "закрытые", "all"
        query: тема, только если она есть в вопросе
        limit: сколько находок показать
        response_format: "concise", "detailed" или "report" (для документа:
            описание, чем грозит, как исправить — бери текст из этих полей)
    """
    severity, query = _clean(severity), _clean(query)
    # У статуса «all»/«все» — осмысленное значение (все состояния), его не
    # чистим; пустышки — это «по умолчанию», то есть открытые
    if not status or status.strip().casefold() in {"", "null", "none", "nil", "undefined"}:
        status = "open"
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
    if result.get("note"):
        # Продукт есть в DefectDojo, но не в индексе — не «продукта нет»
        out["note"] = result["note"]
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
    if result.get("note"):
        out["citation_instruction"] = (
            "Перескажи поле note: продукт СУЩЕСТВУЕТ, в индексе по нему нет "
            "находок индексируемых уровней. Не говори, что продукта нет. Если "
            "спрашивали про engagement, сравнение или release notes — вызови "
            "dojo_engagements, dojo_compare или dojo_release_notes."
        )
    indexed = result.get("indexed_levels") or []
    if len(indexed) < len(dojo.SEVERITIES):
        out["indexed_levels"] = indexed
        out["citation_instruction"] += (
            f" В индексе только уровни {', '.join(indexed)}: скажи об этом, "
            "остальных уровней в сводке нет не потому, что их ноль."
        )
    return out


def _live(product: str) -> dict | None:
    """Ошибка, если живой запрос к DefectDojo сделать нельзя, иначе None."""
    if not dojo.configured():
        return {"error": "У сервера не заданы DOJO_URL и DOJO_TOKEN."}
    if dojo_retriever.wants_all(product):
        return {
            "error": "Нужен продукт: engagement'ы принадлежат продукту. Спроси, "
            "какой продукт имеется в виду."
        }
    return None


@mcp.tool(annotations=RO)
def dojo_engagements(product: str) -> dict:
    """Список engagement'ов продукта в DefectDojo: названия, статус, дата.
    Только при слове «dojo»/«defectdojo». Engagement у нас — ветка или релиз,
    название вида «ветка_продукт». Зови, чтобы узнать точные названия перед
    сравнением. Пример: «dojo какие engagement есть в abinf» -> product="abinf".

    Args:
        product: продукт, можно неполно: "abinf"
    """
    if err := _live(product):
        return err
    try:
        return compare_mod.list_engagements(product)
    except dojo.DojoError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Запрос к DefectDojo не удался: {e}"}


@mcp.tool(annotations=RO)
def dojo_compare(
    product: str, base: str, target: str, severity: str | None = None, limit: int = 25
) -> dict:
    """Сравнить два engagement'а продукта в DefectDojo: что появилось, что
    устранено, что осталось. Только при слове «dojo»/«defectdojo».
    base — с чем сравниваем (прошлый релиз, основная ветка), target — что
    проверяем (новый релиз, ветка). Названия можно коротко: «main», «release-1.2».
    Сюда же «чем X отличается от Y», «что появилось в X», «что изменилось».
    Пример: «dojo сравни main и feature-x в abinf» -> product="abinf",
    base="main", target="feature-x".

    Ответ — три группы, отвечай в этом порядке: new_in_target (появилось),
    fixed_in_target (было в base, в target не открыто — устранено),
    still_open (открыто в обоих). У каждой сводка по уровням и ссылки.

    Args:
        product: продукт, можно неполно
        base: прошлый engagement
        target: новый engagement
        severity: ТОЛЬКО если уровни названы в вопросе, например
            "критичные и высокие". Не названы — не заполняй
        limit: сколько находок показать в каждой группе
    """
    if err := _live(product):
        return err
    severity = _clean(severity)
    try:
        levels = release_notes.parse_levels(severity)
        result = compare_mod.compare(product, base, target, "open", None, limit=100000)
    except dojo.DojoError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Запрос к DefectDojo не удался: {e}"}

    groups = {
        "new_in_target": result["only_in_second"],
        "fixed_in_target": result["only_in_first"],
        "still_open": result["in_both"],
    }
    out = {"product": result["product"], "base": result["first"], "target": result["second"]}
    for key, group in groups.items():
        group = release_notes.only(group, levels)
        out[key] = {**group, "findings": group["findings"][:limit]}
    if levels:
        out["severity"] = levels
    out["citation_instruction"] = (
        "Ответь по трём группам в этом порядке: new_in_target — что появилось, "
        "fixed_in_target — что устранено, still_open — что осталось. Для каждой "
        "сводка по уровням и находки со ссылками. Данные живые, из DefectDojo."
    )
    return out


@mcp.tool(annotations=RO)
def dojo_release_notes(
    product: str, base: str, target: str, severity: str | None = None
) -> dict:
    """Release notes по устранённым уязвимостям между двумя engagement'ами
    (релизами) в DefectDojo — готовый markdown-документ. Только при слове
    «dojo»/«defectdojo». base — прошлый релиз, target — новый.
    ТОЛЬКО когда просят именно release notes или документ для релиза. На
    «сравни», «чем отличается», «что появилось» — dojo_compare, даже если
    engagement'ы называются release-*.
    Пример: «dojo release notes между release-1.1 и release-1.2 в abinf»
    -> product="abinf", base="release-1.1", target="release-1.2".
    Выведи поле release_notes ДОСЛОВНО.

    Args:
        product: продукт, можно неполно
        base: прошлый релиз
        target: новый релиз
        severity: ТОЛЬКО если уровни названы в вопросе, например
            "критичные и высокие". Не названы — не заполняй: в release notes
            должно попасть всё устранённое
    """
    if err := _live(product):
        return err
    severity = _clean(severity)
    try:
        text, _ = release_notes.build(
            product, base, target, severity, max_items=CHAT_NOTES_ITEMS
        )
    except dojo.DojoError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Запрос к DefectDojo не удался: {e}"}
    return {
        "release_notes": text,
        "citation_instruction": (
            "Выведи поле release_notes ДОСЛОВНО, markdown как есть: это документ "
            "для релиза, собранный из данных DefectDojo. Ничего не пересказывай, "
            "не добавляй и не убирай."
        ),
    }


# Сколько находок показывать в release notes в чате: длинный список съел бы
# контекст модели. Полный документ — командой python -m kb.release_notes
CHAT_NOTES_ITEMS = 100


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
