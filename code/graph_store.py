"""Графы Graphify по репозиториям -> одна база SQLite на диске.

Зачем. Граф релиза CB18.5 — 120 репозиториев, графы Graphify вместе 3.4 ГБ,
один (main) — 1.3 ГБ. Склейка `graphify merge-graphs` держит всё в networkx
(в 5–10 раз больше файла) и падает по памяти. Здесь склейки нет: каждый
graph.json читается потоково, по элементу, и ложится в свою часть базы.
Памяти нужно на один узел или связь, а не на граф.

Ключ узла — (репозиторий, id): id Graphify между репозиториями не уникальны
(cmd_main есть в каждом Go-сервисе).

    python graph_store.py build /data --db /data/graph/graph.sqlite [--jenkins]
    python graph_store.py stats --db /data/graph/graph.sqlite

build берёт <каталог>/<репо>/graphify-out/graph.json. Репозиторий, у которого
graph.json не менялся с прошлого раза, пропускается (--force — перезаписать
все); репозиторий, которого больше нет в каталоге, из базы удаляется.

Только стандартная библиотека. Читает graph_server.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

CHUNK = 1 << 20          # сколько читать из файла за раз
MAX_ITEM = 64 << 20      # элемент больше — файл испорчен: не читать его в память до конца
NUMBER_TAIL = set("0123456789.eE+-")
BATCH = 5000             # строк на один executemany
JENKINS_REPO = "_jenkins"  # связи Jenkins (jenkins_graph.py) — отдельной частью

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    rid INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    graph_size INTEGER, graph_mtime INTEGER,
    nodes INTEGER DEFAULT 0, edges INTEGER DEFAULT 0, dropped INTEGER DEFAULT 0,
    hub INTEGER DEFAULT 50,          -- степень, выше которой узел не раскрываем в обходе
    built_at TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    nid INTEGER PRIMARY KEY,
    rid INTEGER NOT NULL,
    id TEXT NOT NULL,
    label TEXT,
    name TEXT,                       -- label в нижнем регистре, без "()" и ведущей точки
    source_file TEXT,
    loc TEXT,
    file_type TEXT,
    community INTEGER,
    degree INTEGER DEFAULT 0
);
-- id первым: тот же индекс ищет и по (репозиторий, id), и по одному id
CREATE UNIQUE INDEX IF NOT EXISTS nodes_key ON nodes(id, rid);
CREATE INDEX IF NOT EXISTS nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS nodes_degree ON nodes(degree);
CREATE INDEX IF NOT EXISTS nodes_repo_degree ON nodes(rid, degree);
CREATE TABLE IF NOT EXISTS edges (
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    relation TEXT,
    confidence TEXT,
    context TEXT,
    source_file TEXT,                -- NULL, если совпадает с файлом узла-источника
    loc TEXT
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS communities (
    rid INTEGER NOT NULL,
    cid INTEGER NOT NULL,
    name TEXT,
    size INTEGER,
    PRIMARY KEY (rid, cid)
);
"""


# --- потоковое чтение node-link JSON -------------------------------------

class StreamError(ValueError):
    pass


class _Stream:
    """Буфер над файлом: raw_decode по одному значению, дочитывая по мере нужды."""

    def __init__(self, f, chunk: int = CHUNK):
        self.f = f
        self.chunk = chunk
        self.buf = ""
        self.pos = 0
        self.eof = False
        self.dec = json.JSONDecoder()

    def _more(self) -> bool:
        if self.eof:
            return False
        data = self.f.read(self.chunk)
        if not data:
            self.eof = True
            return False
        # Прочитанное выбрасываем, иначе буфер вырос бы до размера файла
        if self.pos > len(self.buf) // 2:
            self.buf = self.buf[self.pos:]
            self.pos = 0
        self.buf += data
        return True

    def peek(self) -> str:
        """Следующий значащий символ ('' — конец файла), пробелы пропускаются."""
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in " \t\r\n":
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._more():
                return ""

    def expect(self, ch: str) -> None:
        got = self.peek()
        if got != ch:
            raise StreamError(f"ожидался {ch!r}, а в файле {got!r}")
        self.pos += 1

    def value(self):
        """Одно JSON-значение целиком."""
        self.peek()
        while True:
            try:
                val, end = self.dec.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                # Незаконченный элемент — дочитать. Но не бесконечно: на битом
                # файле так ушёл бы в память весь остаток (до гигабайта)
                if len(self.buf) - self.pos > MAX_ITEM:
                    raise StreamError(f"элемент длиннее {MAX_ITEM >> 20} МБ — файл испорчен?")
                if self._more():
                    continue
                raise
            # Число или литерал на краю буфера мог быть обрезан: 12 из 123, 12 из 12.5
            cut = end >= len(self.buf) or (
                isinstance(val, (int, float)) and not isinstance(val, bool) and self.buf[end] in NUMBER_TAIL)
            if cut and not self.eof and self._more():
                continue
            self.pos = end
            return val

    def array(self):
        """Элементы массива по одному."""
        self.expect("[")
        if self.peek() == "]":
            self.pos += 1
            return
        while True:
            yield self.value()
            ch = self.peek()
            self.pos += 1
            if ch == "]":
                return
            if ch != ",":
                raise StreamError(f"в массиве ожидалась ',' или ']', а в файле {ch!r}")


def read_graph(path: Path, chunk: int = CHUNK):
    """События node-link JSON: ("node", dict), ("link", dict), ("meta", ключ, значение).

    Массивы nodes и links (или edges — так пишут некоторые версии) отдаются по
    элементу. Прочие массивы (hyperedges) тоже читаются по элементу и
    выбрасываются: их размер не должен влиять на память.
    """
    with open(path, encoding="utf-8") as f:
        s = _Stream(f, chunk)
        s.expect("{")
        if s.peek() == "}":
            return
        while True:
            key = s.value()
            s.expect(":")
            if key == "nodes" and s.peek() == "[":
                for item in s.array():
                    if isinstance(item, dict):
                        yield ("node", item)
            elif key in ("links", "edges") and s.peek() == "[":
                for item in s.array():
                    if isinstance(item, dict):
                        yield ("link", item)
            elif s.peek() == "[":
                for _ in s.array():
                    pass
            else:
                yield ("meta", key, s.value())
            ch = s.peek()
            s.pos += 1
            if ch == "}":
                return
            if ch != ",":
                raise StreamError(f"в объекте ожидалась ',' или '}}', а в файле {ch!r}")


# --- загрузка -------------------------------------------------------------

def norm_name(label: str) -> str:
    """Имя для поиска: '.auth_flow()' -> 'auth_flow', 'GetUser' -> 'getuser'."""
    name = (label or "").strip().lower()
    if name.endswith("()"):
        name = name[:-2]
    return name.lstrip(".")


def _community(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stage_path(db: Path) -> Path:
    return db.with_name(db.name + ".stage")


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    # Индекс ключа прежней версии был (rid, id) — поиск по одному id его не
    # берёт, а IF NOT EXISTS старый не заменит
    if [r[2] for r in con.execute("PRAGMA index_info('nodes_key')")][:1] == ["rid"]:
        con.execute("DROP INDEX nodes_key")
    con.executescript(SCHEMA)
    # Промежуточные таблицы загрузки: одноразовые. Остаток прошлого сбоя — долой.
    # Журнал в памяти: откат работает, а на диск не пишется
    stage_path(db).unlink(missing_ok=True)
    con.execute("ATTACH DATABASE ? AS st", (str(stage_path(db)),))
    con.execute("PRAGMA st.journal_mode=MEMORY")
    con.execute("PRAGMA st.synchronous=OFF")
    return con


def close(con: sqlite3.Connection, db: Path) -> None:
    con.execute("DETACH DATABASE st")
    con.close()
    stage_path(db).unlink(missing_ok=True)


def delete_repo(con: sqlite3.Connection, rid: int) -> None:
    con.execute("DELETE FROM edges WHERE src IN (SELECT nid FROM nodes WHERE rid=?)", (rid,))
    con.execute("DELETE FROM nodes WHERE rid=?", (rid,))
    con.execute("DELETE FROM communities WHERE rid=?", (rid,))
    con.execute("DELETE FROM repos WHERE rid=?", (rid,))


def load_items(con: sqlite3.Connection, repo: str, items, *, size: int | None = None,
               mtime: int | None = None) -> dict:
    """Записать один репозиторий из потока событий read_graph. Одна транзакция:
    упадёт посередине — в базе останется прежняя версия репозитория."""
    with con:
        row = con.execute("SELECT rid FROM repos WHERE name=?", (repo,)).fetchone()
        if row:
            delete_repo(con, row[0])
        rid = con.execute(
            "INSERT INTO repos(name, graph_size, graph_mtime) VALUES (?,?,?)", (repo, size, mtime)
        ).lastrowid
        # Связи сначала в промежуточную таблицу с текстовыми id: узлы в файле
        # могут идти и после связей, а словарь id -> nid для 1 ГБ графа — это
        # память. Таблица — в отдельном файле рядом с базой (connect): не TEMP,
        # потому что временные SQLite кладёт в /tmp контейнера, а тут гигабайты;
        # и не в самой базе — освободившиеся страницы раздули бы её в полтора раза
        con.execute("DROP TABLE IF EXISTS st._stage")
        con.execute("CREATE TABLE st._stage (src TEXT, dst TEXT, relation TEXT, confidence TEXT,"
                    " context TEXT, source_file TEXT, loc TEXT)")
        comm_names: dict[int, str] = {}
        comm_sizes: dict[int, int] = {}
        nodes, links = [], []

        def flush():
            if nodes:
                con.executemany(
                    "INSERT OR IGNORE INTO nodes(rid,id,label,name,source_file,loc,file_type,community)"
                    " VALUES (?,?,?,?,?,?,?,?)", nodes)
                nodes.clear()
            if links:
                con.executemany("INSERT INTO st._stage VALUES (?,?,?,?,?,?,?)", links)
                links.clear()

        for ev in items:
            if ev[0] == "node":
                n = ev[1]
                nid = n.get("id")
                if nid is None:
                    continue
                label = str(n.get("label") or nid)
                cid = _community(n.get("community"))
                if cid is not None:
                    comm_sizes[cid] = comm_sizes.get(cid, 0) + 1
                    if n.get("community_name") and cid not in comm_names:
                        comm_names[cid] = str(n["community_name"])
                nodes.append((rid, str(nid), label, norm_name(label), n.get("source_file"),
                              n.get("source_location"), n.get("file_type"), cid))
            elif ev[0] == "link":
                l = ev[1]
                # Настоящее направление — в _src/_tgt: старые файлы Graphify
                # хранят дугу перевёрнутой (см. graphify/serve.py, #2309)
                src = l.get("_src", l.get("source"))
                dst = l.get("_tgt", l.get("target"))
                if src is None or dst is None:
                    continue
                links.append((str(src), str(dst), l.get("relation"), l.get("confidence"),
                              l.get("context"), l.get("source_file"), l.get("source_location")))
            if len(nodes) >= BATCH or len(links) >= BATCH:
                flush()
        flush()

        staged = con.execute("SELECT count(*) FROM st._stage").fetchone()[0]
        con.execute(
            """INSERT INTO edges(src,dst,relation,confidence,context,source_file,loc)
               SELECT a.nid, b.nid, s.relation, s.confidence, s.context,
                      CASE WHEN s.source_file IS a.source_file THEN NULL ELSE s.source_file END,
                      s.loc
               FROM st._stage s
               JOIN nodes a ON a.rid=? AND a.id=s.src
               JOIN nodes b ON b.rid=? AND b.id=s.dst""", (rid, rid))
        n_edges = con.execute("SELECT changes()").fetchone()[0]
        con.execute("DROP TABLE st._stage")

        # Степень — для «главных узлов» и чтобы обход не раскрывал хабы
        con.execute("DROP TABLE IF EXISTS st._deg")
        con.execute("CREATE TABLE st._deg (nid INTEGER PRIMARY KEY, c INTEGER)")
        con.execute(
            """INSERT INTO st._deg SELECT nid, count(*) FROM (
                   SELECT e.src AS nid FROM edges e JOIN nodes n ON n.nid=e.src WHERE n.rid=?
                   UNION ALL
                   SELECT e.dst FROM edges e JOIN nodes n ON n.nid=e.dst WHERE n.rid=?
               ) GROUP BY nid""", (rid, rid))
        con.execute("UPDATE nodes SET degree=(SELECT c FROM st._deg d WHERE d.nid=nodes.nid)"
                    " WHERE nid IN (SELECT nid FROM st._deg)")
        con.execute("DROP TABLE st._deg")

        n_nodes = con.execute("SELECT count(*) FROM nodes WHERE rid=?", (rid,)).fetchone()[0]
        # Порог хаба — как у Graphify: 99-й перцентиль степени, не ниже 50
        hub = 50
        if n_nodes:
            p99 = con.execute("SELECT degree FROM nodes WHERE rid=? ORDER BY degree LIMIT 1 OFFSET ?",
                              (rid, int(n_nodes * 0.99))).fetchone()
            hub = max(50, p99[0] if p99 else 0)
        con.executemany("INSERT INTO communities(rid,cid,name,size) VALUES (?,?,?,?)",
                        [(rid, c, comm_names.get(c), sz) for c, sz in comm_sizes.items()])
        con.execute("UPDATE repos SET nodes=?, edges=?, dropped=?, hub=?, built_at=? WHERE rid=?",
                    (n_nodes, n_edges, staged - n_edges, hub, time.strftime("%Y-%m-%d %H:%M:%S"), rid))
    return {"repo": repo, "nodes": n_nodes, "edges": n_edges, "dropped": staged - n_edges}


def load_graph_file(con: sqlite3.Connection, repo: str, path: Path) -> dict:
    st = path.stat()
    return load_items(con, repo, read_graph(path), size=st.st_size, mtime=st.st_mtime_ns)


def load_jenkins(con: sqlite3.Connection, repos_dir: Path) -> dict | None:
    """Связи Jenkins (шаги общей библиотеки) — отдельной частью базы.
    Они связывают узлы только между собой, поэтому склейка не нужна."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import jenkins_graph
    nodes, links, stats = jenkins_graph.build(repos_dir, 0)
    if not nodes:
        row = con.execute("SELECT rid FROM repos WHERE name=?", (JENKINS_REPO,)).fetchone()
        if row:
            with con:
                delete_repo(con, row[0])
        return None
    items = [("node", n) for n in nodes] + [("link", l) for l in links]
    res = load_items(con, JENKINS_REPO, items)
    res["stats"] = stats
    return res


def find_graphs(root: Path) -> dict[str, Path]:
    """{репозиторий: graph.json} по каталогу с клонами."""
    out = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name.startswith(".") or d.name == "graph":
            continue
        g = d / "graphify-out" / "graph.json"
        if g.is_file():
            out[d.name] = g
    return out


def build(root: Path, db: Path, *, force: bool = False, jenkins: bool = False,
          log=print) -> int:
    graphs = find_graphs(root)
    if not graphs:
        log(f"В {root} нет ни одного <репо>/graphify-out/graph.json")
        return 1
    con = connect(db)
    t0 = time.time()
    known = {name: (rid, size, mtime) for rid, name, size, mtime in
             con.execute("SELECT rid, name, graph_size, graph_mtime FROM repos")}
    failed = 0
    for i, (repo, path) in enumerate(graphs.items(), 1):
        st = path.stat()
        old = known.get(repo)
        if not force and old and old[1] == st.st_size and old[2] == st.st_mtime_ns:
            continue
        t = time.time()
        try:
            r = load_graph_file(con, repo, path)
        except (OSError, ValueError, sqlite3.Error) as e:
            failed += 1
            log(f"    [!] {repo}: {e} — в базе осталась прежняя версия")
            continue
        extra = f", без узла на конце: {r['dropped']}" if r["dropped"] else ""
        log(f"    {i}/{len(graphs)} {repo}: узлов {r['nodes']}, связей {r['edges']}{extra}"
            f" ({st.st_size / 1e6:.0f} МБ, {time.time() - t:.0f} с)")
    gone = [n for n in known if n not in graphs and n != JENKINS_REPO]
    for repo in gone:
        with con:
            delete_repo(con, known[repo][0])
        log(f"    {repo}: графа больше нет — убран из базы")
    if jenkins:
        try:
            r = load_jenkins(con, root)
            if r:
                log(f"    связи Jenkins: " + ", ".join(f"{k} {v}" for k, v in r["stats"].items()))
        except Exception as e:  # сбой здесь не должен ронять граф
            log(f"    [!] связи Jenkins не добавлены: {e}")
    con.execute("PRAGMA optimize")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    total = con.execute("SELECT count(*), coalesce(sum(nodes),0), coalesce(sum(edges),0) FROM repos").fetchone()
    close(con, db)
    log(f"База {db}: репозиториев {total[0]}, узлов {total[1]}, связей {total[2]},"
        f" {db.stat().st_size / 1e6:.0f} МБ, {time.time() - t0:.0f} с")
    if failed:
        # Один битый граф из 120 не должен останавливать update-cb.sh:
        # остальные в базе, у битого — прежняя версия
        log(f"    [!] не загружено репозиториев: {failed} — см. выше")
    return 0 if total[0] else 1


def stats(db: Path, log=print) -> int:
    if not db.is_file():
        log(f"Нет базы {db}")
        return 1
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT name, nodes, edges, dropped, built_at FROM repos ORDER BY nodes DESC").fetchall()
    for name, n, e, d, at in rows:
        log(f"  {name}: узлов {n}, связей {e}" + (f", потеряно {d}" if d else "") + f"  [{at}]")
    log(f"Всего репозиториев {len(rows)}, узлов {sum(r[1] for r in rows)}, связей {sum(r[2] for r in rows)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Графы Graphify по репозиториям -> SQLite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="загрузить <каталог>/<репо>/graphify-out/graph.json")
    b.add_argument("root", type=Path)
    b.add_argument("--db", type=Path, required=True)
    b.add_argument("--force", action="store_true", help="перезаписать и неизменённые")
    b.add_argument("--jenkins", action="store_true", help="добавить связи Jenkins")
    s = sub.add_parser("stats", help="что в базе")
    s.add_argument("--db", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        if not args.root.is_dir():
            print(f"Нет каталога {args.root}")
            return 1
        return build(args.root, args.db, force=args.force, jenkins=args.jenkins)
    return stats(args.db)


if __name__ == "__main__":
    sys.exit(main())
