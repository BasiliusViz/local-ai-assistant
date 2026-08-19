"""Индексация документов (markdown, txt) в коллекцию knowledge.

Замена index-files.ps1: тот работал только под Windows, а на сервере нужен
запуск в контейнере. Логика та же, включая все решения, добытые замерами:

1. **Заголовок никогда не становится отдельным чанком.** Раньше «## Откат»
   попадал в базу самостоятельным фрагментом и обгонял в выдаче настоящий
   ответ.
2. **Путь заголовков приклеивается к тексту ПЕРЕД эмбеддингом**, но хранится
   отдельно. Фрагмент, вырванный из раздела, теряет слова, по которым его
   ищут. Дешёвая половина Contextual Retrieval: без вызовов LLM.
3. **Разбиение не режет блоки кода.** В примерах .gitignore комментарии
   начинаются с #, и наивное разбиение по заголовкам рвало их на куски
   вроде «build/», которые лезли в топ выдачи.
4. **Идентификатор чанка детерминирован** (source|путь|номер): повторный
   запуск обновляет точки, а не плодит дубли. Основа инкрементальности.
5. **Перед записью файла его старые точки удаляются**: документ мог стать
   короче, и хвост остался бы висеть в базе осиротевшим.

Запуск:
    docker compose exec kb python -m kb.doc_index /data/docs --source confluence
"""

import argparse
import hashlib
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import config
from kb.embedder import embed_batch

log = logging.getLogger(__name__)

SUFFIXES = {".md", ".markdown", ".txt"}
MAX_CHUNK_CHARS = 1500
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE = re.compile(r"^\s*```")


def split_text(text: str, max_len: int = MAX_CHUNK_CHARS) -> list[dict]:
    """Разбивает документ на чанки, сохраняя путь заголовков для каждого."""
    sections: list[tuple[str, str]] = []  # (путь заголовков, тело)
    trail: list[str] = [""] * 7
    current: list[str] = []
    current_path = ""
    fenced = False

    def flush() -> None:
        nonlocal current
        body = "\n".join(current).strip()
        if body:
            sections.append((current_path, body))
        current = []

    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced

        m = None if fenced else HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            trail[level] = title
            for k in range(level + 1, 7):
                trail[k] = ""
            # Путь раздела - его собственные крошки. Заголовок без текста
            # чанком не станет (тело пустое), но останется в trail и попадёт
            # в крошки вложенных разделов - подпорка не нужна
            current_path = " - ".join(t for t in trail if t)
            continue

        current.append(line)

    flush()

    chunks: list[dict] = []
    for path, body in sections:
        if len(body) <= max_len:
            chunks.append({"heading": path, "text": body})
            continue
        buf = ""
        for para in re.split(r"\r?\n\s*\r?\n", body):
            para = para.strip()
            if not para:
                continue
            if buf and len(buf) + len(para) > max_len:
                chunks.append({"heading": path, "text": buf.strip()})
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            chunks.append({"heading": path, "text": buf.strip()})
    return chunks


def ensure_collection(client: QdrantClient, name: str) -> None:
    if client.collection_exists(name):
        return
    client.create_collection(
        collection_name=name,
        vectors_config={
            config.DENSE_VECTOR: models.VectorParams(
                size=config.EMBED_DIM, distance=models.Distance.COSINE
            )
        },
        # Место под гибридный поиск: заполнять нечем (Ollama не даёт sparse),
        # но слот создаётся сразу - иначе потом переиндексировать всё заново
        sparse_vectors_config={"sparse": models.SparseVectorParams()},
    )
    for field in ("source", "source_id", "space", "acl_groups"):
        client.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
    client.create_payload_index(
        collection_name=name,
        field_name="updated_at",
        field_schema=models.PayloadSchemaType.DATETIME,
    )
    print(f"Коллекция {name} создана")


def point_id(source: str, rel: str, idx: int) -> str:
    seed = f"{source}|{rel}|{idx}"
    return str(uuid.UUID(hashlib.md5(seed.encode()).hexdigest()))


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    # httpx рапортует о каждом запросе; при индексации это сотни строк,
    # среди которых теряется собственный вывод
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Индексация документов в Qdrant")
    ap.add_argument("root", help="каталог с документами")
    ap.add_argument("--source", default="local", help="метка источника")
    ap.add_argument("--collection", default=config.COLLECTION)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"Не каталог: {root}")
        return 1

    client = QdrantClient(url=config.QDRANT_URL, timeout=120)
    ensure_collection(client, args.collection)

    files = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in SUFFIXES:
                files.append(Path(dirpath) / name)
    files.sort()

    print(f"Файлов: {len(files)}")
    total = 0

    for number, path in enumerate(files, 1):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  [!] {path}: {e}")
            continue
        if not text.strip():
            continue

        chunks = split_text(text)
        if not chunks:
            continue

        rel = str(path.relative_to(root)).replace("\\", "/")
        print(f"[{number}/{len(files)}] {rel} - чанков: {len(chunks)}")

        # Старые точки этого файла долой: документ мог стать короче
        client.delete(
            collection_name=args.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source", match=models.MatchValue(value=args.source)
                        ),
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=rel)
                        ),
                    ]
                )
            ),
            wait=True,
        )

        stat = path.stat()
        updated = f"{__import__('datetime').datetime.utcfromtimestamp(stat.st_mtime).isoformat()}Z"

        for start in range(0, len(chunks), args.batch):
            batch = chunks[start : start + args.batch]
            # В вектор идёт заголовок + текст, в базу - только текст
            vectors = embed_batch(
                [
                    f"{c['heading']}\n\n{c['text']}" if c["heading"] else c["text"]
                    for c in batch
                ]
            )
            client.upsert(
                collection_name=args.collection,
                points=[
                    models.PointStruct(
                        id=point_id(args.source, rel, start + i),
                        vector={config.DENSE_VECTOR: vec},
                        payload={
                            "source": args.source,
                            "source_id": rel,
                            "space": path.parent.name,
                            "title": path.stem,
                            "url": str(path),
                            "acl_groups": ["all"],
                            "updated_at": updated,
                            "chunk_idx": start + i,
                            "heading": chunk["heading"],
                            "text": chunk["text"],
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
