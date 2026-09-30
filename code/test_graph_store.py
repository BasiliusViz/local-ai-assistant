"""Проверка graph_store.py (потоковое чтение и загрузка) и graph_server.py
(инструменты cb_* и протокол MCP) на крошечных графах.

    python3 code/test_graph_store.py
    docker compose exec cb-graph python /app/test_graph_store.py

Только стандартная библиотека.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import graph_server as gs  # noqa: E402
import graph_store as st  # noqa: E402


def node(nid, label, src="a.go", loc="L1", community=0, **kw):
    return {"id": nid, "label": label, "source_file": src, "source_location": loc,
            "file_type": "code", "community": community, "community_name": f"c{community}", **kw}


def link(s, t, rel="calls", **kw):
    return {"source": s, "target": t, "relation": rel, "confidence": "EXTRACTED",
            "source_file": "a.go", "source_location": "L5", **kw}


# Репозиторий A: main -> handle -> save -> db; handle -> log. Хаб — узел-файл.
REPO_A = {
    "directed": False, "multigraph": False, "graph": {"name": "a", "nested": {"x": [1, 2]}},
    "nodes": [
        node("a_go", "a.go"),
        node("cmd_main", "main()", loc="L3"),
        node("handle", "Handle()", loc="L10"),
        node("save", "Save()", "store/save.go", "L7", community=1),
        node("db", "DB", "store/db.go", "L2", community=1),
        node("log", "logInfo()", loc="L20"),
    ],
    "links": [
        link("cmd_main", "handle"),
        link("handle", "save"),
        link("save", "db", "uses"),
        link("handle", "log"),
        link("a_go", "cmd_main", "contains"),
        link("ghost", "handle"),  # узла ghost нет — связь отбрасывается
    ],
    "hyperedges": [{"nodes": ["a", "b"]}, {"nodes": ["c"]}],
}
# Репозиторий B: тот же id cmd_main (id Graphify не уникальны между репозиториями);
# связи — ключом edges, одна связь в старом формате с _src/_tgt (перевёрнутая дуга)
REPO_B = {
    "directed": True,
    "nodes": [node("cmd_main", "main()", "cmd/main.go", "L1"), node("run", "Run()", "cmd/run.go", "L4")],
    "edges": [{"source": "run", "target": "cmd_main", "_src": "cmd_main", "_tgt": "run", "relation": "calls"}],
}


def write_repo(root: Path, name: str, data: dict) -> Path:
    p = root / name / "graphify-out" / "graph.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return p


class ReaderTest(unittest.TestCase):
    def test_small_chunks_same_as_json_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_repo(Path(d), "a", REPO_A)
            for chunk in (1, 3, 7, 64, 1 << 20):
                ev = list(st.read_graph(p, chunk=chunk))
                self.assertEqual([e[1] for e in ev if e[0] == "node"], REPO_A["nodes"], chunk)
                self.assertEqual([e[1] for e in ev if e[0] == "link"], REPO_A["links"], chunk)
                meta = {e[1]: e[2] for e in ev if e[0] == "meta"}
                self.assertEqual(meta, {"directed": False, "multigraph": False, "graph": REPO_A["graph"]})

    def test_edges_key_and_numbers_at_chunk_edge(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            p.write_text('{"x":12345,"edges":[{"source":"a","target":"b"}],"nodes":[],"y":true}')
            for chunk in (1, 2, 5):
                ev = list(st.read_graph(p, chunk=chunk))
                self.assertIn(("meta", "x", 12345), ev)
                self.assertIn(("meta", "y", True), ev)
                self.assertIn(("link", {"source": "a", "target": "b"}), ev)

    def test_float_cut_at_chunk_edge(self):
        """12.5 разрезано на 12 и .5 — дочитать, а не разобрать как 12."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            p.write_text('{"directed": true, "w": 12.5, "e": -1e-3, "nodes": [], "hyperedges": [3.25, 1]}')
            for chunk in range(1, 12):
                ev = list(st.read_graph(p, chunk=chunk))
                self.assertIn(("meta", "w", 12.5), ev, chunk)
                self.assertIn(("meta", "e", -1e-3), ev, chunk)

    def test_broken_element_does_not_eat_memory(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            p.write_text('{"nodes":[{"id":"a", ' + '"x": 1, ' * 5000 + '"bad" 1}, {"id":"b"}]}')
            old = st.MAX_ITEM
            st.MAX_ITEM = 1000
            try:
                with self.assertRaises(st.StreamError):
                    list(st.read_graph(p, chunk=100))
            finally:
                st.MAX_ITEM = old

    def test_broken_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            p.write_text('{"nodes":[{"id":"a"} {"id":"b"}]}')
            with self.assertRaises(ValueError):
                list(st.read_graph(p, chunk=4))

    def test_big_file_memory_stays_small(self):
        """Буфер не растёт до размера файла: прочитанное выбрасывается."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            nodes = [node(f"n{i}", f"f{i}") for i in range(20000)]
            p.write_text(json.dumps({"nodes": nodes, "links": []}))
            with open(p, encoding="utf-8") as f:
                s = st._Stream(f, chunk=4096)
                s.expect("{")
                s.value()
                s.expect(":")
                peak = 0
                for _ in s.array():
                    peak = max(peak, len(s.buf))
            self.assertLess(peak, 3 * 4096)
            self.assertGreater(p.stat().st_size, 100 * peak)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_repo(self.root, "alpha", REPO_A)
        write_repo(self.root, "beta", REPO_B)
        self.db = self.root / "graph" / "graph.sqlite"
        self.log = []
        self.assertEqual(st.build(self.root, self.db, log=self.log.append), 0)
        self.con = sqlite3.connect(self.db)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def q(self, sql, *a):
        return self.con.execute(sql, a).fetchall()

    def test_counts_and_dropped(self):
        rows = dict((r[0], r[1:]) for r in self.q("SELECT name, nodes, edges, dropped FROM repos"))
        self.assertEqual(rows["alpha"], (6, 5, 1))
        self.assertEqual(rows["beta"], (2, 1, 0))

    def test_same_id_in_two_repos(self):
        self.assertEqual(len(self.q("SELECT 1 FROM nodes WHERE id='cmd_main'")), 2)

    def test_direction_from_src_tgt(self):
        rows = self.q("""SELECT a.id, b.id FROM edges e JOIN nodes a ON a.nid=e.src JOIN nodes b ON b.nid=e.dst
                         JOIN repos r ON r.rid=a.rid WHERE r.name='beta'""")
        self.assertEqual(rows, [("cmd_main", "run")])

    def test_degree_name_community(self):
        row = self.q("SELECT degree, name FROM nodes WHERE id='handle'")[0]
        self.assertEqual(row, (3, "handle"))  # связь от ghost отброшена
        self.assertEqual(self.q("SELECT c.name, c.size FROM communities c JOIN repos r USING(rid) "
                                "WHERE r.name='alpha' ORDER BY cid"), [("c0", 4), ("c1", 2)])

    def test_edge_source_file_only_when_different(self):
        rows = self.q("SELECT e.source_file FROM edges e JOIN nodes n ON n.nid=e.src WHERE n.id='save'")
        self.assertEqual(rows, [("a.go",)])  # у save свой файл store/save.go
        rows = self.q("SELECT e.source_file FROM edges e JOIN nodes n ON n.nid=e.src WHERE n.id='handle'")
        self.assertEqual({r[0] for r in rows}, {None})

    def test_rebuild_skips_unchanged_and_drops_removed(self):
        self.log.clear()
        st.build(self.root, self.db, log=self.log.append)
        self.assertFalse(any("alpha" in l or "beta" in l for l in self.log), self.log)
        import shutil
        shutil.rmtree(self.root / "beta")
        data = dict(REPO_A, nodes=REPO_A["nodes"][:2], links=[])
        write_repo(self.root, "alpha", data)
        st.build(self.root, self.db, log=self.log.append)
        self.assertEqual(self.q("SELECT name, nodes, edges FROM repos"), [("alpha", 2, 0)])
        self.assertEqual(self.q("SELECT count(*) FROM nodes"), [(2,)])
        self.assertEqual(self.q("SELECT count(*) FROM edges"), [(0,)])

    def test_broken_repo_keeps_old_version(self):
        p = self.root / "alpha" / "graphify-out" / "graph.json"
        p.write_text('{"nodes":[{"id":"x"},', encoding="utf-8")
        # Один битый граф не валит сборку (update-cb.sh идёт дальше), но виден в логе
        self.assertEqual(st.build(self.root, self.db, log=self.log.append), 0)
        self.assertTrue(any("не загружено репозиториев: 1" in l for l in self.log), self.log)
        self.assertEqual(self.q("SELECT nodes FROM repos WHERE name='alpha'"), [(6,)])

    def test_lookups_use_indexes(self):
        """Поиск узла — в каждом вызове; полный проход по десяткам миллионов узлов недопустим."""
        p = {"q": "x", "name": "x", "hi": "y", "rid": 1, "lim": 5, "nid": 1, "path": "a/x", "suffix": "%/a/x"}
        for sql in (gs.SQL_NAME, gs.SQL_NAME_R, gs.SQL_ID, gs.SQL_ID_R, gs.SQL_PREFIX, gs.SQL_PREFIX_R,
                    gs.SQL_FILE, gs.SQL_FILE_R,
                    gs.SQL_OUT, gs.SQL_IN, gs.SQL_ADJ_OUT, gs.SQL_ADJ_IN, gs.SQL_GOD_REPO, gs.SQL_NODE):
            # запросы — константы из graph_server.py  # nosemgrep
            plan = " ".join(r[-1] for r in self.con.execute("EXPLAIN QUERY PLAN " + sql, {**p, "rel": None}))  # nosemgrep
            self.assertNotRegex(plan, r"SCAN (nodes|edges|e|n)\b(?! USING)", sql)

    def test_no_staging_tables_left(self):
        names = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(names, {"repos", "nodes", "edges", "communities"})


class ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        write_repo(root, "alpha", REPO_A)
        write_repo(root, "beta", REPO_B)
        cls.db = root / "graph" / "graph.sqlite"
        st.build(root, cls.db, log=lambda *_: None)
        cls.t = gs.Tools(gs.Graph(cls.db), "cb_", "РЕЛИЗ")

    @classmethod
    def tearDownClass(cls):
        cls.t.g.local.con.close()
        cls.tmp.cleanup()

    def call(self, name, **a):
        return self.t.call("cb_" + name, a)

    def test_list_names_and_label(self):
        tools = self.t.list()
        self.assertEqual({t["name"] for t in tools}, {"cb_query_graph", "cb_get_node", "cb_get_neighbors",
                                                      "cb_shortest_path", "cb_graph_stats", "cb_god_nodes"})
        self.assertTrue(all(t["description"].startswith("[РЕЛИЗ]") for t in tools))
        self.assertTrue(all("repo" in t["inputSchema"]["properties"] for t in tools))

    def test_get_node(self):
        out = self.call("get_node", label="Save")
        self.assertIn("store/save.go L7", out)
        self.assertIn("Repo: alpha", out)
        self.assertIn("Community: c1", out)

    def test_ambiguous_then_repo(self):
        out = self.call("get_neighbors", label="main")
        self.assertIn("Неоднозначно", out)
        self.assertIn("repo=alpha", out)
        self.assertIn("repo=beta", out)
        out = self.call("get_neighbors", label="main", repo="beta")
        self.assertIn("--> Run()", out)

    def test_neighbors_both_directions_and_filter(self):
        out = self.call("get_neighbors", label="Handle")
        self.assertIn("--> Save() [calls]", out)
        self.assertIn("<-- main() [calls]", out)
        out = self.call("get_neighbors", label="save", relation_filter="uses")
        self.assertIn("--> DB [uses]", out)
        self.assertNotIn("Handle", out)

    def test_prefix_and_substring(self):
        self.assertIn("logInfo()", self.call("get_node", label="loginf"))
        self.assertIn("logInfo()", self.call("get_node", label="ginf"))
        self.assertIn("нет", self.call("get_node", label="nothing_here"))

    def test_query_graph_depth(self):
        out = self.call("query_graph", question="кто вызывает Save", depth=1)
        self.assertIn("Start: ['Save() (alpha)']", out)
        self.assertIn("EDGE Handle() --calls [EXTRACTED]--> Save()", out)
        self.assertNotIn("main()", out)
        out = self.call("query_graph", question="Save", depth=2)
        self.assertIn("main()", out)
        out = self.call("query_graph", question="Save", depth=99)
        self.assertIn(f"depth={gs.MAX_DEPTH}", out)
        self.assertIn("Подходящих узлов нет", self.call("query_graph", question="кто вызывает"))

    def test_shortest_path(self):
        out = self.call("shortest_path", source="Handle", target="DB")
        self.assertIn("Shortest path (2 hops", out)
        self.assertIn("Handle() --calls [EXTRACTED]--> Save() --uses [EXTRACTED]--> DB", out)
        out = self.call("shortest_path", source="DB", target="Handle")
        self.assertIn("undirected=true", out)
        out = self.call("shortest_path", source="DB", target="Handle", undirected=True)
        self.assertIn("DB <--uses [EXTRACTED]-- Save() <--calls [EXTRACTED]-- Handle()", out)
        out = self.call("shortest_path", source="Handle", target="DB", max_hops=1)
        self.assertIn("не длиннее 1", out)
        out = self.call("shortest_path", source="Run", target="Handle", undirected=True)
        self.assertIn("разных репозиториях", out)

    def test_stats_and_god_nodes(self):
        out = self.call("graph_stats")
        self.assertIn("Repositories: 2", out)
        self.assertIn("alpha: 6 / 5", out)
        out = self.call("graph_stats", repo="alp")
        self.assertIn("Связей без узла на конце (отброшены): 1", out)
        out = self.call("god_nodes", top_n=1)
        self.assertIn("1. Handle() - 3 edges", out)
        self.assertNotIn("a.go -", out)

    def test_file_path_and_bad_arguments(self):
        out = self.call("get_node", label="store/save.go")
        self.assertIn("store/save.go", out)
        with self.assertRaisesRegex(LookupError, "Репозитория '123'"):  # repo не строкой — понятная ошибка
            self.call("get_node", label="save", repo=123)
        with self.assertRaises(ValueError):
            self.t.call("cb_get_node", ["label"])

    def test_file_path_among_many_same_names(self):
        """main.go в сотне каталогов: нужный путь не отрезается лимитом выборки."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            root = Path(d)
            nodes = [node(f"f{i}", "main.go", f"svc{i}/main.go", "L1") for i in range(gs.CANDIDATES * 2)]
            write_repo(root, "many", {"nodes": nodes, "links": []})
            db = root / "graph.sqlite"
            st.build(root, db, log=lambda *_: None)
            t = gs.Tools(gs.Graph(db), "cb_", "Р")
            out = t.call("cb_get_node", {"label": f"svc{gs.CANDIDATES * 2 - 1}/main.go"})
            self.assertIn(f"ID: f{gs.CANDIDATES * 2 - 1}", out)
            t.g.local.con.close()

    def test_substring_search_time_limit(self):
        old = gs.PART_SECONDS, gs.PART_STEP
        gs.PART_SECONDS, gs.PART_STEP = -1, 1  # предел уже истёк
        try:
            out = self.call("get_node", label="ginf")
            self.assertIn("слишком долгий", out)
            self.assertIn("logInfo()", self.call("get_node", label="loginfo"))  # точное имя — без предела
        finally:
            gs.PART_SECONDS, gs.PART_STEP = old

    def test_path_does_not_go_through_hubs(self):
        old = gs.Graph.hub
        gs.Graph.hub = lambda self, n: 3  # Handle (степень 3) — хаб
        try:
            out = self.call("shortest_path", source="main", target="Save", repo="alpha")
            self.assertIn("хабы", out)
            out = self.call("shortest_path", source="main", target="Handle", repo="alpha")
            self.assertIn("1 hops", out)  # хаб как конец пути — можно
        finally:
            gs.Graph.hub = old

    def test_rebuild_seen_without_restart(self):
        """Сервер читает базу, пока graph_store её пересобирает: новые данные видны
        следующему вызову, перезапуск не нужен."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            root = Path(d)
            write_repo(root, "alpha", REPO_A)
            db = root / "graph.sqlite"
            st.build(root, db, log=lambda *_: None)
            t = gs.Tools(gs.Graph(db), "cb_", "Р")
            self.assertIn("Repositories: 1", t.call("cb_graph_stats", {}))
            write_repo(root, "beta", REPO_B)
            st.build(root, db, log=lambda *_: None)
            out = t.call("cb_get_neighbors", {"label": "main", "repo": "beta"})
            self.assertIn("--> Run()", out)
            t.g.local.con.close()

    def test_unknown_repo(self):
        with self.assertRaises(LookupError):
            self.call("graph_stats", repo="gamma")


class ProtocolTest(unittest.TestCase):
    """HTTP как у Continue: initialize, уведомление, tools/list, tools/call, GET."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows: базу держат потоки сервера
        root = Path(cls.tmp.name)
        write_repo(root, "alpha", REPO_A)
        st.build(root, root / "graph.sqlite", log=lambda *_: None)
        missing = gs.Rpc(gs.Tools(gs.Graph(root / "none.sqlite"), "cb_", "Р"), "cb-graph")
        cls.missing = missing
        rpc = gs.Rpc(gs.Tools(gs.Graph(root / "graph.sqlite"), "cb_", "Р"), "cb-graph")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), gs.make_handler(rpc))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def post(self, msg):
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)

    def test_session(self):
        status, r = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["name"], "cb-graph")
        status, r = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, r), (202, None))
        _, r = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("cb_get_neighbors", [t["name"] for t in r["result"]["tools"]])
        _, r = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "cb_get_node", "arguments": {"label": "DB"}}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("store/db.go", r["result"]["content"][0]["text"])
        _, r = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "get_node", "arguments": {"label": "DB"}}})
        self.assertFalse(r["result"]["isError"])  # и без префикса
        _, r = self.post({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "cb_nope", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_get_is_405_not_hanging(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.url, timeout=5)
        self.assertEqual(e.exception.code, 405)

    def test_no_database_is_tool_error(self):
        r = self.missing.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "cb_graph_stats", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("update-cb.sh", r["result"]["content"][0]["text"])
        r = self.missing.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(r["result"]["tools"]), 6)


if __name__ == "__main__":
    unittest.main(verbosity=1)
