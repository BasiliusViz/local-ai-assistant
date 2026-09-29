"""Индексация кода для смыслового поиска.

Отвечает на вопросы вида «где у нас реализовано ограничение частоты запросов»,
«есть ли уже функция для парсинга дат» — то есть находит МЕСТО по описанию
задачи, а не по имени символа. Структурные вопросы («кто вызывает») решает
граф Graphify, это другой инструмент.

Три решения, каждое обосновано:

1. **Чанк = функция или класс целиком**, а не N символов подряд. Разрезанная
   пополам функция бесполезна: по ней нельзя ни понять логику, ни показать
   человеку, куда править.
2. **Перед эмбеддингом дописывается шапка** — репозиторий, путь, класс,
   сигнатура, докстринг. Метод `_check` в вакууме не опознаётся; он же с
   шапкой «payments/api.py :: RateLimiter._check» находится по смыслу. Тот же
   приём, что с заголовками в документации.
3. **Отдельная коллекция `code`.** Кода в разы больше документации, в общей
   коллекции он утопил бы вики в выдаче. Плюс свои пороги и свой размер чанка.

Запуск:
    python -m kb.code_index D:\\code-data\\repos
    python -m kb.code_index D:\\code-data\\repos --recreate
"""

import argparse
import ast
import hashlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import code_chunks, config
from kb.embedder import EmbedError, embed_batch

log = logging.getLogger(__name__)

CODE_COLLECTION = "code"
# Код релиза CB18.5 — отдельно, со своим инструментом cb_search: он в разы
# больше остального кода и забивал бы выдачу code_search. Индексируется
# вручную (update-cb.sh), см. guides/PLAN-CB.md
CB_COLLECTION = "code_cb"

# Языки программирования режутся на функции в kb/code_chunks.py (tree-sitter),
# Python — здесь, через ast. Остальное, по чему искать нужно, но разбирать
# нечего, идёт кусками текста: «какая джоба деплоит сервис», «от какого
# образа наследуемся», «какая таблица хранит платежи»
TEXT_FILES = {
    "Dockerfile", "Makefile", "docker-compose.yml", "docker-compose.yaml",
    "requirements.txt", "pyproject.toml", "go.mod", "pom.xml", "package.json",
    "Cargo.toml", "composer.json", "Vagrantfile", "Procfile", ".gitlab-ci.yml",
}
TEXT_SUFFIXES = {
    ".yml", ".yaml", ".tf", ".tfvars", ".hcl", ".ini", ".cfg", ".toml", ".conf",
    ".properties", ".sql", ".proto", ".graphql", ".gql", ".xml", ".md", ".rst",
    ".dockerfile", ".vue", ".svelte", ".ex", ".exs", ".erl", ".dart", ".r",
    ".pl", ".pm", ".bat", ".cmd", ".j2", ".tpl",
}

# Слишком большой файл почти всегда сгенерирован (схемы, дампы, бандлы), и в
# выдаче он только шумит, а в индексации стоит дороже всего
MAX_FILE_BYTES = 300_000
# Сгенерированное и минифицированное — по имени. Такой код не пишут руками,
# и «где реализовано X» на него указывать не должен
GENERATED_MARKERS = (
    ".min.js", ".min.css", ".pb.go", "_pb2.py", "_pb2_grpc.py", ".pb.cc", ".pb.h",
    ".generated.", "_generated.", ".g.dart", ".designer.cs", "bundle.js",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "cargo.lock",
)

# Тесты и вендорные каталоги только зашумляют выдачу: на вопрос «где
# реализовано X» первым лезет тест этого X, а не сама реализация
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    "migrations", "vendor", "third_party", "dist", "build", ".idea", ".vscode",
    # graph — наш собственный каталог с результатом, он оказывается рядом
    # с проектами, когда код лежит прямо в CODE_DIR
    "graphify-out", "site-packages", "graph",
    # каталоги тестов целиком: фильтр по имени файла их не ловит
    # (tests/__init__.py, tests/testserver/server.py), а в выдаче они
    # вытесняют саму реализацию
    "tests", "test", "testing", "e2e", "fixtures", "__tests__", "__mocks__",
    # сборка и кеши других языков: target — Maven/Cargo, obj — .NET
    "target", "obj", ".gradle", ".terraform", ".next", ".nuxt", "coverage",
    "__generated__", "generated", "Pods",
}
SKIP_NAME_PARTS = ("test_", "_test", "conftest", ".spec.", ".test.", "_spec.rb")
# Тесты по соглашениям Java/Kotlin/C#/Scala: PaymentTest.java, PaymentIT.kt
TEST_SUFFIXES = ("Test", "Tests", "IT", "Spec")
TEST_LANG_EXT = {".java", ".kt", ".scala", ".groovy", ".cs"}

MAX_CHUNK_CHARS = 4000


def _skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    name = path.name.lower()
    if any(marker in name for marker in SKIP_NAME_PARTS):
        return True
    return path.suffix in TEST_LANG_EXT and path.stem.endswith(TEST_SUFFIXES)


def _generated(path: Path, source: str) -> bool:
    name = path.name.lower()
    if any(marker in name for marker in GENERATED_MARKERS):
        return True
    # Минифицированное без говорящего имени: строки по сотни символов
    lines = source.splitlines() or [""]
    return len(source) > 5000 and len(source) / len(lines) > 300


def _signature(node: ast.AST) -> str:
    """Сигнатура функции без тела — она несёт смысл, а тело часто длинное."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [a.arg for a in node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(b) for b in node.bases]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    return ""


def _python_chunks(path: Path, rel: str, repo: str, source: str) -> list[dict]:
    """Функции и классы файла отдельными чанками."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        log.warning("не разобрался %s: %s", rel, e)
        return []

    chunks = []
    lines = source.splitlines()

    def emit(node, owner: str = "") -> None:
        body = ast.get_source_segment(source, node) or ""
        if not body.strip():
            return
        symbol = f"{owner}.{node.name}" if owner else node.name
        chunks.append({
            "symbol": symbol,
            "kind": type(node).__name__.replace("Def", "").lower(),
            "signature": _signature(node),
            "doc": (ast.get_docstring(node) or "").strip()[:300],
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", node.lineno),
            "text": body[:MAX_CHUNK_CHARS],
        })

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emit(node)
        elif isinstance(node, ast.ClassDef):
            # Класс целиком плюс методы по отдельности: вопрос бывает и про
            # назначение класса, и про конкретный метод
            emit(node)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit(sub, owner=node.name)

    # Модуль без функций (конфиг, константы) тоже иногда нужен
    if not chunks and len(lines) > 3:
        chunks.append({
            "symbol": path.stem,
            "kind": "module",
            "signature": "",
            "doc": (ast.get_docstring(tree) or "").strip()[:300],
            "line_start": 1,
            "line_end": len(lines),
            "text": source[:MAX_CHUNK_CHARS],
        })
    return chunks


def _text_chunks(path: Path, source: str) -> list[dict]:
    """Инфраструктурные файлы: целиком либо кусками, если длинные."""
    lines = source.splitlines()
    out = []
    step = 120
    for start in range(0, len(lines), step):
        piece = "\n".join(lines[start : start + step])
        if not piece.strip():
            continue
        out.append({
            "symbol": path.name,
            "kind": "config",
            "signature": "",
            "doc": "",
            "line_start": start + 1,
            "line_end": min(start + step, len(lines)),
            "text": piece[:MAX_CHUNK_CHARS],
        })
    return out


def find_jenkins_steps(repo_dirs: list[Path]) -> set[str]:
    """Имена шагов: vars/abActions.groovy -> abActions, во всех репозиториях."""
    steps = set()
    for repo_dir in repo_dirs:
        for dirpath, dirnames, filenames in os.walk(repo_dir, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if Path(dirpath).name == "vars":
                steps.update(Path(f).stem for f in filenames if f.endswith(".groovy"))
    return steps


def collect(root: Path) -> list[dict]:
    """Все чанки всех репозиториев внутри root.

    По ходу печатает, что взято, а что отброшено. Молчаливый обход слишком
    легко принять за поломку: фильтр по типам файлов узкий, и проект, где
    лежат одни .ps1, даёт ноль чанков — по виду неотличимо от «не зашёл в
    подкаталоги».
    """
    items = []
    # Расширения, отброшенные по типу: подсказка, что дописать в TEXT_SUFFIXES
    skipped_ext: dict[str, int] = {}
    # Сколько файлов какого языка разрезано на функции — видно, что работает
    by_lang: dict[str, int] = {}
    # Скрытые каталоги — это недокачанные клоны repos/sync.py (.имя.partial),
    # graph — общий граф code-graph: ни то ни другое не репозиторий
    repo_dirs = [
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "graph"
    ]
    # Шаги общей библиотеки Jenkins — до обхода: пайплайн проекта может
    # оказаться раньше библиотеки, а его сводке нужны имена всех шагов
    code_chunks.KNOWN_STEPS = find_jenkins_steps(repo_dirs)
    if code_chunks.KNOWN_STEPS:
        print(f"Шагов общей библиотеки Jenkins: {len(code_chunks.KNOWN_STEPS)}")
    for repo_dir in sorted(repo_dirs):
        repo = repo_dir.name
        seen = taken = by_name = generated = 0
        before = len(items)
        # os.walk, а не rglob: в репозиториях встречаются симлинки на
        # несуществующие цели (тесты с сертификатами, подмодули). Windows даёт
        # на них WinError 1920, и rglob обрывает обход всего репозитория.
        # onerror здесь молча пропускает такие каталоги
        for dirpath, dirnames, filenames in os.walk(repo_dir, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                path = Path(dirpath) / filename
                if _skip(path.relative_to(repo_dir)):
                    by_name += 1
                    continue
                seen += 1

                is_py = path.suffix == ".py"
                lang = code_chunks.language_for(path)
                is_text = (
                    path.name in TEXT_FILES
                    or path.suffix.lower() in TEXT_SUFFIXES
                    or path.name.startswith("Dockerfile")
                )
                if not (is_py or lang or is_text):
                    ext = path.suffix.lower() or "(без расширения)"
                    skipped_ext[ext] = skipped_ext.get(ext, 0) + 1
                    continue

                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        generated += 1
                        continue
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not source.strip():
                    continue
                if _generated(path, source):
                    generated += 1
                    continue

                rel = str(path.relative_to(repo_dir)).replace("\\", "/")
                try:
                    if is_py:
                        found = _python_chunks(path, rel, repo, source)
                        lang_name = "python"
                    elif lang:
                        found = code_chunks.chunks(path, source, MAX_CHUNK_CHARS)
                        lang_name = lang[0]
                        # Разборщика нет или он не справился — не теряем файл
                        if found is None:
                            found = _text_chunks(path, source)
                            lang_name = f"{lang[0]} (текстом)"
                    else:
                        found = _text_chunks(path, source)
                        lang_name = "текст"
                # Один странный файл (глубокая вложенность -> RecursionError,
                # сбой разборщика) не должен ронять индексацию всего кода:
                # такой файл идёт кусками текста
                except Exception as e:
                    log.warning("не разобрался %s/%s (%s), беру текстом", repo, rel, type(e).__name__)
                    found = _text_chunks(path, source)
                    lang_name = "не разобрано (текстом)"
                by_lang[lang_name] = by_lang.get(lang_name, 0) + 1
                taken += 1
                for chunk in found:
                    chunk["repo"] = repo
                    chunk["path"] = rel
                    items.append(chunk)

        note = f", отброшено как тесты: {by_name}" if by_name else ""
        if generated:
            note += f", сгенерированных и огромных: {generated}"
        print(
            f"--- {repo}: подходящих файлов {taken} из {seen}{note}, "
            f"чанков {len(items) - before}"
        )

    if by_lang:
        print("\nФайлов по языкам:")
        for name, count in sorted(by_lang.items(), key=lambda kv: -kv[1]):
            print(f"  {name}: {count}")

    if skipped_ext:
        top = sorted(skipped_ext.items(), key=lambda kv: -kv[1])[:12]
        print("\nПропущено по типу файла (в индекс не попало):")
        for ext, count in top:
            print(f"  {ext}: {count}")
        print("Нужные типы дописать в TEXT_SUFFIXES в kb/code_index.py.")
    return items


def embed_text(chunk: dict) -> str:
    """Что уходит в вектор: шапка с контекстом плюс сам код.

    Без шапки метод `_check` неотличим от сотни других `_check` в базе.
    """
    head = f"{chunk['repo']}/{chunk['path']} :: {chunk['symbol']}"
    parts = [head]
    if chunk["signature"]:
        parts.append(chunk["signature"])
    if chunk["doc"]:
        parts.append(chunk["doc"])
    parts.append(chunk["text"])
    return "\n".join(parts)


# Сколько символов уходит в ВЕКТОР (в выдаче код остаётся целиком). Шлюз
# отдаёт bge-m3 с окном меньше заявленного, и кусок в 4000 символов кода
# получал 400 «input length exceeds the context length» — прогон релиза
# падал через час работы. Тот же лимит, что у документов (kb/doc_index.py)
EMBED_MAX_CHARS = int(os.getenv("KB_EMBED_MAX_CHARS", "3000"))
# Кусок, который и после урезания не влез, режется вдвое, но не короче
MIN_EMBED_CHARS = 300
# Сетевой сбой или перезапуск модели — повтор с паузой 5, 10, 20, 40 с
EMBED_RETRIES = 5


def _too_long(e: Exception) -> bool:
    text = str(e).lower()
    return "context length" in text or "input length" in text or "too long" in text


def _embed_retrying(texts: list[str], embed=embed_batch, sleep=time.sleep) -> list:
    """embed с повторами при сбоях. Ошибка длины не повторяется — сразу наверх."""
    for attempt in range(EMBED_RETRIES):
        try:
            return embed(texts)
        except EmbedError as e:
            if _too_long(e) or attempt == EMBED_RETRIES - 1:
                raise
            wait = min(60, 5 * 2 ** attempt)
            log.warning("эмбеддер: %s — повтор через %d с", str(e)[:160], wait)
            sleep(wait)
    raise AssertionError("недостижимо")


def embed_safe(texts: list[str], embed=embed_batch, sleep=time.sleep) -> list:
    """Векторы для пачки: урезать до EMBED_MAX_CHARS, повторять при сбоях, а
    если эмбеддер всё равно отказал по длине — по одному, укорачивая вдвое
    тот, что не влез. Один длинный кусок не роняет многочасовой прогон."""
    texts = [t[:EMBED_MAX_CHARS] for t in texts]
    try:
        return _embed_retrying(texts, embed, sleep)
    except EmbedError as e:
        if not _too_long(e):
            raise
    out = []
    for text in texts:
        while True:
            try:
                out.append(_embed_retrying([text], embed, sleep)[0])
                break
            except EmbedError as e:
                if not _too_long(e) or len(text) <= MIN_EMBED_CHARS:
                    raise
                text = text[: len(text) // 2]
                log.warning("кусок не влез в окно эмбеддера — укорочен до %d символов", len(text))
    return out


# Релиз — 1.6 млн кусков: векторы в памяти заняли бы ~6.5 ГБ, и Qdrant на
# прогоне обрывал соединение посреди upsert. Для таких коллекций векторы
# лежат на диске, а в памяти — сжатая int8-копия (~1 КБ на кусок вместо 4):
# поиск идёт по ней, точный пересчёт лучших — с диска
COMPACT_COLLECTIONS = {CB_COLLECTION}
# Qdrant перезапускается (после нехватки памяти поднимается минуты) —
# повтор записи с паузой 10, 20, 40, 80 с, а не обрыв многочасового прогона
UPSERT_RETRIES = 5


def compact(client, collection: str) -> None:
    client.update_collection(
        collection_name=collection,
        vectors_config={config.DENSE_VECTOR: models.VectorParamsDiff(on_disk=True)},
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8, quantile=0.99, always_ram=True,
            )
        ),
    )


def upsert_retrying(client, sleep=time.sleep, **kwargs) -> None:
    for attempt in range(UPSERT_RETRIES):
        try:
            client.upsert(**kwargs)
            return
        # qdrant_client оборачивает сетевые ошибки по-разному в разных версиях
        # (ResponseHandlingException, httpx.*): ловим всё и повторяем
        except Exception as e:
            if attempt == UPSERT_RETRIES - 1:
                raise
            wait = min(120, 10 * 2 ** attempt)
            log.warning("Qdrant: %s — повтор записи через %d с", str(e)[:160], wait)
            sleep(wait)


def existing_ids(client, collection: str) -> set[str]:
    """Идентификаторы уже записанных кусков — для --resume."""
    ids: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection, limit=10000, offset=offset,
            with_payload=False, with_vectors=False,
        )
        ids.update(str(p.id) for p in points)
        if offset is None:
            return ids


def point_id(chunk: dict) -> str:
    seed = f"{chunk['repo']}|{chunk['path']}|{chunk['symbol']}|{chunk['line_start']}"
    return str(uuid.UUID(hashlib.md5(seed.encode()).hexdigest()))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx рапортует о каждом запросе; при индексации это сотни строк,
    # среди которых теряется собственный вывод
    logging.getLogger("httpx").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Индексация кода в Qdrant")
    ap.add_argument("root", help="каталог с репозиториями")
    ap.add_argument("--recreate", action="store_true", help="пересоздать коллекцию")
    ap.add_argument(
        "--resume", action="store_true",
        help="продолжить оборванную индексацию: уже записанные куски пропустить",
    )
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument(
        # Только коды: опечатка вроде knowledge с --recreate снесла бы документы
        "--collection", default=CODE_COLLECTION, choices=[CODE_COLLECTION, CB_COLLECTION],
        help=f"коллекция Qdrant: {CODE_COLLECTION} (по умолчанию) или {CB_COLLECTION} для релиза",
    )
    args = ap.parse_args()

    if args.recreate and args.resume:
        print("--recreate и --resume вместе нельзя: продолжать нечего, если коллекцию стереть")
        return 1

    root = Path(args.root)
    if not root.is_dir():
        print(f"Не каталог: {root}")
        return 1

    # Репозитории могут лежать и прямо в указанной папке, и во вложенной
    # repos/ (туда их кладёт клонирование). Поддерживаем оба варианта, чтобы
    # можно было просто показать каталог с кодом, ничего не перекладывая
    if (root / "repos").is_dir():
        root = root / "repos"
    print(f"Каталог с репозиториями: {root}")

    client = QdrantClient(url=config.QDRANT_URL, timeout=120)

    exists = client.collection_exists(args.collection)
    if args.recreate and exists:
        client.delete_collection(args.collection)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=args.collection,
            vectors_config={
                config.DENSE_VECTOR: models.VectorParams(
                    size=config.EMBED_DIM, distance=models.Distance.COSINE
                )
            },
        )
        for field in ("repo", "path", "symbol", "kind"):
            client.create_payload_index(
                collection_name=args.collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        print(f"Коллекция {args.collection} создана")
    if args.collection in COMPACT_COLLECTIONS:
        # И для новой, и для уже заполненной: настройка применяется на ходу,
        # записанное не теряется, повторный вызов ничего не меняет
        compact(client, args.collection)
        print(f"Коллекция {args.collection}: векторы на диске, в памяти сжатая копия")

    chunks = collect(root)
    print(f"Найдено чанков: {len(chunks)}")
    if not chunks:
        return 0
    if args.resume:
        # id детерминирован (репозиторий, путь, символ, строка): тот же кусок
        # даёт тот же id, и записанное в оборванном прогоне узнаётся
        done = existing_ids(client, args.collection)
        chunks = [c for c in chunks if point_id(c) not in done]
        print(f"Уже в коллекции: {len(done)}, осталось записать: {len(chunks)}")

    written = 0
    for start in range(0, len(chunks), args.batch):
        batch = chunks[start : start + args.batch]
        vectors = embed_safe([embed_text(c) for c in batch])

        upsert_retrying(
            client,
            collection_name=args.collection,
            points=[
                models.PointStruct(
                    id=point_id(c),
                    vector={config.DENSE_VECTOR: v},
                    payload=c,
                )
                for c, v in zip(batch, vectors)
            ],
            wait=True,
        )
        written += len(batch)
        print(f"  {written}/{len(chunks)}", end="\r")

    info = client.get_collection(args.collection)
    print(f"\nЗаписано: {written}, всего в коллекции: {info.points_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
