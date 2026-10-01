"""Сколько вызовов модели уйдёт на пересказ репозиториев по папкам (guides/PLAN-CB-SUMMARY.md).

Модель не вызывается. Узлы графа группируются по папкам их source_file; папка,
в которой (вместе с уже слитыми в неё подпапками) меньше порога узлов, сливается
в родительскую. Корень репозитория — всегда отдельный пересказ. Итог на репозиторий:
папок-пересказов + 1 вызов на README, и примерный объём входа (скелеты сигнатур).

    python summary_survey.py <каталог с клонами>            # обычный код: <репо>/graphify-out/graph.json
    python summary_survey.py --db /data/graph/graph.sqlite  # релиз CB18.5
    python summary_survey.py <каталог> --thresholds 10,20,50 --top 15

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path, PurePosixPath

from graph_store import find_graphs, read_graph

CHARS_PER_TOKEN = 3.5      # грубо, смесь кода и английского
LINE_OVERHEAD = 12         # отступ, тип узла, номер строки в строке скелета


def rel_path(source_file: str, repo: str) -> str:
    """source_file -> путь от корня репозитория (Graphify пишет и абсолютные, и относительные)."""
    p = source_file.replace("\\", "/")
    marker = f"/{repo}/"
    if marker in p:
        p = p.rsplit(marker, 1)[1]
    elif p.startswith(repo + "/"):
        p = p[len(repo) + 1:]
    if p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def folder_units(counts: dict[str, int], threshold: int) -> int:
    """Число пересказов папок: снизу вверх, мелкие папки сливаются в родителя.

    counts — {папка: узлов в файлах прямо в ней}; "" — корень.
    """
    total = dict(counts)
    for folder in list(counts):           # у каждой папки должны быть все предки
        p = PurePosixPath(folder)
        while str(p) not in ("", "."):
            p = p.parent
            key = "" if str(p) == "." else str(p)
            total.setdefault(key, 0)
    units = 0
    # глубокие первыми: к родителю приходит уже всё, что слилось снизу
    for folder in sorted(total, key=lambda f: (-f.count("/") if f else 1, f)):
        if folder == "":
            continue
        if total[folder] >= threshold:
            units += 1
        else:
            parent = str(PurePosixPath(folder).parent)
            parent = "" if parent == "." else parent
            total[parent] += total[folder]
    return units + 1                       # корень


def survey_nodes(nodes, repo: str) -> dict:
    """nodes — итератор (source_file, label, loc)."""
    counts: dict[str, int] = {}
    files: set[str] = set()
    chars = 0
    n = 0
    for sf, label, loc in nodes:
        if not sf:
            continue
        path = rel_path(str(sf), repo)
        folder = str(PurePosixPath(path).parent)
        folder = "" if folder == "." else folder
        counts[folder] = counts.get(folder, 0) + 1
        files.add(path)
        chars += len(label or "") + len(loc or "") + LINE_OVERHEAD
        n += 1
    return {"nodes": n, "files": len(files), "folders": len(counts),
            "counts": counts, "chars": chars}


def from_graph_json(path: Path, repo: str) -> dict:
    def gen():
        for ev in read_graph(path):
            if ev[0] == "node":
                d = ev[1]
                yield d.get("source_file"), str(d.get("label") or ""), d.get("source_location")
    return survey_nodes(gen(), repo)


def from_db(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for rid, name in con.execute("SELECT rid, name FROM repos WHERE name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"):
        rows = con.execute("SELECT source_file, label, loc FROM nodes WHERE rid=?", (rid,))
        yield name, survey_nodes(rows, name)
    con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", type=Path, help="каталог с клонами (<репо>/graphify-out/graph.json)")
    ap.add_argument("--db", type=Path, help="graph.sqlite релиза вместо каталога")
    ap.add_argument("--thresholds", default="10,20,50", help="пороги слияния, узлов (по умолчанию 10,20,50)")
    ap.add_argument("--top", type=int, default=20, help="сколько крупнейших репозиториев показать")
    a = ap.parse_args(argv)
    if not a.root and not a.db:
        ap.error("нужен каталог или --db")
    ths = [int(x) for x in a.thresholds.split(",") if x.strip()]

    if a.db:
        source = from_db(a.db)
    else:
        graphs = find_graphs(a.root)
        if not graphs:
            print(f"В {a.root} нет ни одного <репо>/graphify-out/graph.json", file=sys.stderr)
            return 1
        def source_gen():
            for repo, g in graphs.items():
                try:
                    yield repo, from_graph_json(g, repo)
                except Exception as e:      # испорченный граф не должен ронять весь обзор
                    print(f"  {repo}: не прочитан ({e})", file=sys.stderr)
        source = source_gen()

    rows = []
    for repo, s in source:
        s["units"] = {t: folder_units(s["counts"], t) for t in ths}
        rows.append((repo, s))
        print(f"  {repo}: узлов {s['nodes']}, файлов {s['files']}, папок {s['folders']}", file=sys.stderr)

    rows.sort(key=lambda r: -r[1]["nodes"])
    head = f"{'репозиторий':<40} {'узлов':>9} {'файлов':>7} {'папок':>6} " + " ".join(f"{'п>=' + str(t):>7}" for t in ths) + f" {'вход, ктк':>10}"
    print(head)
    print("-" * len(head))
    for repo, s in rows[:a.top]:
        print(f"{repo[:40]:<40} {s['nodes']:>9} {s['files']:>7} {s['folders']:>6} "
              + " ".join(f"{s['units'][t]:>7}" for t in ths)
              + f" {s['chars'] / CHARS_PER_TOKEN / 1000:>10.0f}")
    if len(rows) > a.top:
        print(f"... и ещё {len(rows) - a.top}")
    print("-" * len(head))
    tot = lambda k: sum(s[k] for _, s in rows)
    print(f"{'ИТОГО (' + str(len(rows)) + ' репоз.)':<40} {tot('nodes'):>9} {tot('files'):>7} {tot('folders'):>6} "
          + " ".join(f"{sum(s['units'][t] for _, s in rows):>7}" for t in ths)
          + f" {tot('chars') / CHARS_PER_TOKEN / 1000:>10.0f}")
    print()
    print("п>=N — пересказов папок при пороге N узлов (вызовов модели; +1 на README репозитория).")
    print("вход — тыс. токенов скелетов сигнатур на все папки (без пересказов подпапок и README).")
    for t in ths:
        calls = sum(s["units"][t] for _, s in rows) + len(rows)
        print(f"  порог {t}: вызовов {calls}; при 20 с на вызов ~{calls * 20 / 3600:.1f} ч, при 60 с ~{calls / 60:.1f} ч")
    return 0


if __name__ == "__main__":
    sys.exit(main())
