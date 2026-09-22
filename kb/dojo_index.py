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

Каждый прогон забирает находки по API целиком — это дёшево, — но пересчитывает
векторы только тем, у которых что-то поменялось: статус, уровень, описание.
Находки, пропавшие из выдачи, убираются из индекса. Поэтому запуск по
расписанию (dojo/cron.sh) без изменений занимает время одной выгрузки, а не
пересчёта всего.

Запуск:
    docker compose exec kb python -m kb.dojo_index
    docker compose exec kb python -m kb.dojo_index --full
    docker compose exec kb python -m kb.dojo_index --dry-run
    docker compose exec kb python -m kb.dojo_index --dump /docs/dojo-dump
    docker compose exec kb python -m kb.dojo_index --from /docs/dojo-dump
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import config, dojo
from kb.doc_index import ensure_collection, load_state, point_id, save_state
from kb.embedder import embed_batch

log = logging.getLogger(__name__)

SOURCE = "dojo"
MAX_CHUNK_CHARS = 1500

# Версия нарезки (chunks) и состава записи (normalize). Хранится в файле
# состояния: поменяли нарезку — первый же прогон пересчитает всё, хотя сами
# находки в DefectDojo не менялись
DOJO_CHUNKER_VERSION = 1

# Файл состояния лежит в /docs — это том, он переживает пересборку контейнера.
# Внутри контейнера каталог есть всегда; при запуске с хоста задаётся --state
STATE_PATH = os.getenv("DOJO_STATE", "/docs/dojo/.index_state.dojo.json")

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
        # Служебные файлы (состояние индексатора) — не находки
        if path.name.startswith("."):
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  [!] {path.name}: {e}")
    return records


def digest(record: dict) -> str:
    """Отпечаток находки: поменялось любое поле — поменялся и он.

    Считаем по нормализованной записи, а не по ответу API: в ответе DefectDojo
    есть поля, которые меняются без смысла для нас (счётчики, служебные даты),
    и из-за них находка пересчитывалась бы на каждом прогоне.
    """
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _by_id(finding_id: str) -> models.FilterSelector:
    return models.FilterSelector(
        filter=models.Filter(
            must=[
                models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE)),
                models.FieldCondition(
                    key="source_id", match=models.MatchValue(value=finding_id)
                ),
            ]
        )
    )


def indexed_ids(client: QdrantClient, collection: str) -> set[str]:
    """Номера находок, которые уже лежат в базе.

    Нужно, когда файла состояния нет: первый прогон после перехода на
    инкрементальную индексацию или после --full. Без этого находки, которых
    больше нет в DefectDojo, остались бы в базе навсегда — удалять их было бы
    не по чему.
    """
    ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source", match=models.MatchValue(value=SOURCE)
                    ),
                    models.FieldCondition(
                        key="chunk_idx", match=models.MatchValue(value=0)
                    ),
                ]
            ),
            limit=1000,
            offset=offset,
            with_payload=["source_id"],
            with_vectors=False,
        )
        ids.update(str((p.payload or {}).get("source_id", "")) for p in points)
        if offset is None:
            ids.discard("")
            return ids


def points_for(record: dict, pieces: list[str], vectors: list) -> list[models.PointStruct]:
    title = f"{record['id']} · {record['title']}"
    return [
        models.PointStruct(
            id=point_id(SOURCE, str(record["id"]), idx),
            vector={config.DENSE_VECTOR: vec},
            payload={
                "source": SOURCE,
                "source_id": str(record["id"]),
                "space": record["product"],
                "title": title,
                "url": record["url"],
                "acl_groups": [f"dojo:{record['product']}"],
                "updated_at": record["updated"],
                "chunk_idx": idx,
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
                # Описание и рекомендация кладутся ПОЛЯМИ, а не только в текст
                # для поиска. Без этого отчёт по продукту получался с пустой
                # графой «как чинить»: текст рекомендации доставался лишь тогда,
                # когда находку нашли смысловым поиском, а при отборе по
                # фильтру — нет. Пишем только в первый чанк: дублировать в
                # каждый незачем, а карточку retriever и так предпочитает
                "description": record["description"] if idx == 0 else "",
                "mitigation": record["mitigation"] if idx == 0 else "",
                "impact": record["impact"] if idx == 0 else "",
            },
        )
        for idx, (piece, vec) in enumerate(zip(pieces, vectors))
    ]


def index(
    records: list[dict],
    collection: str,
    batch: int,
    state_path: Path,
    full: bool = False,
    force_prune: bool = False,
    dry_run: bool = False,
) -> int:
    """Пересчитать изменившиеся находки и убрать пропавшие.

    Раньше каждый прогон сносил все точки продукта и считал их заново. Для
    ручного запуска терпимо, для расписания нет: всё время пересчёта продукт
    в поиске пуст, упавший на середине прогон оставляет его полупустым, а
    эмбеддер, который делит видеокарту с чатом, каждые два часа пережёвывает
    тысячи находок, в которых ничего не поменялось.
    """
    client = QdrantClient(url=config.QDRANT_URL, timeout=120)
    exists = client.collection_exists(collection)
    if not dry_run:
        ensure_collection(client, collection)
        ensure_dojo_indexes(client, collection)

    previous = {} if full else load_state(state_path, DOJO_CHUNKER_VERSION)
    if not previous and exists:
        # Истории нет — берём, что лежит в базе, с пустым отпечатком: всё
        # пересчитается, а пропавшее из DefectDojo найдётся и удалится
        previous = {fid: "" for fid in indexed_ids(client, collection)}
        if previous:
            print(f"Файла состояния нет, в базе находок: {len(previous)} — пересчитываю все")

    current: dict[str, str] = {}
    total = 0
    skipped = 0
    stats = {"новых": 0, "обновлено": 0}

    for number, record in enumerate(records, 1):
        fid = str(record["id"])
        stamp = digest(record)
        current[fid] = stamp

        if not dry_run and (number % 100 == 0 or number == len(records)):
            print(f"[{number}/{len(records)}]")

        # Ничего не поменялось — векторы в базе актуальны. На прогоне по
        # расписанию это почти все находки
        if previous.get(fid) == stamp:
            skipped += 1
            continue

        pieces = chunks(record)
        action = "обновлено" if fid in previous else "новых"
        stats[action] += 1

        # Пробный прогон: показать, что было бы сделано, без эмбеддингов и
        # без записи в Qdrant
        if dry_run:
            print(
                f"  [{action:9}] {fid:>8} {str(record['severity'] or ''):8} "
                f"{str(record['product'] or '')[:20]:20} {record['title'][:50]} "
                f"(чанков: {len(pieces)})"
            )
            total += len(pieces)
            continue

        title = f"{record['id']} · {record['title']}"
        vectors: list = []
        for start in range(0, len(pieces), batch):
            # Заголовок находки приклеивается к каждому куску: «Как чинить»
            # без него не найдётся — в тексте рекомендации самой уязвимости
            # обычно не названо
            vectors.extend(
                embed_batch([f"{title}\n\n{p}" for p in pieces[start : start + batch]])
            )

        # Сначала векторы, потом замена. Если эмбеддер упадёт, находка
        # останется в базе в прежнем виде, а не исчезнет. Старые точки удаляем
        # целиком: описание могло стать короче, и лишний хвостовой чанк иначе
        # остался бы висеть
        client.delete(collection_name=collection, points_selector=_by_id(fid), wait=True)
        client.upsert(
            collection_name=collection,
            points=points_for(record, pieces, vectors),
            wait=True,
        )
        total += len(pieces)

    # Пропавшие из выдачи API: удалены в DefectDojo, продукт убран из
    # DOJO_PRODUCTS или у ключа отобрали права. Закрытые сюда НЕ попадают —
    # они остаются в выдаче со статусом fixed и обновляются как изменившиеся
    gone = sorted(set(previous) - set(current))
    if gone and not force_prune and len(gone) > max(10, len(previous) // 2):
        print(
            f"\n[!] Из DefectDojo разом пропало {len(gone)} из {len(previous)} находок.\n"
            "    Удалять не стал: так выглядит скорее сменившийся ключ, права или\n"
            "    DOJO_PRODUCTS, чем настоящее удаление. Если всё верно:\n"
            "    python -m kb.dojo_index --force-prune"
        )
        # Запоминаем их как есть, чтобы следующий прогон снова их заметил
        for fid in gone:
            current[fid] = previous[fid]
        gone = []

    if dry_run:
        for fid in gone:
            print(f"  [удалено  ] {fid:>8}")
        print("\nИтог (пробный прогон, ничего не записано):")
        print(f"    {'новых':14} {stats['новых']}")
        print(f"    {'обновлено':14} {stats['обновлено']}")
        print(f"    {'без изменений':14} {skipped}")
        print(f"    {'удалено':14} {len(gone)}")
        print(f"    {'чанков':14} {total}")
        return total

    for fid in gone:
        client.delete(collection_name=collection, points_selector=_by_id(fid), wait=True)

    save_state(state_path, current, DOJO_CHUNKER_VERSION)

    info = client.get_collection(collection)
    print(f"\nЗаписано чанков: {total}")
    print(f"Без изменений, пропущено находок: {skipped}")
    if gone:
        print(f"Пропало из DefectDojo и убрано из индекса: {len(gone)}")
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
    ap.add_argument(
        "--state",
        default=STATE_PATH,
        metavar="ФАЙЛ",
        help=f"где помнить, что уже посчитано (по умолчанию {STATE_PATH})",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="пересчитать все находки, не глядя на то, что уже посчитано",
    )
    ap.add_argument(
        "--force-prune",
        action="store_true",
        help="удалить пропавшие находки, даже если их подозрительно много",
    )
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    args = ap.parse_args()

    if args.dry_run and args.dump:
        print("--dump пишет файлы, а --dry-run ничего не пишет: выберите что-то одно.")
        return 1

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

    state_path = Path(args.state)
    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    index(
        records,
        args.collection,
        args.batch,
        state_path,
        args.full,
        args.force_prune,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
