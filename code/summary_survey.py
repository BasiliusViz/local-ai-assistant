"""Сколько вызовов модели уйдёт на пересказ репозиториев по папкам (guides/PLAN-CB-SUMMARY.md).

Модель не вызывается. Что модель увидит из файла:
  - маленький файл (до --small байт) — целиком: Jenkins-скрипт из двух шагов по
    сигнатурам не понять, а текст его дешёвый;
  - большой — скелет из графа (сигнатуры и строки узлов с этим source_file);
    нет узлов (Graphify не разобрал) — первые HEAD_CHARS символов.
Файлы берутся с диска, поэтому учитываются и те, для которых в графе узлов нет
(Jenkinsfile, .sh, .yaml). Без каталога с клонами (только --db) — одни скелеты.

Папки сливаются по объёму входа: папка, у которой (вместе с уже слитыми в неё
подпапками) меньше порога тыс. токенов, уходит в родительскую целиком — её текст
не теряется, а читается в вызове родителя. Корень репозитория — всегда отдельный
пересказ, так что даже репозиторий из двух скриптов получает свой.

    python summary_survey.py <каталог с клонами>                 # обычный код
    python summary_survey.py <каталог с клонами> --db graph.sqlite  # релиз CB18.5
    python summary_survey.py <каталог> --thresholds 1,2,4 --top 15

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path, PurePosixPath

from graph_store import find_graphs, read_graph

CHARS_PER_TOKEN = 3.5      # грубо, смесь кода и английского
LINE_OVERHEAD = 12         # отступ, тип узла, номер строки в строке скелета
HEAD_CHARS = 2000          # большой файл без узлов в графе — сколько показать с начала
MAX_FILE = 5 << 20         # больше — сгенерированное или данные, не читаем
SKIP_DIRS = {".git", "graphify-out", "node_modules", "vendor", "dist", "build", "target",
             "out", "bin", "obj", ".idea", ".vscode", "__pycache__", ".gradle", ".mvn", ".venv", "venv"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz", ".tgz", ".jar",
            ".war", ".class", ".so", ".dll", ".exe", ".bin", ".woff", ".woff2", ".ttf", ".eot",
            ".lock", ".sum", ".min.js", ".map", ".mp4", ".mp3", ".xlsx", ".docx", ".pyc"}


def rel_path(source_file: str, repo: str) -> str:
    """source_file -> путь от корня репозитория (Graphify пишет и абсолютные, и относительные)."""
    p = source_file.replace("\\", "/")
    marker = f"/{repo}/"
    # метку ищем только в абсолютном пути: в относительном src/requests/x.py у
    # репозитория requests это его же подпапка
    if (p.startswith("/") or p[1:3] == ":/") and marker in p:
        p = p.rsplit(marker, 1)[1]
    elif p.startswith(repo + "/"):
        p = p[len(repo) + 1:]
    if p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def parent_of(path: str) -> str:
    p = str(PurePosixPath(path).parent)
    return "" if p == "." else p


def folder_units(weights: dict[str, float], threshold: float,
                 max_weight: float | None = None) -> tuple[int, int]:
    """(пересказов папок, из них больше max_weight) — снизу вверх, мелкие сливаются в родителя.

    weights — {папка: вес файлов прямо в ней}; "" — корень.
    """
    total = dict(weights)
    for folder in list(weights):           # у каждой папки должны быть все предки
        while folder:
            folder = parent_of(folder)
            total.setdefault(folder, 0)
    units = over = 0
    # глубокие первыми: к родителю приходит уже всё, что слилось снизу
    for folder in sorted(total, key=lambda f: -f.count("/") if f else 1):
        if folder and total[folder] < threshold:
            total[parent_of(folder)] += total[folder]
            continue
        units += 1
        if max_weight is not None and total[folder] > max_weight:
            over += 1
    return units, over


def disk_files(repo_dir: Path) -> dict[str, int]:
    """{путь от корня: размер} текстовых файлов клона."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            low = name.lower()
            if any(low.endswith(e) for e in SKIP_EXT):
                continue
            full = Path(dirpath) / name
            try:
                size = full.stat().st_size
                if size == 0 or size > MAX_FILE:
                    continue
                with open(full, "rb") as f:
                    if b"\0" in f.read(1024):
                        continue
            except OSError:
                continue
            out[full.relative_to(repo_dir).as_posix()] = size
    return out


def survey(nodes, repo: str, files_on_disk: dict[str, int] | None, small: int) -> dict:
    """nodes — итератор (source_file, label, loc)."""
    skel: dict[str, int] = {}
    n = 0
    for sf, label, loc in nodes:
        if not sf:
            continue
        path = rel_path(str(sf), repo)
        skel[path] = skel.get(path, 0) + len(label or "") + len(loc or "") + LINE_OVERHEAD
        n += 1
    sizes = files_on_disk if files_on_disk is not None else {}
    weights: dict[str, float] = {}
    whole = no_nodes = 0
    paths = set(sizes) | set(skel)
    for path in paths:
        size = sizes.get(path)
        if size is not None and size <= small:
            chars = size
            whole += 1
        elif path in skel:
            chars = skel[path]
        else:
            chars = HEAD_CHARS
        if size is not None and path not in skel:
            no_nodes += 1
        folder = parent_of(path)
        weights[folder] = weights.get(folder, 0) + chars / CHARS_PER_TOKEN / 1000
    return {"nodes": n, "files": len(paths), "whole": whole,
            "no_nodes": no_nodes, "folders": len(weights), "weights": weights,
            "ktok": sum(weights.values())}


def nodes_from_graph_json(path: Path):
    for ev in read_graph(path):
        if ev[0] == "node":
            d = ev[1]
            yield d.get("source_file"), str(d.get("label") or ""), d.get("source_location")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", type=Path, help="каталог с клонами (<репо>/graphify-out/graph.json)")
    ap.add_argument("--db", type=Path, help="graph.sqlite релиза вместо graph.json")
    ap.add_argument("--thresholds", default="1,2,4",
                    help="порог слияния папок, тыс. токенов входа (по умолчанию 1,2,4)")
    ap.add_argument("--small", type=int, default=8000, help="файл до стольких байт модель читает целиком")
    ap.add_argument("--max-ktok", type=float, default=24,
                    help="вход одного вызова больше этого — папку придётся дробить (по умолчанию 24)")
    ap.add_argument("--top", type=int, default=20, help="сколько крупнейших репозиториев показать")
    a = ap.parse_args(argv)
    if not a.root and not a.db:
        ap.error("нужен каталог или --db")
    ths = [float(x) for x in a.thresholds.split(",") if x.strip()]

    def disk(repo):
        d = a.root / repo if a.root else None
        return disk_files(d) if d and d.is_dir() else None

    def source():
        if a.db:
            con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
            repos = con.execute("SELECT rid, name FROM repos WHERE name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name").fetchall()
            for rid, name in repos:
                rows = con.execute("SELECT source_file, label, loc FROM nodes WHERE rid=?", (rid,))
                yield name, survey(rows, name, disk(name), a.small)
            con.close()
            return
        graphs = find_graphs(a.root)
        if not graphs:
            print(f"В {a.root} нет ни одного <репо>/graphify-out/graph.json", file=sys.stderr)
        for repo, g in graphs.items():
            try:
                yield repo, survey(nodes_from_graph_json(g), repo, disk(repo), a.small)
            except Exception as e:      # испорченный граф не должен ронять весь обзор
                print(f"  {repo}: не прочитан ({e})", file=sys.stderr)

    rows = []
    for repo, s in source():
        s["units"] = {t: folder_units(s["weights"], t, a.max_ktok) for t in ths}
        rows.append((repo, s))
        print(f"  {repo}: файлов {s['files']}, целиком {s['whole']}, без узлов {s['no_nodes']}",
              file=sys.stderr)
    if not rows:
        return 1

    rows.sort(key=lambda r: -r[1]["ktok"])
    cols = " ".join(f"{'п>=' + format(t, 'g'):>7}" for t in ths)
    head = (f"{'репозиторий':<36} {'узлов':>8} {'файлов':>7} {'целиком':>7} {'без узл':>7} "
            f"{'папок':>6} {cols} {'вход ктк':>9}")
    print(head)
    print("-" * len(head))

    def line(name, s, units):
        return (f"{name[:36]:<36} {s['nodes']:>8} {s['files']:>7} {s['whole']:>7} {s['no_nodes']:>7} "
                f"{s['folders']:>6} " + " ".join(f"{units[t][0]:>7}" for t in ths) + f" {s['ktok']:>9.0f}")

    for repo, s in rows[:a.top]:
        print(line(repo, s, s["units"]))
    if len(rows) > a.top:
        print(f"... и ещё {len(rows) - a.top}")
    print("-" * len(head))
    tot = {k: sum(s[k] for _, s in rows) for k in ("nodes", "files", "whole", "no_nodes", "folders", "ktok")}
    tot_units = {t: (sum(s["units"][t][0] for _, s in rows), sum(s["units"][t][1] for _, s in rows)) for t in ths}
    print(line(f"ИТОГО ({len(rows)} репоз.)", tot, tot_units))
    print()
    print("целиком — файлов до", a.small, "байт, модель читает их полностью; без узл — файлов, которых нет в графе.")
    print("п>=N — пересказов папок при пороге N тыс. токенов (вызовов модели; +1 на README репозитория).")
    print("вход — тыс. токенов файлов и скелетов на все папки (без пересказов подпапок).")
    for t in ths:
        calls = tot_units[t][0] + len(rows)
        print(f"  порог {t:g}: вызовов {calls}, больше {a.max_ktok:g} ктк — {tot_units[t][1]}; "
              f"при 20 с на вызов ~{calls * 20 / 3600:.1f} ч, при 60 с ~{calls / 60:.1f} ч")
    if a.root:
        jenkins_report(a.root)
    return 0


STEP_NOTE_TOKENS = 60      # строка «шаг — что делает», подмешиваемая к пайплайну


def jenkins_report(root: Path) -> None:
    """Шаги общей библиотеки и пайплайны (jenkins_graph.py): к пересказу пайплайна
    подмешиваются описания шагов, которые он зовёт, — поэтому библиотеки идут первыми."""
    try:
        import jenkins_graph
        nodes, links, stats = jenkins_graph.build(root, 0)
    except Exception as e:
        print(f"\nJenkins: не посчитано ({e})")
        return
    if not stats.get("шагов") and not stats.get("пайплайнов"):
        return
    print("\nJenkins (jenkins_graph.py):")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    lib_repos = sorted({n["source_file"].split("/", 1)[0] for n in nodes
                        if "/vars/" in "/" + n.get("source_file", "")})
    callers = {l["source"] for l in links}
    print(f"  репозиториев-библиотек (vars/): {len(lib_repos)} — их пересказывать первыми"
          + (f": {', '.join(lib_repos[:10])}" + (" ..." if len(lib_repos) > 10 else "") if lib_repos else ""))
    print(f"  добавка к входу: {len(links)} описаний шагов x ~{STEP_NOTE_TOKENS} ток. = "
          f"~{len(links) * STEP_NOTE_TOKENS / 1000:.0f} ктк на {len(callers)} вызывающих файлов")


if __name__ == "__main__":
    sys.exit(main())
