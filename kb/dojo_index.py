"""Индексация находок DefectDojo в коллекцию knowledge.

Забирает находки по API и кладёт в Qdrant — без промежуточных файлов, в
отличие от Confluence и Jira. Причина простая: у находки нет тяжёлого тела,
которое дорого выгружать повторно, а перезабрать всё по API быстрее, чем
поддерживать ещё один каталог на диске. Если DefectDojo не виден с машины, где
крутится Qdrant, есть `--dump` и `--from`: выгрузить в JSON там, где доступ
есть, и проиндексировать там, где есть база.

Устройство то же, что у задач: поля находки едут в payload с индексами, а в
вектор идёт текст. Вопросы к уязвимостям почти всегда счётные («сколько
открытых критичных») либо отборочные («что по этому продукту»), и решаются они
фильтром. Смысловой поиск нужен реже — «что у нас по инъекциям», — но раз
описания и рекомендации в находке есть, они индексируются.

Запуск:
    docker compose exec kb python -m kb.dojo_index
    docker compose exec kb python -m kb.dojo_index --dump /docs/dojo
    docker compose exec kb python -m kb.dojo_index --from /docs/dojo
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import config, dojo
from kb.doc_index import ensure_collection, point_id
from kb.embedder import embed_batch

log = logging.getLogger(__name__)

SOURCE = "dojo"
MAX_CHUNK_CHARS = 1500

DOJO_INDEXED_FIELDS = (
    "finding_id",
    "product",
    "severity",
    "finding_status",
    "scanner",
    "cwe",
    "component",
)


def ensure_dojo_indexes(client: QdrantClient, collection: str) -> None:
    """Индексы payload под поля находок. Повторный вызов безвреден."""
    for field in DOJO_INDEXED_FIELDS:
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as e:  # уже есть — обычное дело при переиндексации
            log.debug("индекс по %s не создан: %s", field, e)


def normalize(item: dict, product: str) -> dict:
    """Находка DefectDojo -> плоская запись для индекса."""
    fid = item.get("id", 0)
    component = " ".join(
        filter(
            None,
            [item.get("component_name") or "", item.get("component_version") or ""],
        )
    ).strip()

    where = item.get("file_path") or ""
    if where and item.get("line"):
        where = f"{where}:{item['line']}"

    return {
        "id": fid,
        "product": product,
        "title": (item.get("title") or "").strip(),
        "severity": item.get("severity", ""),
        "status": dojo.finding_status(item),
        "scanner": dojo.scanner_name(item),
        # CWE у DefectDojo число, 0 означает «не указан»
        "cwe": f"CWE-{item['cwe']}" if item.get("cwe") else "",
        "component": component,
        "location": where,
        "description": (item.get("description") or "").strip(),
        "mitigation": (item.get("mitigation") or "").strip(),
        "impact": (item.get("impact") or "").strip(),
        "date": item.get("date") or "",
        "updated": item.get("last_status_update") or item.get("date") or "",
        "url": f"{dojo.URL}/finding/{fid}",
    }


def chunks(record: dict) -> list[str]:
    """Находка -> куски текста для эмбеддинга.

    Карточка идёт первой и всегда: по ней находится сама находка. Описание,
    влияние и рекомендация — отдельными кусками, если они длинные: рекомендация
    по устранению это часто самая полезная часть, и растворять её в общем
    полотне не стоит.
    """
    card = [
        f"{record['severity']} · {record['title']}",
        f"Продукт: {record['product']}. Статус: {record['status']}.",
    ]
    if record["scanner"]:
        card.append(f"Нашёл: {record['scanner']}.")
    if record["cwe"]:
        card.append(f"{record['cwe']}.")
    if record["component"]:
        card.append(f"Компонент: {record['component']}.")
    if record["location"]:
        card.append(f"Где: {record['location']}.")

    out = [" ".join(card)]

    for label, key in (("Описание", "description"), ("Влияние", "impact"), ("Как чинить", "mitigation")):
        text = record.get(key, "")
        if not text:
            continue
        for start in range(0, len(text), MAX_CHUNK_CHARS):
            out.append(f"{label}: {text[start : start + MAX_CHUNK_CHARS]}")

    return out


def fetch(dump_to: Path | None = None) -> list[dict]:
    """Находки всех разрешённых продуктов, при желании с записью в файлы."""
    if not dojo.configured():
        print("Не заданы DOJO_URL и DOJO_TOKEN. Пропишите их в .env,")
        print("затем: docker compose up -d kb")
        return []

    records: list[dict] = []
    with dojo._client() as client:
        visible = dojo.products(client)
        if not visible:
            print("Нет доступных продуктов: проверьте права ключа и DOJO_PRODUCTS.")
            return []

        print(f"Продуктов: {len(visible)}")
        for product in visible:
            name = product.get("name", "")
            got = [normalize(item, name) for item in dojo.all_findings(client, product["id"])]
            print(f"    {name:30} находок: {len(got)}")
            records.extend(got)

            if dump_to:
                folder = dump_to / name.replace("/", "-")
                folder.mkdir(parents=True, exist_ok=True)
                for record in got:
                    (folder / f"{record['id']}.json").write_text(
                        json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

    return records


def load(folder: Path) -> list[dict]:
    """Ранее выгруженные находки из файлов."""
    records = []
    for path in sorted(folder.rglob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  [!] {path.name}: {e}")
    return records


def index(records: list[dict], collection: str, batch: int) -> int:
    client = QdrantClient(url=config.QDRANT_URL, timeout=120)
    ensure_collection(client, collection)
    ensure_dojo_indexes(client, collection)

    # Продукты, которые сейчас переиндексируем: старые точки по ним убираем
    # целиком. Находка могла быть закрыта и исчезнуть из выдачи API — если её
    # не удалить, ассистент продолжит показывать её как открытую
    touched = sorted({r["product"] for r in records})
    for product in touched:
        client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source", match=models.MatchValue(value=SOURCE)
                        ),
                        models.FieldCondition(
                            key="product", match=models.MatchValue(value=product)
                        ),
                    ]
                )
            ),
            wait=True,
        )

    total = 0
    for number, record in enumerate(records, 1):
        pieces = chunks(record)
        title = f"{record['id']} · {record['title']}"

        if number % 100 == 0 or number == len(records):
            print(f"[{number}/{len(records)}]")

        for start in range(0, len(pieces), batch):
            part = pieces[start : start + batch]
            # Заголовок находки приклеивается к каждому куску: «Как чинить»
            # без него не найдётся — в тексте рекомендации самой уязвимости
            # обычно не названо
            vectors = embed_batch([f"{title}\n\n{piece}" for piece in part])
            client.upsert(
                collection_name=collection,
                points=[
                    models.PointStruct(
                        id=point_id(SOURCE, str(record["id"]), start + i),
                        vector={config.DENSE_VECTOR: vec},
                        payload={
                            "source": SOURCE,
                            "source_id": str(record["id"]),
                            "space": record["product"],
                            "title": title,
                            "url": record["url"],
                            "acl_groups": [f"dojo:{record['product']}"],
                            "updated_at": record["updated"],
                            "chunk_idx": start + i,
                            "heading": title,
                            "text": piece,
                            "finding_id": str(record["id"]),
                            "product": record["product"],
                            "severity": record["severity"],
                            "finding_status": record["status"],
                            "scanner": record["scanner"],
                            "cwe": record["cwe"],
                            "component": record["component"],
                            "location": record["location"],
                            "found_at": record["date"],
                        },
                    )
                    for i, (piece, vec) in enumerate(zip(part, vectors))
                ],
                wait=True,
            )
            total += len(part)

    info = client.get_collection(collection)
    print(f"\nЗаписано чанков: {total}")
    print(f"Всего в коллекции {collection}: {info.points_count}")
    return total


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Индексация находок DefectDojo")
    ap.add_argument("--dump", metavar="КАТАЛОГ", help="сохранить находки в файлы")
    ap.add_argument(
        "--from",
        dest="source_dir",
        metavar="КАТАЛОГ",
        help="индексировать ранее выгруженное, не обращаясь к DefectDojo",
    )
    ap.add_argument("--collection", default=config.COLLECTION)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    if args.source_dir:
        folder = Path(args.source_dir)
        if not folder.is_dir():
            print(f"Не каталог: {folder}")
            return 1
        records = load(folder)
    else:
        records = fetch(Path(args.dump) if args.dump else None)

    if not records:
        print("Индексировать нечего.")
        return 1

    print(f"\nНаходок к индексации: {len(records)}")
    if args.dump and not args.source_dir:
        print(f"Файлы: {args.dump}")

    index(records, args.collection, args.batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
