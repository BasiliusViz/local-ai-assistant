"""Поиск по коллекции knowledge в Qdrant.

Отдаёт результат уже в том виде, в каком его удобно читать модели:
человекочитаемые поля, никаких UUID точек и внутренних идентификаторов.
"""

import logging
from dataclasses import dataclass, field

from qdrant_client import QdrantClient, models

from kb import config
from kb.embedder import embed_batch
from kb.expander import expand
from kb.reranker import rerank

log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


@dataclass
class Hit:
    text: str
    title: str
    source: str
    source_id: str
    space: str
    url: str
    updated_at: str
    score: float
    chunk_idx: int = 0

    def as_dict(self, detailed: bool = False) -> dict:
        """concise отдаёт минимум: текст и откуда он. detailed — всё."""
        if not detailed:
            # Текст целиком, без обрезки: она экономила копейки контекста
            # (5 чанков по 1500 символов против окна в 32K), зато модель
            # копировала обрубок в ответ вместе с многоточием и обрывала
            # фразу на полуслове. concise/detailed различаются метаданными.
            #
            # location отдаём и в кратком режиме: без кликабельной ссылки
            # ответ по внутренней документации нечем проверить. Для Confluence
            # тут будет URL страницы, для локальных файлов - путь
            return {
                "text": self.text,
                "document": self.source_id,
                "location": self.url,
            }
        return {
            "text": self.text,
            "title": self.title,
            "source": self.source,
            "document": self.source_id,
            "space": self.space,
            "location": self.url,
            "updated": self.updated_at,
            "chunk": self.chunk_idx,
            "score": round(self.score, 4),
        }


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    dropped: int = 0


_client: QdrantClient | None = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=config.QDRANT_URL, timeout=int(config.HTTP_TIMEOUT))
    return _client


def known_values(field_name: str) -> list[str]:
    """Какие значения реально есть в базе — для внятных сообщений об ошибках."""
    try:
        res = client().facet(
            collection_name=config.COLLECTION, key=field_name, limit=50
        )
        return sorted(str(h.value) for h in res.hits)
    except Exception as e:  # фасеты есть не во всех версиях Qdrant
        log.debug("facet по %s не сработал: %s", field_name, e)
        return []


def _build_filter(source: str | None, space: str | None) -> models.Filter | None:
    must = []
    if source:
        must.append(
            models.FieldCondition(key="source", match=models.MatchValue(value=source))
        )
    if space:
        must.append(
            models.FieldCondition(key="space", match=models.MatchValue(value=space))
        )
    return models.Filter(must=must) if must else None


def _search_many(queries: list[str], limit: int, flt) -> list:
    """Поиск по нескольким формулировкам со слиянием по RRF.

    Скоры разных запросов несравнимы между собой, поэтому складываем не их, а
    обратные ранги: 1/(k + позиция). Документ, попавший в несколько списков,
    поднимается наверх, даже если ни в одном не был первым.

    В `score` при этом остаётся ЛУЧШИЙ косинус из всех запросов, а не вес RRF:
    порог MIN_SCORE откалиброван под косинус, и подменять его несравнимой
    величиной нельзя — иначе отсечка мусора («рецепт борща») перестанет работать.
    """
    vectors = embed_batch(queries)

    responses = client().query_batch_points(
        collection_name=config.COLLECTION,
        requests=[
            models.QueryRequest(
                query=v,
                using=config.DENSE_VECTOR,
                filter=flt,
                limit=limit,
                with_payload=True,
            )
            for v in vectors
        ],
    )

    fused: dict[str, float] = {}
    best: dict[str, object] = {}

    for response in responses:
        for rank, point in enumerate(response.points):
            key = str(point.id)
            fused[key] = fused.get(key, 0.0) + 1.0 / (config.RRF_K + rank + 1)
            if key not in best or point.score > best[key].score:
                best[key] = point

    # Сортируем по ЛУЧШЕМУ косинусу среди всех формулировок, а RRF оставляем
    # тай-брейкером. Чистый RRF здесь вредил: он поднимает то, что нашлось по
    # всем вариантам сразу, а верный ответ обычно находит ровно одна удачная
    # переформулировка — и она тонула. Замерено на двух разных вопросах.
    order = sorted(fused, key=lambda k: (best[k].score, fused[k]), reverse=True)
    return [best[k] for k in order][:limit]


def _to_hit(point, score: float) -> Hit:
    pl = point.payload or {}
    return Hit(
        text=pl.get("text", ""),
        title=pl.get("title", ""),
        source=pl.get("source", ""),
        source_id=pl.get("source_id", ""),
        space=pl.get("space", ""),
        url=pl.get("url", ""),
        updated_at=pl.get("updated_at", ""),
        chunk_idx=pl.get("chunk_idx", 0),
        score=score,
    )


def _rerank_hits(query: str, points, top_k: int) -> "SearchResult":
    """Вторая ступень: модель переупорядочивает кандидатов и режет по оценке."""
    texts = [(p.payload or {}).get("text", "") for p in points]
    scores = rerank(query, texts)

    # Реранк не сработал - отдаём порядок Qdrant со старым порогом,
    # это хуже, но лучше пустого ответа
    if not scores:
        kept = [_to_hit(p, p.score) for p in points if p.score >= config.MIN_SCORE]
        return SearchResult(hits=kept[:top_k], dropped=len(points) - len(kept))

    ranked = sorted(zip(points, scores), key=lambda x: x[1], reverse=True)
    hits = [_to_hit(p, s) for p, s in ranked if s >= config.RERANK_MIN][:top_k]

    log.info(
        "реранк: %d кандидатов -> %d прошло (оценки: %s)",
        len(points),
        len(hits),
        ", ".join(f"{s:+.1f}" for _, s in ranked[:6]),
    )
    return SearchResult(hits=hits, dropped=len(points) - len(hits))


def collection_ready() -> bool:
    """Есть ли коллекция. На свежем развёртывании её ещё нет."""
    try:
        return client().collection_exists(config.COLLECTION)
    except Exception as e:
        log.debug("не удалось проверить коллекцию: %s", e)
        return False


def search(
    query: str,
    top_k: int = config.DEFAULT_TOP_K,
    source: str | None = None,
    space: str | None = None,
) -> SearchResult:
    if not query or not query.strip():
        raise SearchError("Пустой запрос: нужен вопрос на естественном языке.")

    # Коллекция создаётся при первой индексации. Без этой проверки поиск
    # падал с невнятной ошибкой Qdrant вместо понятного объяснения
    if not collection_ready():
        raise SearchError(
            f"Коллекция «{config.COLLECTION}» ещё не создана — документы не "
            "проиндексированы. Положите их в каталог DOCS_DIR и выполните: "
            "docker compose exec kb python -m kb.doc_index /docs/<папка> "
            "--source <имя источника>"
        )

    top_k = max(1, min(top_k, config.MAX_TOP_K))

    # Фильтры проверяем заранее: пустой ответ из-за опечатки в источнике
    # неотличим от «ничего не нашлось», а это разные проблемы.
    if source:
        available = known_values("source")
        if available and source not in available:
            raise SearchError(
                f"Источника '{source}' в базе нет. "
                f"Доступные: {', '.join(available)}."
            )
    if space:
        available = known_values("space")
        if available and space not in available:
            raise SearchError(
                f"Пространства '{space}' в базе нет. "
                f"Доступные: {', '.join(available)}."
            )

    # С реранком берём с запасом и БЕЗ порога: на первой ступени важно не
    # потерять верный ответ, его позиция неважна — её исправит вторая ступень
    limit = config.CANDIDATES if config.RERANK else top_k

    flt = _build_filter(source, space)

    # Сначала обычный поиск. Если он уверен - расширение не нужно, и запрос
    # укладывается в секунду вместо четырёх. Переформулировка включается
    # только на трудных вопросах, где разрыв в словаре и мешает.
    points = _search_many([query], limit, flt)
    if config.EXPAND and (not points or points[0].score < config.EXPAND_IF_BELOW):
        variants = expand(query)
        if len(variants) > 1:
            # Кандидатов с запасом на КАЖДЫЙ вариант. Обрезать здесь нельзя:
            # сначала отсекаем слабые по порогу, и только потом берём top_k -
            # иначе хорошие результаты вылетают до фильтрации, а их места
            # занимают слабые, которые фильтр всё равно выбросит
            points = _search_many(variants, config.CANDIDATES, flt)
            points = [p for p in points if p.score >= config.MIN_SCORE][:limit]

    if config.RERANK and points:
        return _rerank_hits(query, points, top_k)

    hits = []
    dropped = 0
    for p in points:
        if p.score < config.MIN_SCORE:
            dropped += 1
            continue
        pl = p.payload or {}
        hits.append(
            Hit(
                text=pl.get("text", ""),
                title=pl.get("title", ""),
                source=pl.get("source", ""),
                source_id=pl.get("source_id", ""),
                space=pl.get("space", ""),
                url=pl.get("url", ""),
                updated_at=pl.get("updated_at", ""),
                chunk_idx=pl.get("chunk_idx", 0),
                score=p.score,
            )
        )

    if dropped:
        log.info("отсеяно по порогу %.2f: %d из %d", config.MIN_SCORE, dropped, len(points))

    return SearchResult(hits=hits, dropped=dropped)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    questions = [
        "как откатить деплой платёжного шлюза?",
        "какие требования к паролям и хранению секретов?",
        "что делать новому сотруднику в первый день?",
    ]

    for q in questions:
        print(f"\n{'=' * 70}\nВОПРОС: {q}\n{'=' * 70}")
        for i, h in enumerate(search(q, top_k=3).hits, 1):
            snippet = " ".join(h.text.split())[:90]
            print(f"[{i}] {h.score:.3f}  {h.source_id}")
            print(f"    {snippet}…")

    print(f"\nИсточники в базе: {known_values('source')}")
    print(f"Пространства:     {known_values('space')}")

    print("\nПроверка ошибки на несуществующем источнике:")
    try:
        search("тест", source="jira")
    except SearchError as e:
        print(f"  {e}")
