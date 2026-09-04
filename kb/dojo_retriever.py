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
    snippet: str = ""
    score: float = 0.0
    chunk_idx: int = 0

    def as_dict(self, detailed: bool = False) -> dict:
        out = {
            "id": self.finding_id,
            "title": self.title,
            "severity": self.severity,
            "status": self.status,
            "url": self.url,
        }
        if self.component:
            out["component"] = self.component
        if self.location:
            out["location"] = self.location
        if detailed:
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


def counts(product: str, status: str | None) -> dict:
    """Сводка по уровням. Считаем запросами, а не выгрузкой находок."""
    out = {}
    for severity in dojo.SEVERITIES:
        must = [
            models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE)),
            models.FieldCondition(key="product", match=models.MatchValue(value=product)),
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
            snippet=pl.get("text", ""),
            score=score,
            chunk_idx=idx,
        )
    return list(seen.values())[:limit]


# Порядок серьёзности для сортировки выборки: сначала худшее
SEVERITY_ORDER = {name: i for i, name in enumerate(dojo.SEVERITIES)}


def search(
    product: str,
    status: str | None = "open",
    severity: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> dict:
    """Находки продукта: сводка по уровням плюс сами находки."""
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

    name = resolve_product(product)
    state = normalize_status(status) if status and status != "all" else None
    level = dojo.normalize_severity(severity) if severity else None

    must = [
        models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE)),
        models.FieldCondition(key="product", match=models.MatchValue(value=name)),
    ]
    if state:
        must.append(
            models.FieldCondition(
                key="finding_status", match=models.MatchValue(value=state)
            )
        )
    if level:
        must.append(
            models.FieldCondition(key="severity", match=models.MatchValue(value=level))
        )

    flt = models.Filter(must=must)
    applied = {"product": name, "status": state or "любой", "severity": level or "любой"}

    if query and query.strip():
        applied["query"] = query
        vector = embed_batch([query])[0]
        found = client().query_points(
            collection_name=config.COLLECTION,
            query=vector,
            using=config.DENSE_VECTOR,
            query_filter=flt,
            limit=max(limit * 4, 20),
            with_payload=True,
        )
        hits = _dedupe(list(found.points), limit)
    else:
        # Без смысловой части это выборка: берём карточки находок и сортируем
        # по серьёзности. Порог релевантности здесь не при чём — фильтр уже
        # отобрал всё, что нужно
        points, _ = client().scroll(
            collection_name=config.COLLECTION,
            scroll_filter=models.Filter(
                must=must
                + [models.FieldCondition(key="chunk_idx", match=models.MatchValue(value=0))]
            ),
            limit=max(limit * 4, 100),
            with_payload=True,
        )
        hits = _dedupe(points, len(points))
        hits.sort(key=lambda h: (SEVERITY_ORDER.get(h.severity, 9), h.found_at))
        hits = hits[:limit]

    return {
        "product": name,
        "summary": counts(name, state),
        "applied_filters": applied,
        "hits": hits,
    }
