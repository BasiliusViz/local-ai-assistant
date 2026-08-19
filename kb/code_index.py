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
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from kb import config
from kb.embedder import embed_batch

log = logging.getLogger(__name__)

CODE_COLLECTION = "code"

# Инфраструктурные файлы графом не разбираются, но искать по ним нужно:
# «какая джоба деплоит сервис», «от какого образа наследуемся»
TEXT_FILES = {
    "Jenkinsfile", "Dockerfile", "Makefile", "docker-compose.yml",
    "docker-compose.yaml", "requirements.txt", "pyproject.toml",
}
TEXT_SUFFIXES = {".yml", ".yaml", ".sh", ".tf", ".ini", ".cfg", ".toml"}

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
    "tests", "test", "testing", "e2e", "fixtures",
}
SKIP_NAME_PARTS = ("test_", "_test", "conftest")

MAX_CHUNK_CHARS = 4000


def _skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    name = path.name.lower()
    return any(marker in name for marker in SKIP_NAME_PARTS)


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


def collect(root: Path) -> list[dict]:
    """Все чанки всех репозиториев внутри root."""
    items = []
    for repo_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        repo = repo_dir.name
        # os.walk, а не rglob: в репозиториях встречаются симлинки на
        # несуществующие цели (тесты с сертификатами, подмодули). Windows даёт
        # на них WinError 1920, и rglob обрывает обход всего репозитория.
        # onerror здесь молча пропускает такие каталоги
        for dirpath, dirnames, filenames in os.walk(repo_dir, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                path = Path(dirpath) / filename
                if _skip(path.relative_to(repo_dir)):
                    continue

                is_py = path.suffix == ".py"
                is_text = path.name in TEXT_FILES or path.suffix in TEXT_SUFFIXES
                if not (is_py or is_text):
                    continue

                try:
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not source.strip():
                    continue

                rel = str(path.relative_to(repo_dir)).replace("\\", "/")
                found = (
                    _python_chunks(path, rel, repo, source)
                    if is_py
                    else _text_chunks(path, source)
                )
                for chunk in found:
                    chunk["repo"] = repo
                    chunk["path"] = rel
                    items.append(chunk)
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
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

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

    exists = client.collection_exists(CODE_COLLECTION)
    if args.recreate and exists:
        client.delete_collection(CODE_COLLECTION)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=CODE_COLLECTION,
            vectors_config={
                config.DENSE_VECTOR: models.VectorParams(
                    size=config.EMBED_DIM, distance=models.Distance.COSINE
                )
            },
        )
        for field in ("repo", "path", "symbol", "kind"):
            client.create_payload_index(
                collection_name=CODE_COLLECTION,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        print(f"Коллекция {CODE_COLLECTION} создана")

    chunks = collect(root)
    print(f"Найдено чанков: {len(chunks)}")
    if not chunks:
        return 0

    written = 0
    for start in range(0, len(chunks), args.batch):
        batch = chunks[start : start + args.batch]
        vectors = embed_batch([embed_text(c) for c in batch])

        client.upsert(
            collection_name=CODE_COLLECTION,
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

    info = client.get_collection(CODE_COLLECTION)
    print(f"\nЗаписано: {written}, всего в коллекции: {info.points_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
