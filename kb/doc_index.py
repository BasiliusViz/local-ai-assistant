"""Индексация документов (markdown, txt) в коллекцию knowledge.

Замена index-files.ps1 (теперь в win_old/): тот работал только под Windows, а на сервере нужен
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
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import config
from kb.embedder import EmbedError, embed_batch

log = logging.getLogger(__name__)

SUFFIXES = {".md", ".markdown", ".txt"}
MAX_CHUNK_CHARS = 1500
# Потолок одного куска для эмбеддера. Шлюз может отдавать bge-m3 с окном
# меньше заявленных 8192 токенов, и слишком длинный кусок получает 400.
# 3000 символов — порядка 1000-1500 токенов даже на таблицах. Если 400
# повторяются — уменьшить в .env: KB_EMBED_MAX_CHARS=1500
EMBED_MAX_CHARS = int(os.getenv("KB_EMBED_MAX_CHARS", "3000"))
# Сколько файлов подряд может не посчитаться, прежде чем прогон остановится:
# один-два — плохие страницы, десяток подряд — эмбеддер лежит, и молотить
# остальные тысячи файлов бессмысленно
MAX_FAILS_IN_ROW = 10
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

    # Абзац не режется — таблица, список, блок кода остаются одним куском,
    # чтобы «шаг 3» не терял «шаги 1-2». Но на реальном Confluence бывают
    # таблицы на сотни строк и вставленные логи: такой кусок превышает окно
    # эмбеддера на шлюзе, и тот отвечает 400. Режем только их
    out: list[dict] = []
    for chunk in chunks:
        if len(chunk["text"]) <= EMBED_MAX_CHARS:
            out.append(chunk)
        else:
            out += [
                {"heading": chunk["heading"], "text": piece}
                for piece in split_oversized(chunk["text"], EMBED_MAX_CHARS)
            ]
    return out


def split_oversized(text: str, limit: int) -> list[str]:
    """Кусок длиннее окна эмбеддера -> части по строкам, не длиннее limit.

    У таблицы в каждую часть повторяется шапка (строка заголовков и
    разделитель): строка «| nginx | 1.25 | prod |» без неё — набор слов,
    непонятно, что это за столбцы. Строка длиннее limit (минифицированный
    JSON, base64) режется по символам — смысла в ней всё равно нет.
    """
    lines = text.splitlines()
    header: list[str] = []
    if (
        len(lines) >= 2
        and lines[0].lstrip().startswith("|")
        and re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[1])
    ):
        header = lines[:2]
        lines = lines[2:]
    head = "\n".join(header)
    room = max(limit - len(head) - 1, limit // 2)

    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        for part in [line[i : i + room] for i in range(0, len(line), room)] or [""]:
            if buf and size + len(part) + 1 > room:
                pieces.append("\n".join(buf))
                buf, size = [], 0
            buf.append(part)
            size += len(part) + 1
    if buf:
        pieces.append("\n".join(buf))
    return [f"{head}\n{p}" if head else p for p in pieces if p.strip()]


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


# Версия нарезки. Входит в файл состояния: если поменять split_text, хеши
# файлов останутся прежними, и индексатор решил бы, что пересчитывать нечего,
# хотя чанки в базе нарезаны по-старому. Меняется вручную вместе с логикой
# нарезки — тогда первый же прогон переиндексирует всё
CHUNKER_VERSION = 1


def load_state(path: Path, version: int = CHUNKER_VERSION) -> dict[str, str]:
    """Хеши файлов с прошлого прогона. Нет файла или другая нарезка — пусто.

    Версию передаёт тот, кто режет: у задач Jira нарезка своя, и правка
    split_text здесь не должна заставлять их пересчитываться, как и наоборот.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("chunker_version") != version:
        print("Нарезка документов поменялась с прошлого прогона — пересчитываю всё")
        return {}
    return data.get("files", {})


# Как часто сохранять состояние по ходу прогона, в файлах
CHECKPOINT_EVERY = 200


def save_state(path: Path, files: dict[str, str], version: int = CHUNKER_VERSION) -> None:
    """Записать состояние через временный файл.

    Если прогон оборвётся посреди записи, повреждённый JSON при следующем
    запуске прочитается как пустой — и индексатор пересчитает всё заново.
    Это безопасно, но дорого; атомарная замена такого не допускает.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {"chunker_version": version, "files": files},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def delete_file_points(client: QdrantClient, collection: str, source: str, rel: str) -> None:
    """Убрать из базы все чанки одного файла."""
    client.delete(
        collection_name=collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source", match=models.MatchValue(value=source)
                    ),
                    models.FieldCondition(
                        key="source_id", match=models.MatchValue(value=rel)
                    ),
                ]
            )
        ),
        wait=True,
    )


def read_front_matter(raw: bytes) -> tuple[dict[str, str], bytes]:
    """Шапка «---\\nключ: значение\\n---\\n» в начале файла -> (поля, текст).

    Её пишет выгрузка Confluence: адрес страницы и заголовок. Текст
    возвращается байтами ровно как лежит после шапки — хеш по нему совпадает
    с хешем файла, выгруженного до появления шапки, и такой файл не
    пересчитывается. Нет шапки — пустые поля и файл целиком.
    """
    # \r?\n: файл мог пройти через Windows
    header = re.match(rb"---\r?\n(.*?)\r?\n---\r?\n", raw, re.DOTALL)
    if not header:
        return {}, raw
    meta = {}
    for line in header.group(1).decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        field = re.fullmatch(r"\s*([A-Za-z_][\w-]*)\s*:\s?(.*)", line)
        # Документ, который просто начинается с горизонтальной черты «---»:
        # между чертами обычный текст. Это не шапка — иначе он выпал бы из
        # поиска
        if not field:
            return {}, raw
        meta[field.group(1)] = field.group(2).strip()
    # «Note: ...» между двумя чертами тоже похоже на поле. Шапкой считаем,
    # только если в ней есть то, ради чего она пишется
    if not ({"url", "title"} & meta.keys()):
        return {}, raw
    return meta, raw[header.end() :]


def load_links(path: Path) -> dict[str, tuple[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {rel: (v[0], v[1]) for rel, v in data.items()}
    except (OSError, ValueError, TypeError, IndexError):
        return {}


def save_links(path: Path, links: dict[str, tuple[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(links, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def set_link(
    client: QdrantClient, collection: str, source: str, rel: str, url: str, title: str
) -> None:
    """Поменять ссылку и заголовок у всех чанков файла, не трогая векторы."""
    client.set_payload(
        collection_name=collection,
        payload={"url": url, "title": title},
        points=models.Filter(
            must=[
                models.FieldCondition(key="source", match=models.MatchValue(value=source)),
                models.FieldCondition(key="source_id", match=models.MatchValue(value=rel)),
            ]
        ),
        wait=True,
    )


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
    ap.add_argument(
        "--full",
        action="store_true",
        help="переиндексировать всё, не глядя на то, что уже посчитано",
    )
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

    state_path = root / f".index_state.{args.source}.json"
    previous = {} if args.full else load_state(state_path)
    current: dict[str, str] = {}

    # Какие ссылка и заголовок сейчас записаны у точек каждого файла. Отдельный
    # файл, а не общее состояние: его формат читают и jira_index, и dojo_index
    links_path = root / f".index_links.{args.source}.json"
    links = {} if args.full else load_links(links_path)
    relinked = 0

    print(f"Файлов: {len(files)}" + ("" if args.full else f", уже посчитано: {len(previous)}"))
    total = 0
    skipped = 0
    failed: list[str] = []
    fails_in_row = 0

    for number, path in enumerate(files, 1):
        # Промежуточное сохранение. Без него прогон на десятки тысяч файлов,
        # оборванный на середине (SSH, перезапуск контейнера), начинался бы
        # следующим запуском с нуля — часы эмбеддингов впустую. Пишем в НАЧАЛЕ
        # итерации: все файлы до текущего обработаны целиком. Ещё не
        # пройденные берём из прошлого состояния, иначе их пересчитали бы зря;
        # пропавшие с диска тоже остаются — их вычистит конец прогона
        if number > 1 and (number - 1) % CHECKPOINT_EVERY == 0:
            save_state(state_path, {**previous, **current})
            save_links(links_path, links)

        rel = str(path.relative_to(root)).replace("\\", "/")
        try:
            raw = path.read_bytes()
        except OSError as e:
            print(f"  [!] {path}: {e}")
            # Прочитать не вышло — оставляем как было, чтобы следующий прогон
            # попробовал снова, а не решил, что файл удалён
            if rel in previous:
                current[rel] = previous[rel]
            continue

        # Шапка (адрес и заголовок страницы) — отдельно от текста. Хеш и
        # эмбеддинги только по тексту: добавление шапки к уже посчитанному
        # файлу не должно заставлять пересчитывать его векторы
        meta, body = read_front_matter(raw)
        url = meta.get("url") or str(path)
        title = meta.get("title") or path.stem
        digest = hashlib.sha256(body).hexdigest()
        current[rel] = digest

        # Файл не менялся с прошлого прогона — векторы в базе актуальны.
        # Ради этого всё и затевалось: при запуске по расписанию раз в час
        # правят три страницы из сотен, и пересчитывать остальные — значит
        # каждый час гонять эмбеддер вхолостую и отбирать видеокарту у чата
        if previous.get(rel) == digest:
            skipped += 1
            # Текст тот же, а ссылка новая (например, выгрузка стала писать
            # адрес страницы) — меняем только поля, без эмбеддингов
            if links.get(rel, (str(path), path.stem)) != (url, title):
                set_link(client, args.collection, args.source, rel, url, title)
                relinked += 1
            links[rel] = (url, title)
            continue

        text = body.decode("utf-8", errors="replace")
        chunks = split_text(text) if text.strip() else []

        if chunks:
            print(f"[{number}/{len(files)}] {rel} - чанков: {len(chunks)}")

        # Сначала векторы, потом замена — как в dojo_index. Упал эмбеддер на
        # этом файле — старые точки остаются, страница в поиске в прежнем
        # виде, а не пропадает
        try:
            vectors = []
            for start in range(0, len(chunks), args.batch):
                # В вектор идёт заголовок + текст, в базу - только текст
                vectors += embed_batch(
                    [
                        f"{c['heading']}\n\n{c['text']}" if c["heading"] else c["text"]
                        for c in chunks[start : start + args.batch]
                    ]
                )
        except EmbedError as e:
            failed.append(rel)
            fails_in_row += 1
            print(f"  [!] {rel}: эмбеддинги не получены, пропускаю — {e}")
            # Не запоминаем новый хеш: следующий прогон попробует снова. Прежний
            # — оставляем, иначе конец прогона счёл бы файл удалённым
            if rel in previous:
                current[rel] = previous[rel]
            else:
                current.pop(rel, None)
            if fails_in_row >= MAX_FAILS_IN_ROW:
                save_state(state_path, {**previous, **current})
                save_links(links_path, links)
                print(
                    f"\n{fails_in_row} файлов подряд без эмбеддингов — похоже, "
                    "эмбеддер недоступен, а не плохие страницы. Останавливаюсь; "
                    "сделанное сохранено, следующий запуск продолжит."
                )
                return 1
            continue
        fails_in_row = 0

        # Старые точки этого файла долой: документ мог стать короче. И для
        # пустого — если страницу очистили, её старый текст не должен остаться
        # в поиске
        delete_file_points(client, args.collection, args.source, rel)
        links[rel] = (url, title)
        if not chunks:
            continue

        stat = path.stat()
        updated = f"{__import__('datetime').datetime.utcfromtimestamp(stat.st_mtime).isoformat()}Z"

        for start in range(0, len(chunks), args.batch):
            batch = chunks[start : start + args.batch]
            batch_vectors = vectors[start : start + args.batch]
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
                            "title": title,
                            "url": url,
                            "acl_groups": ["all"],
                            "updated_at": updated,
                            "chunk_idx": start + i,
                            "heading": chunk["heading"],
                            "text": chunk["text"],
                        },
                    )
                    for i, (chunk, vec) in enumerate(zip(batch, batch_vectors))
                ],
                wait=True,
            )
            total += len(batch)

    # Файлы, которые были в прошлый раз, а теперь пропали с диска. Их чанки
    # иначе висели бы в поиске вечно, и ассистент продолжал бы цитировать
    # страницу, которой больше нет
    gone = sorted(set(previous) - set(current))
    for rel in gone:
        delete_file_points(client, args.collection, args.source, rel)
        print(f"  удалён из индекса: {rel}")
        links.pop(rel, None)

    save_state(state_path, current)
    save_links(links_path, {rel: v for rel, v in links.items() if rel in current})

    info = client.get_collection(args.collection)
    print(f"\nЗаписано чанков: {total}")
    print(f"Без изменений, пропущено файлов: {skipped}")
    if relinked:
        print(f"Из них обновлена только ссылка (без эмбеддингов): {relinked}")
    if gone:
        print(f"Пропало с диска и убрано из индекса: {len(gone)}")
    print(f"Всего в коллекции {args.collection}: {info.points_count}")
    if failed:
        print(f"\nНе посчитано (повторятся в следующий прогон): {len(failed)}")
        for rel in failed[:20]:
            print(f"    {rel}")
        if len(failed) > 20:
            print(f"    ... и ещё {len(failed) - 20}")
        print(
            "Если причина — длина (400, context length), уменьшите "
            f"KB_EMBED_MAX_CHARS в .env (сейчас {EMBED_MAX_CHARS})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
