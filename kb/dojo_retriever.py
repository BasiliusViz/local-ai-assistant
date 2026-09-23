"""Поиск по находкам DefectDojo в коллекции knowledge.

Устроен как поиск по задачам (`kb/jira_retriever.py`) и по тем же причинам:
вопросы к уязвимостям начинаются с фильтра, а не со смысла. «Что открыто по
продукту», «сколько критичных» — это отбор по полям, которые в тексте находки
не написаны. Поэтому фильтр первым, вектор — только если в вопросе есть
смысловая часть.

Одно отличие от задач: сводка по уровням возвращается ВСЕГДА. На вопрос про
уязвимости ответ «вот три критичных» без общей картины вводит в заблуждение —
непонятно, три из трёх это или три из сорока.
"""

import logging
from dataclasses import dataclass

from qdrant_client import models

from kb import config, dojo
from kb.embedder import embed_batch
from kb.retriever import client, qdrant_alive

log = logging.getLogger(__name__)

SOURCE = "dojo"

# Как спрашивают про состояние находки. Значение — то, что лежит в payload
STATUS_RU = {
    "открыт": "open",
    "open": "open",
    "актив": "open",
    "принят": "accepted",
    "accepted": "accepted",
    "риск": "accepted",
    "ложн": "false_positive",
    "false": "false_positive",
    "закрыт": "fixed",
    "испарв": "fixed",
    "fixed": "fixed",
    "почин": "fixed",
}


class DojoSearchError(RuntimeError):
    """Ошибка, которую можно показать модели как есть."""


@dataclass
class Hit:
    finding_id: str
    title: str
    severity: str
    status: str
    product: str
    scanner: str
    cwe: str
    component: str
    location: str
    url: str
    found_at: str
    description: str = ""
    mitigation: str = ""
    impact: str = ""
    snippet: str = ""
    score: float = 0.0
    chunk_idx: int = 0

    def as_dict(
        self, detailed: bool = False, report: bool = False, with_product: bool = False
    ) -> dict:
        out = {
            "id": self.finding_id,
            "title": self.title,
            "severity": self.severity,
            "status": self.status,
            "url": self.url,
        }
        # По всем продуктам находка без имени продукта бесполезна: непонятно,
        # чья она и кому идти чинить
        if with_product:
            out["product"] = self.product
        if self.component:
            out["component"] = self.component
        if self.location:
            out["location"] = self.location
        if report:
            # Всё, из чего пишется документ: в чём проблема, чем грозит и что
            # предлагает сканер. Без этих трёх полей в отчёте графа «как
            # чинить» заполнялась бы общими знаниями модели, а не тем, что
            # реально написано в находке
            out.update(
                {
                    "product": self.product,
                    "scanner": self.scanner,
                    "cwe": self.cwe,
                    "found": self.found_at[:10],
                    "description": self.description,
                    "impact": self.impact,
                    "mitigation": self.mitigation,
                }
            )
        elif detailed:
            out.update(
                {
                    "product": self.product,
                    "scanner": self.scanner,
                    "cwe": self.cwe,
                    "found": self.found_at[:10],
                    "text": self.snippet,
                    "score": round(self.score, 4),
                }
            )
        elif self.score > 0 and self.snippet:
            out["text"] = self.snippet
        return out


def _norm(text: str) -> str:
    return text.strip().casefold().replace("ё", "е")


def _only_dojo() -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE))
        ]
    )


def available() -> bool:
    """Есть ли в базе находки."""
    try:
        if not client().collection_exists(config.COLLECTION):
            return False
        got = client().count(
            collection_name=config.COLLECTION, count_filter=_only_dojo(), exact=False
        )
        return got.count > 0
    except Exception as e:
        log.debug("проверка наличия находок не удалась: %s", e)
        return False


def values(field: str, limit: int = 200) -> list[str]:
    try:
        res = client().facet(
            collection_name=config.COLLECTION,
            key=field,
            facet_filter=_only_dojo(),
            limit=limit,
        )
        return sorted({str(h.value) for h in res.hits if str(h.value).strip()})
    except Exception as e:
        log.debug("facet по %s не сработал: %s", field, e)
        return []


# Как модель передаёт «по всем продуктам», когда не оставляет поле пустым
ALL_PRODUCTS = {"", "*", "all", "any", "все", "всё", "любой", "любые", "все продукты"}


def wants_all(name: str | None) -> bool:
    """Продукт не назван — спрашивают про все сразу."""
    if name is None:
        return True
    wanted = _norm(name)
    return wanted in ALL_PRODUCTS or wanted.startswith(("все ", "всё ", "all "))


def resolve_product(name: str) -> str:
    """Название из вопроса -> продукт, как он записан в индексе."""
    wanted = _norm(name)
    known = values("product")
    if not known:
        raise DojoSearchError(
            "Находки не проиндексированы. Выгрузка и индексация: "
            "docker compose exec kb python -m kb.dojo_index"
        )

    exact = [p for p in known if _norm(p) == wanted]
    if exact:
        return exact[0]

    partial = [p for p in known if wanted in _norm(p)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise DojoSearchError(
            f"Под «{name}» подходит несколько продуктов: {', '.join(partial)}. "
            "Уточни, какой нужен."
        )

    raise DojoSearchError(
        f"Продукта «{name}» среди находок нет. Есть: {', '.join(known)}."
    )


def normalize_status(value: str) -> str:
    wanted = _norm(value)
    for prefix, canonical in STATUS_RU.items():
        if wanted.startswith(prefix):
            return canonical
    raise DojoSearchError(
        f"Состояние «{value}» непонятно. Бывают: открытые, принятые как риск, "
        "ложные срабатывания, закрытые."
    )


def counts(product: str | None, status: str | None) -> dict:
    """Сводка по уровням. Считаем запросами, а не выгрузкой находок.

    product=None — по всем продуктам сразу.
    """
    out = {}
    for severity in dojo.SEVERITIES:
        must = [
            models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE))
        ]
        if product:
            must.append(
                models.FieldCondition(
                    key="product", match=models.MatchValue(value=product)
                )
            )
        must += [
            models.FieldCondition(
                key="severity", match=models.MatchValue(value=severity)
            ),
            # Считаем находки, а не чанки: у одной находки их несколько,
            # и без этого условия «критичных: 40» означало бы 12 находок
            models.FieldCondition(key="chunk_idx", match=models.MatchValue(value=0)),
        ]
        if status:
            must.append(
                models.FieldCondition(
                    key="finding_status", match=models.MatchValue(value=status)
                )
            )
        try:
            got = client().count(
                collection_name=config.COLLECTION,
                count_filter=models.Filter(must=must),
                exact=True,
            )
            out[severity] = got.count
        except Exception as e:
            log.debug("счётчик по %s не сработал: %s", severity, e)
            out[severity] = 0
    return out


def by_product(status: str | None) -> list[dict]:
    """Сводка по каждому продукту: общая картина и заодно список продуктов.

    Продукты без находок в этом состоянии тоже в списке — на вопрос «какие
    продукты есть» иначе пропали бы как раз самые благополучные. Сортировка
    по серьёзности: сначала те, где больше критичных, потом высоких и т. д.
    """
    rows = []
    for name in values("product"):
        c = counts(name, status)
        rows.append({"product": name, "total": sum(c.values()), **c})
    rows.sort(key=lambda r: tuple(-r[s] for s in dojo.SEVERITIES) + (r["product"],))
    return rows


def _worst(must: list, levels: list[str], limit: int) -> list[Hit]:
    """Самые серьёзные находки: идём по уровням от критичного вниз.

    Не «взять сотню и отсортировать»: выборка из Qdrant идёт в порядке id, и
    при сотнях находок критичные за пределами первой сотни терялись бы. По
    уровню за раз — критичные гарантированно первыми.
    """
    hits: list[Hit] = []
    for level in levels:
        if len(hits) >= limit:
            break
        points, _ = client().scroll(
            collection_name=config.COLLECTION,
            scroll_filter=models.Filter(
                must=must
                + [
                    models.FieldCondition(
                        key="severity", match=models.MatchValue(value=level)
                    ),
                    models.FieldCondition(
                        key="chunk_idx", match=models.MatchValue(value=0)
                    ),
                ]
            ),
            limit=WORST_SCAN,
            with_payload=True,
        )
        found = _dedupe(points, len(points))
        # Внутри уровня — давние первыми: дольше всего висят без внимания
        found.sort(key=lambda h: h.found_at)
        hits += found[: limit - len(hits)]
    return hits


# Сколько карточек одного уровня просматривать ради сортировки по дате.
# Больше — точнее «самые давние», но дольше; на выдачу это не влияет
WORST_SCAN = 500


def _dedupe(points, limit: int) -> list[Hit]:
    """Чанки -> находки, лучший кусок как цитата."""
    seen: dict[str, Hit] = {}
    for point in points:
        pl = point.payload or {}
        key = pl.get("finding_id", "")
        if not key:
            continue
        score = float(getattr(point, "score", 0.0) or 0.0)
        idx = pl.get("chunk_idx", 0)
        if key in seen:
            best = seen[key]
            if best.score > score or (best.score == score and best.chunk_idx <= idx):
                continue
        seen[key] = Hit(
            finding_id=key,
            title=pl.get("title", "").split(" · ", 1)[-1],
            severity=pl.get("severity", ""),
            status=pl.get("finding_status", ""),
            product=pl.get("product", ""),
            scanner=pl.get("scanner", ""),
            cwe=pl.get("cwe", ""),
            component=pl.get("component", ""),
            location=pl.get("location", ""),
            url=pl.get("url", ""),
            found_at=pl.get("found_at", ""),
            description=pl.get("description", ""),
            mitigation=pl.get("mitigation", ""),
            impact=pl.get("impact", ""),
            snippet=pl.get("text", ""),
            score=score,
            chunk_idx=idx,
        )
    return list(seen.values())[:limit]



def _enrich(hits: list[Hit]) -> None:
    """Дописать описание и рекомендацию в найденные находки.

    Нужно, потому что описание с рекомендацией лежат только в ПЕРВОМ чанке
    находки, а смысловой поиск мог выбрать любой другой — например тот, где
    совпало слово из обсуждения. Тогда отчёт остался бы без самого главного.
    Догружаем карточки одним запросом на всю выдачу.
    """
    missing = [h for h in hits if not h.description and not h.mitigation]
    if not missing:
        return
    try:
        points, _ = client().scroll(
            collection_name=config.COLLECTION,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source", match=models.MatchValue(value=SOURCE)
                    ),
                    models.FieldCondition(
                        key="finding_id",
                        match=models.MatchAny(any=[h.finding_id for h in missing]),
                    ),
                    models.FieldCondition(
                        key="chunk_idx", match=models.MatchValue(value=0)
                    ),
                ]
            ),
            limit=len(missing) + 5,
            with_payload=True,
        )
    except Exception as e:
        log.debug("не удалось догрузить карточки находок: %s", e)
        return

    cards = {(p.payload or {}).get("finding_id", ""): (p.payload or {}) for p in points}
    for hit in missing:
        card = cards.get(hit.finding_id)
        if not card:
            continue
        hit.description = card.get("description", "")
        hit.mitigation = card.get("mitigation", "")
        hit.impact = card.get("impact", "")


def search(
    product: str | None = None,
    status: str | None = "open",
    severity: str | None = None,
    query: str | None = None,
    limit: int = 10,
    report: bool = False,
) -> dict:
    """Находки: сводка по уровням плюс сами находки.

    Без продукта — по всем продуктам сразу: сводка общая, к ней таблица по
    каждому продукту, а находки подписаны продуктом.
    """
    if not available():
        if not qdrant_alive():
            raise DojoSearchError(
                f"База поиска ({config.QDRANT_URL}) не отвечает — не работает "
                "ничего, не только находки. Проверьте, что контейнер qdrant "
                "поднят: docker compose ps"
            )
        raise DojoSearchError(
            "Находки DefectDojo не проиндексированы. Выгрузка и индексация: "
            "docker compose exec kb python -m kb.dojo_index"
        )

    name = None if wants_all(product) else resolve_product(product or "")
    state = normalize_status(status) if status and status != "all" else None
    level = dojo.normalize_severity(severity) if severity else None

    must = [
        models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE))
    ]
    if name:
        must.append(
            models.FieldCondition(key="product", match=models.MatchValue(value=name))
        )
    if state:
        must.append(
            models.FieldCondition(
                key="finding_status", match=models.MatchValue(value=state)
            )
        )

    applied: dict[str, object] = {
        "product": name or "все продукты",
        "status": state or "любой",
        "severity": level or "любой",
    }

    if query and query.strip():
        applied["query"] = query
        flt_must = list(must)
        if level:
            flt_must.append(
                models.FieldCondition(
                    key="severity", match=models.MatchValue(value=level)
                )
            )
        vector = embed_batch([query])[0]
        found = client().query_points(
            collection_name=config.COLLECTION,
            query=vector,
            using=config.DENSE_VECTOR,
            query_filter=models.Filter(must=flt_must),
            limit=max(limit * 4, 20),
            with_payload=True,
        )
        points = list(found.points)
        # Порог — только по всем продуктам, как в поиске по задачам. Внутри
        # одного продукта косинусы низкие, потому что выбирать не из чего, и
        # порог выкосил бы верное. А по всем без порога на «log4j» пришли бы
        # ближайшие находки, даже если log4j нигде нет
        if not name:
            points = [p for p in points if p.score >= config.MIN_SCORE]
            applied["threshold"] = config.MIN_SCORE
        hits = _dedupe(points, limit)
    else:
        # Без смысловой части это выборка: худшие первыми. Порог
        # релевантности здесь не при чём — фильтр уже отобрал всё, что нужно
        hits = _worst(must, [level] if level else list(dojo.SEVERITIES), limit)

    if report:
        _enrich(hits)

    result = {
        "product": name or "все продукты",
        "summary": counts(name, state),
        "applied_filters": applied,
        "hits": hits,
    }
    if not name:
        result["by_product"] = by_product(state)
    return result
