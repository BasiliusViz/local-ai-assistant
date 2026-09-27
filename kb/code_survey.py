"""Обзор кода перед индексацией: что возьмёт kb.code_index, что отбросит и почему.

    docker compose exec -T kb python -m kb.code_survey /cb       релиз CB18.5
    docker compose exec -T kb python -m kb.code_survey /data     обычный код

Правила те же, что у индексатора (импортируются из kb.code_index), но файлы
не режутся и никуда не пишутся — только чтение и подсчёт, быстро. Отвечает на
вопросы «почему взято 484 файла из 3436», «что мешает», «какие расширения
стоит добавить».

Разделы:
  1. итог          сколько файлов и мегабайт взято, по каким причинам отброшено
  2. расширения    по каждому: сколько взято и сколько отброшено по какой причине
  3. кандидаты     пропущенные по расширению, но на деле текстовые — их можно
                   дописать в TEXT_SUFFIXES (kb/code_index.py), если там код
  4. каталоги      какие каталоги-исключения (tests, build, vendor...) сколько
                   отсекли; где больше всего — там чаще всего чужой код
  5. репозитории   сколько взято у каждого; объём взятого — ориентир для
                   времени индексации
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from kb import code_chunks
from kb.code_index import (
    MAX_FILE_BYTES, SKIP_DIRS, TEXT_FILES, TEXT_SUFFIXES, _generated, _skip,
)

TAKEN = "взят"
REASONS = {
    "dir": "каталог-исключение",
    "test": "тест по имени",
    "ext": "неизвестное расширение",
    "big": f"больше {MAX_FILE_BYTES // 1000} КБ",
    "gen": "сгенерированный",
    "empty": "пустой",
    "err": "не прочитан",
}


def is_text(path: Path) -> bool:
    """Похоже на текст: нет нулевых байтов в начале и читается как UTF-8."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        # обрезали посреди многобайтного символа — это не повод
        return e.start >= len(head) - 4
    return True


def ext_of(path: Path) -> str:
    if path.name in TEXT_FILES or path.name.startswith("Dockerfile"):
        return path.name
    return path.suffix.lower() or "(без расширения)"


def classify(path: Path, rel: Path) -> tuple[str, int]:
    """Причина (или TAKEN) и размер файла."""
    try:
        size = path.stat().st_size
    except OSError:
        return "err", 0
    skip_dir = next((p for p in rel.parts[:-1] if p in SKIP_DIRS), None)
    if skip_dir:
        return "dir", size
    if _skip(rel):
        return "test", size
    known = (
        path.suffix == ".py"
        or code_chunks.language_for(path)
        or path.name in TEXT_FILES
        or path.suffix.lower() in TEXT_SUFFIXES
        or path.name.startswith("Dockerfile")
    )
    if not known:
        return "ext", size
    if size > MAX_FILE_BYTES:
        return "big", size
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "err", size
    if not source.strip():
        return "empty", size
    if _generated(path, source):
        return "gen", size
    return TAKEN, size


def mb(n: int) -> str:
    return f"{n / 2**20:.1f}"


def survey(root: Path, top: int) -> None:
    if (root / "repos").is_dir():
        root = root / "repos"
    repos = sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "graph"
    )

    files: Counter = Counter()          # причина -> файлов
    sizes: Counter = Counter()          # причина -> байт
    by_ext: dict[str, Counter] = defaultdict(Counter)
    ext_bytes: Counter = Counter()
    ext_text: Counter = Counter()       # неизвестное расширение, но текст
    ext_example: dict[str, str] = {}
    by_dir: Counter = Counter()         # имя каталога-исключения -> файлов
    dir_bytes: Counter = Counter()
    by_repo: dict[str, Counter] = defaultdict(Counter)
    repo_bytes: Counter = Counter()

    for repo_dir in repos:
        repo = repo_dir.name
        # без отсечения каталогов: нужно посчитать, сколько они отрезали
        for dirpath, dirnames, filenames in os.walk(repo_dir, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for filename in filenames:
                path = Path(dirpath) / filename
                rel = path.relative_to(repo_dir)
                reason, size = classify(path, rel)
                ext = ext_of(path)
                files[reason] += 1
                sizes[reason] += size
                by_ext[ext][reason] += 1
                ext_bytes[ext] += size
                by_repo[repo][reason] += 1
                if reason == TAKEN:
                    repo_bytes[repo] += size
                elif reason == "dir":
                    name = next(p for p in rel.parts[:-1] if p in SKIP_DIRS)
                    by_dir[name] += 1
                    dir_bytes[name] += size
                elif reason == "ext" and is_text(path):
                    ext_text[ext] += 1
                    ext_example.setdefault(ext, f"{repo}/{rel.as_posix()}")

    total = sum(files.values())
    print(f"Каталог: {root}, репозиториев: {len(repos)}")

    print("\n=== 1. Итог ===")
    print(f"  всего файлов: {total}, {mb(sum(sizes.values()))} МБ")
    for reason in [TAKEN, *REASONS]:
        if files[reason]:
            label = "ВЗЯТО в индекс" if reason == TAKEN else f"отброшено: {REASONS[reason]}"
            share = 100 * files[reason] / total if total else 0
            print(f"  {label:<40} {files[reason]:>8} файлов {share:5.1f}%  {mb(sizes[reason]):>9} МБ")
    if files[TAKEN]:
        # грубо: средний чанк кода 1-2 КБ
        est = sizes[TAKEN] // 1500
        print(f"\n  взятый объём {mb(sizes[TAKEN])} МБ -> порядка {est} чанков "
              f"(точно — kb.code_index collect, HAND-TEST 6б.3)")

    print(f"\n=== 2. Расширения (топ {top} по числу файлов) ===")
    cols = [TAKEN, "dir", "test", "ext", "big", "gen"]
    head = ["взят", "катал.", "тест", "расш.", "большой", "генер."]
    print(f"  {'расширение':<22}{'файлов':>8}{'МБ':>9}  " + "".join(f"{h:>9}" for h in head))
    for ext, c in sorted(by_ext.items(), key=lambda kv: -sum(kv[1].values()))[:top]:
        print(f"  {ext[:22]:<22}{sum(c.values()):>8}{mb(ext_bytes[ext]):>9}  "
              + "".join(f"{c[k] or '':>9}" for k in cols))

    print("\n=== 3. Кандидаты: пропущены по расширению, но это текст ===")
    if not ext_text:
        print("  нет — всё пропущенное по расширению бинарное")
    for ext, n in ext_text.most_common(top):
        print(f"  {ext[:22]:<22}{n:>8} файлов   пример: {ext_example[ext]}")
    if ext_text:
        print("  Если там код или настройки, которые пишут руками, — дописать в "
              "TEXT_SUFFIXES (kb/code_index.py). Данные, дампы, переводы — не нужно.")

    print("\n=== 4. Каталоги-исключения ===")
    if not by_dir:
        print("  ни один не встретился")
    for name, n in by_dir.most_common(top):
        print(f"  {name:<22}{n:>8} файлов {mb(dir_bytes[name]):>9} МБ")

    print(f"\n=== 5. Репозитории (топ {top} по взятому объёму) ===")
    print(f"  {'репозиторий':<40}{'взято':>8}{'из':>8}{'МБ взято':>10}")
    for repo, size in repo_bytes.most_common(top):
        c = by_repo[repo]
        print(f"  {repo[:40]:<40}{c[TAKEN]:>8}{sum(c.values()):>8}{mb(size):>10}")
    empty = [r for r in by_repo if not by_repo[r][TAKEN]]
    if empty:
        print(f"  ничего не взято ({len(empty)}): {', '.join(sorted(empty)[:30])}"
              + (" ..." if len(empty) > 30 else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="Обзор кода перед индексацией")
    ap.add_argument("root", help="каталог с репозиториями: /cb (релиз) или /data (обычный код)")
    ap.add_argument("--top", type=int, default=30, help="сколько строк в таблицах")
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"Не каталог: {root}")
        return 1
    survey(root, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
