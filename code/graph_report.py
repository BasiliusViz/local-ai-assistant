"""Отчёт по базе графа релиза: что загрузилось, чего нет и почему, откуда
столько связей, отвечает ли сервер. Только чтение.

    docker compose exec -T cb-graph python /app/graph_report.py
    (снаружи — ./check-cb-graph.sh)

Работает внутри контейнера cb-graph: база создана им, и с хоста её может быть
не открыть даже на чтение (права на файлы журнала). Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "vendor", "graphify-out", "target", "build", "dist", ".gradle"}
SQL_TOP_REPOS = (
    "SELECT rid, name, nodes, edges, dropped, built_at FROM repos ORDER BY edges DESC"
)
# Считаем в Python потоком: GROUP BY на десятках миллионов строк SQLite
# сортирует во временных файлах в /tmp контейнера
SQL_RELATIONS = (
    "SELECT relation, confidence FROM edges WHERE src IN (SELECT nid FROM nodes WHERE rid = :rid)"
)
SQL_SAMPLE = (
    "SELECT e.src, e.dst, e.relation FROM edges e WHERE e.src IN "
    "(SELECT nid FROM nodes WHERE rid = :rid LIMIT 20000)"
)
SQL_HUBS = (
    "SELECT label, source_file, loc, degree FROM nodes WHERE rid = :rid ORDER BY degree DESC LIMIT 8"
)
SQL_BY_FILE = (
    "SELECT source_file FROM nodes WHERE rid = :rid"
)


def head(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def repo_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and p.name != "graph")


def file_mix(repo: Path, cap: int = 20000) -> tuple[int, str]:
    """Сколько файлов и каких (по расширению) — почему Graphify мог не построить граф."""
    exts: Counter = Counter()
    total = 0
    for _, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            total += 1
            exts[Path(f).suffix.lower() or f] += 1
            if total >= cap:
                break
        if total >= cap:
            break
    mix = ", ".join(f"{e} {n}" for e, n in exts.most_common(5))
    return total, mix + (f" (считал до {cap})" if total >= cap else "")


def mcp_call(url: str, method: str, params: dict | None = None) -> tuple[dict, float]:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})}
    req = urllib.request.Request(url, json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise RuntimeError(data["error"].get("message"))
    return data["result"], time.time() - t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Отчёт по базе графа релиза")
    ap.add_argument("--root", type=Path, default=Path("/data"), help="каталог с репозиториями")
    ap.add_argument("--db", type=Path, default=Path("/data/graph/graph.sqlite"))
    ap.add_argument("--mcp", default=f"http://127.0.0.1:{os.environ.get('MCP_PORT', '8013')}/mcp")
    ap.add_argument("--top", type=int, default=3, help="сколько крупнейших репозиториев разбирать")
    args = ap.parse_args(argv)

    head("1. База")
    if not args.db.is_file():
        print(f"[!!] базы {args.db} нет — сначала graph_store.py build")
        return 1
    for p in sorted(args.db.parent.glob(args.db.name + "*")):
        print(f"  {p.name}: {human(p.stat().st_size)}")
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    repos = con.execute(SQL_TOP_REPOS).fetchall()
    n_nodes = sum(r[2] for r in repos)
    n_edges = sum(r[3] for r in repos)
    print(f"  репозиториев {len(repos)}, узлов {n_nodes}, связей {n_edges}"
          f" ({n_edges / max(n_nodes, 1):.1f} на узел)")

    head("2. Репозитории без графа")
    in_db = {r[1] for r in repos}
    dirs = repo_dirs(args.root)
    missing = [d for d in dirs if d.name not in in_db]
    print(f"  каталогов в {args.root}: {len(dirs)}, в базе: {len(in_db & {d.name for d in dirs})},"
          f" без графа: {len(missing)}")
    for d in missing:
        g = d / "graphify-out"
        state = ("graph.json есть, но не загружен" if (g / "graph.json").is_file()
                 else "graphify-out есть, graph.json нет" if g.is_dir() else "Graphify не запускался")
        total, mix = file_mix(d)
        print(f"  {d.name}: {state}; файлов {total}: {mix}")

    head("3. Крупнейшие репозитории (по связям)")
    print(f"  {'репозиторий':30} {'узлов':>9} {'связей':>11} {'на узел':>8} {'потеряно':>9}")
    for rid, name, n, e, dropped, _ in repos[:15]:
        print(f"  {name[:30]:30} {n:>9} {e:>11} {e / max(n, 1):>8.1f} {dropped:>9}")

    for rid, name, n, e, _, _ in repos[:args.top]:
        head(f"4. Разбор: {name}")
        t = time.time()
        print("  связи по типу (relation, confidence, сколько):")
        rels = Counter(con.execute(SQL_RELATIONS, {"rid": rid}))
        for (rel, conf), k in rels.most_common(12):
            print(f"    {rel or '-':20} {conf or '-':12} {k:>11}  {k / max(e, 1):6.1%}")
        sample = con.execute(SQL_SAMPLE, {"rid": rid}).fetchall()
        if sample:
            dup = len(sample) - len(set(sample))
            print(f"  дубли в выборке {len(sample)} связей: {dup} ({dup / len(sample):.1%})")
        print("  самые связанные узлы:")
        for label, sf, loc, deg in con.execute(SQL_HUBS, {"rid": rid}):
            print(f"    {deg:>9}  {label}  [{sf}:{loc}]")
        print("  файлы с наибольшим числом узлов:")
        files = Counter(r[0] for r in con.execute(SQL_BY_FILE, {"rid": rid}))
        for sf, k in files.most_common(8):
            print(f"    {k:>9}  {sf}")
        print(f"  ({time.time() - t:.0f} с)")

    head("5. Сервер cb-graph")
    try:
        res, dt = mcp_call(args.mcp, "tools/list")
        print(f"  [ok] отвечает ({dt * 1000:.0f} мс): {', '.join(t['name'] for t in res['tools'])}")
        top = con.execute("SELECT label, rid FROM nodes WHERE loc IS NOT 'L1' AND source_file LIKE '%.%' "
                          "ORDER BY degree DESC LIMIT 1").fetchone()
        repo = next((r[1] for r in repos if r[0] == top[1]), None) if top else None
        if top:
            for tool, a in (("cb_get_neighbors", {"label": top[0], "repo": repo, "token_budget": 300}),
                            ("cb_query_graph", {"question": top[0], "repo": repo, "depth": 2, "token_budget": 300}),
                            ("cb_graph_stats", {"repo": repo})):
                res, dt = mcp_call(args.mcp, "tools/call", {"name": tool, "arguments": a})
                text = res["content"][0]["text"]
                mark = "[!!]" if res.get("isError") else "[ok]"
                print(f"  {mark} {tool} {json.dumps(a, ensure_ascii=False)} — {dt * 1000:.0f} мс")
                for line in text.splitlines()[:6]:
                    print(f"      {line[:150]}")
    except Exception as e:
        print(f"  [!!] сервер не ответил: {e}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
