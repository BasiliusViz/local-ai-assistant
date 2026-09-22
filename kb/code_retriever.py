"""Смысловой поиск по коду: найти МЕСТО по описанию задачи.

Отдельно от retriever.py, потому что у кода своя коллекция, свой порог и своя
подача результата: человеку нужны файл и строки, куда идти править, а не
пересказ содержимого.
"""

import logging
import os
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from kb import config
from kb.code_index import CODE_COLLECTION
from kb.embedder import embed

log = logging.getLogger(__name__)

# Порог ниже, чем у документации: код формулируется иначе, чем задача,
# и совпадения по смыслу дают меньшие косинусы. Замерить на реальных
# репозиториях и поправить.
MIN_SCORE = 0.35

_client: QdrantClient | None = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=config.QDRANT_URL, timeout=60)
    return _client


@dataclass
class CodeHit:
    repo: str
    path: str
    symbol: str
    kind: str
    signature: str
    doc: str
    line_start: int
    line_end: int
    text: str
    score: float

    def as_dict(self, full_code: bool) -> dict:
        out = {
            # Главное в ответе - куда идти. Формат file:line кликабелен в IDE
            "location": f"{self.repo}/{self.path}:{self.line_start}",
            "symbol": self.symbol,
            "kind": self.kind,
        }
        if self.signature:
            out["signature"] = self.signature
        if self.doc:
            out["doc"] = self.doc
        out["code"] = self.text if full_code else self.text[:600]
        return out


# Сколько лучших фрагментов отдаётся целиком в обычном режиме и общий бюджет
# символов кода на ответ. Раньше все фрагменты резались до 600 символов, и от
# куска Jenkinsfile (до 120 строк) модель видела только @Library и agent —
# стадий не было, и она говорила, что внутри ничего нет. Полный текст
# давал лишь "detailed", а модель его почти никогда не просит
FULL_TOP = 2
PREVIEW_CHARS = 600
# 6000, а не больше: ответ инструмента, правила и описания всех инструментов
# MCP ложатся в один контекст модели, и при contextLength 8-16K в Continue
# 9000 символов кода уже давали «message exceeds context limit». С большим
# контекстом (32K) можно поднять в .env
BUDGET_CHARS = int(os.getenv("KB_CODE_BUDGET_CHARS", "6000"))


def shape(hits: list[CodeHit], detailed: bool) -> list[dict]:
    """Ответ инструмента: лучшие целиком, остальные — началом, в пределах бюджета.

    Бюджет нужен, потому что у модели ограничен контекст: восемь кусков по
    4000 символов — это уже больше, чем влезает в 8K токенов вместе с
    вопросом и ответом.
    """
    out = []
    left = BUDGET_CHARS
    for i, hit in enumerate(hits):
        item = hit.as_dict(full_code=True)
        code = item["code"]
        if detailed:
            keep = len(code)
        elif i < FULL_TOP:
            keep = max(left, PREVIEW_CHARS)  # лучшие — целиком, пока есть бюджет
        else:
            keep = min(PREVIEW_CHARS, max(left, 0))
        if keep <= 0:
            # бюджет кончился — только адрес: куда идти, без кода
            item.pop("code")
            item["truncated"] = True
        elif keep < len(code):
            item["code"] = code[:keep]
            item["truncated"] = True
        left -= len(item.get("code", ""))
        out.append(item)
    return out


def available() -> bool:
    try:
        return client().collection_exists(CODE_COLLECTION)
    except Exception:
        return False


def repos() -> list[str]:
    """Какие репозитории проиндексированы - для внятных ошибок фильтра."""
    try:
        res = client().facet(collection_name=CODE_COLLECTION, key="repo", limit=50)
        return sorted(str(h.value) for h in res.hits)
    except Exception as e:
        log.debug("facet по repo не сработал: %s", e)
        return []


def search(query: str, top_k: int = 5, repo: str | None = None) -> list[CodeHit]:
    if not query.strip():
        return []

    flt = None
    if repo:
        flt = models.Filter(
            must=[models.FieldCondition(key="repo", match=models.MatchValue(value=repo))]
        )

    points = client().query_points(
        collection_name=CODE_COLLECTION,
        query=embed(query),
        using=config.DENSE_VECTOR,
        query_filter=flt,
        limit=top_k,
        with_payload=True,
    ).points

    hits = []
    for p in points:
        if p.score < MIN_SCORE:
            continue
        pl = p.payload or {}
        hits.append(
            CodeHit(
                repo=pl.get("repo", ""),
                path=pl.get("path", ""),
                symbol=pl.get("symbol", ""),
                kind=pl.get("kind", ""),
                signature=pl.get("signature", ""),
                doc=pl.get("doc", ""),
                line_start=pl.get("line_start", 0),
                line_end=pl.get("line_end", 0),
                text=pl.get("text", ""),
                score=p.score,
            )
        )
    return hits
