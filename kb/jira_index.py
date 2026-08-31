"""Индексация выгруженных задач Jira в коллекцию knowledge.

Отдельно от doc_index не из любви к симметрии: у задачи есть поля, и вопросы
к ней ставятся по полям — «что на Иванове», «что открыто в DEVSEC». Векторный
поиск на такие вопросы отвечает плохо, потому что исполнитель и статус в
тексте задачи не написаны. Поэтому поля едут в payload и получают индексы, а
вектор остаётся для смысловой части вопроса («что там про авторизацию»).

Раскладка чанков:
  - описание задачи, при необходимости разрезанное на части;
  - каждый комментарий отдельным чанком.
Все чанки одной задачи несут одинаковые поля (исполнитель, статус, проект) —
иначе фильтр «задачи Иванова» терял бы обсуждения, а именно в обсуждении
обычно и лежит ответ.

Перед эмбеддингом к каждому чанку приклеивается «KEY: заголовок» — комментарий
в отрыве от задачи не значит ничего, и без этой приписки не находится.

Запуск:
    docker compose exec kb python -m kb.jira_index /docs/jira
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import config
from kb.doc_index import ensure_collection, point_id
from kb.embedder import embed_batch

log = logging.getLogger(__name__)

SOURCE = "jira"
MAX_CHUNK_CHARS = 1500

# Поля задачи, по которым фильтруем. Без индекса Qdrant тоже отфильтрует, но
# полным перебором коллекции — на десятках тысяч чанков это заметно
JIRA_INDEXED_FIELDS = (
    "issue_key",
    "project",
    "status",
    "status_category",
    "issue_type",
    "assignee",
    "assignee_login",
    "reporter",
    "labels",
)


def ensure_jira_indexes(client: QdrantClient, collection: str) -> None:
    """Индексы payload под поля задач. Повторный вызов безвреден."""
    for field in JIRA_INDEXED_FIELDS:
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as e:  # индекс уже есть — обычное дело при переиндексации
            log.debug("индекс по %s не создан: %s", field, e)


def iso(value: str) -> str:
    """Дата Jira -> RFC 3339, который принимает Qdrant.

    Jira отдаёт смещение без двоеточия (+0300), а Qdrant ждёт +03:00 и на
    исходном виде спотыкается. Разбираем и печатаем заново, а не правим
    строку регуляркой: форматы у разных сборок расходятся.
    """
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
    return value


def split_body(text: str, max_len: int = MAX_CHUNK_CHARS) -> list[str]:
    """Режет длинный текст по абзацам, не рассекая их посередине."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    buf = ""
    for para in re.split(r"\r?\n\s*\r?\n", text):
        para = para.strip()
        if not para:
            continue
        if buf and len(buf) + len(para) > max_len:
            parts.append(buf.strip())
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf.strip():
        parts.append(buf.strip())
    return parts


def issue_chunks(issue: dict) -> list[dict]:
    """Задача -> список чанков с пометкой, откуда каждый взялся."""
    chunks: list[dict] = []

    # Шапка первого чанка: то, что человек и так увидел бы в карточке задачи.
    # Нужна, чтобы вопрос «в каком статусе задача про импорт» находил ответ
    # смысловым поиском, а не только фильтром
    card = [
        f"Задача {issue['key']}: {issue['summary']}",
        f"Проект: {issue.get('project', '')}. Тип: {issue.get('type', '')}. "
        f"Статус: {issue.get('status', '')}.",
    ]
    if issue.get("assignee"):
        card.append(f"Исполнитель: {issue['assignee']}.")
    else:
        card.append("Исполнитель: не назначен.")
    if issue.get("reporter"):
        card.append(f"Автор: {issue['reporter']}.")
    if issue.get("priority"):
        card.append(f"Приоритет: {issue['priority']}.")
    if issue.get("resolution"):
        card.append(f"Резолюция: {issue['resolution']}.")
    if issue.get("labels"):
        card.append(f"Метки: {', '.join(issue['labels'])}.")
    if issue.get("components"):
        card.append(f"Компоненты: {', '.join(issue['components'])}.")

    body = split_body(issue.get("description", ""))
    head = " ".join(card)
    chunks.append({"kind": "issue", "text": f"{head}\n\n{body[0]}" if body else head})
    for extra in body[1:]:
        chunks.append({"kind": "issue", "text": extra})

    for comment in issue.get("comments", []):
        text = (comment.get("text") or "").strip()
        if not text:
            continue
        when = (comment.get("created") or "")[:10]
        author = comment.get("author") or "неизвестно"
        for part in split_body(text):
            chunks.append(
                {"kind": "comment", "text": f"Комментарий, {author}, {when}:\n{part}"}
            )

    return chunks


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Индексация задач Jira в Qdrant")
    ap.add_argument("root", help="каталог с выгрузкой (jira/sync.py)")
    ap.add_argument("--collection", default=config.COLLECTION)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"Не каталог: {root}")
        print("Сначала выгрузите задачи: python jira/sync.py")
        return 1

    files = sorted(p for p in root.rglob("*.json") if not p.name.startswith("."))
    if not files:
        print(f"В {root} нет выгруженных задач.")
        print("Сначала выгрузите их: python jira/sync.py")
        return 1

    client = QdrantClient(url=config.QDRANT_URL, timeout=120)
    ensure_collection(client, args.collection)
    ensure_jira_indexes(client, args.collection)

    print(f"Задач: {len(files)}")
    total = 0

    for number, path in enumerate(files, 1):
        try:
            issue = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  [!] {path.name}: {e}")
            continue

        key = issue.get("key") or path.stem
        chunks = issue_chunks(issue)
        if not chunks:
            continue

        if number % 50 == 0 or number == len(files):
            print(f"[{number}/{len(files)}] {key}")

        # Старые точки этой задачи долой: описание могло стать короче, а
        # комментарий — исчезнуть. Иначе хвост останется висеть осиротевшим
        client.delete(
            collection_name=args.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source", match=models.MatchValue(value=SOURCE)
                        ),
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=key)
                        ),
                    ]
                )
            ),
            wait=True,
        )

        project = issue.get("project", "")
        title = f"{key}: {issue.get('summary', '')}"
        updated = iso(issue.get("updated", ""))

        for start in range(0, len(chunks), args.batch):
            batch = chunks[start : start + args.batch]
            # В вектор идёт номер и заголовок задачи вместе с текстом, в базу —
            # только текст. Комментарий сам по себе нередко бессодержателен
            # («сделал, проверь»), и без приписки его не найти
            vectors = embed_batch([f"{title}\n\n{c['text']}" for c in batch])
            client.upsert(
                collection_name=args.collection,
                points=[
                    models.PointStruct(
                        id=point_id(SOURCE, key, start + i),
                        vector={config.DENSE_VECTOR: vec},
                        payload={
                            # Общая для всех источников часть
                            "source": SOURCE,
                            "source_id": key,
                            # space = проект: поле уже индексировано, и
                            # space_filter в kb_search начинает работать даром
                            "space": project,
                            "title": title,
                            "url": issue.get("url", ""),
                            # Не ["all"]: у задач права по проектам, и когда
                            # авторизация появится, переиндексировать не придётся
                            "acl_groups": [f"jira:{project}"],
                            "updated_at": updated,
                            "chunk_idx": start + i,
                            "heading": title,
                            "text": chunk["text"],
                            # Часть, ради которой всё и затевалось
                            "issue_key": key,
                            "project": project,
                            "status": issue.get("status", ""),
                            "status_category": issue.get("status_category", ""),
                            "issue_type": issue.get("type", ""),
                            "priority": issue.get("priority", ""),
                            "resolution": issue.get("resolution", ""),
                            "assignee": issue.get("assignee", ""),
                            "assignee_login": issue.get("assignee_login", ""),
                            "reporter": issue.get("reporter", ""),
                            "reporter_login": issue.get("reporter_login", ""),
                            "labels": issue.get("labels", []),
                            "chunk_kind": chunk["kind"],
                        },
                    )
                    for i, (chunk, vec) in enumerate(zip(batch, vectors))
                ],
                wait=True,
            )
            total += len(batch)

    info = client.get_collection(args.collection)
    print(f"\nЗаписано чанков: {total}")
    print(f"Всего в коллекции {args.collection}: {info.points_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
