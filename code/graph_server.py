"""MCP-сервер графа кода поверх базы SQLite (её строит graph_store.py).

Замена Graphify для большого графа: Graphify держит граф в памяти целиком, а
граф релиза CB18.5 (120 репозиториев) в память не влезает. Здесь каждый
запрос — несколько обращений к базе на диске.

Инструменты повторяют Graphify (graphify/serve.py 0.9.48): get_node,
get_neighbors, query_graph, shortest_path, graph_stats, god_nodes — сразу под
префиксом (cb_get_neighbors...), чтобы не путаться с общим графом code-graph.
У всех есть необязательный repo: узлы в базе хранятся по репозиториям, и
одно имя (main, Config) бывает в десятках из них.

Протокол — MCP streamable HTTP без сессий, ответ всегда JSON (не SSE):
initialize, tools/list, tools/call, ping. Только стандартная библиотека,
всё только на чтение.

    python graph_server.py --db /data/graph/graph.sqlite --port 8013 \\
        --prefix cb_ --label "РЕЛИЗ CB18.5" --name cb-graph
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_cards  # noqa: E402

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_DEPTH = 6
MAX_HOPS = 6
VISIT_CAP = 400          # узлов в ответе query_graph — дальше всё равно режет бюджет
FANOUT = 200             # соседей одного узла, которых берём в обход
PATH_CAP = 200_000       # узлов, которые shortest_path готов перебрать
NEIGHBOR_ROWS = 2000     # строк в get_neighbors до обрезки бюджетом
CANDIDATES = 50          # кандидатов на одно имя
PART_SECONDS = 3.0       # предел поиска по части имени (полный проход по узлам)
PART_STEP = 20_000       # как часто (в шагах SQLite) сверяться с пределом
MAX_LABEL = 256
MAX_BUDGET = 20_000      # потолок token_budget: ответ идёт в контекст модели
MAX_BODY = 1 << 20       # запрос MCP больше мегабайта — не наш клиент

# Запросы — постоянные строки, значения только параметрами
NODE_COLS = ("nid", "rid", "id", "label", "source_file", "loc", "file_type", "community", "degree")
SQL_NAME = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_name WHERE name = :name ORDER BY degree DESC LIMIT :lim"
)
SQL_NAME_R = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_name WHERE name = :name AND rid = :rid ORDER BY degree DESC LIMIT :lim"
)
SQL_ID = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_key WHERE id = :q ORDER BY degree DESC LIMIT :lim"
)
SQL_ID_R = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_key WHERE id = :q AND rid = :rid LIMIT :lim"
)
SQL_FILE = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_name WHERE name = :name AND (lower(source_file) = :path OR lower(source_file) LIKE :suffix ESCAPE '!') ORDER BY loc = 'L1' DESC, degree DESC LIMIT :lim"
)
SQL_FILE_R = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_name WHERE name = :name AND rid = :rid AND (lower(source_file) = :path OR lower(source_file) LIKE :suffix ESCAPE '!') ORDER BY loc = 'L1' DESC, degree DESC LIMIT :lim"
)
SQL_RELATIONS = (
    "SELECT relation, count(*) FROM edges WHERE src IN (SELECT nid FROM nodes WHERE rid = :rid) GROUP BY relation ORDER BY 2 DESC LIMIT 10"
)
SQL_PREFIX = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_name WHERE name > :name AND name < :hi ORDER BY degree DESC LIMIT :lim"
)
SQL_PREFIX_R = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes INDEXED BY nodes_name WHERE name > :name AND name < :hi AND rid = :rid ORDER BY degree DESC LIMIT :lim"
)
SQL_PART = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes WHERE name LIKE :pat ESCAPE '!' ORDER BY degree DESC LIMIT :lim"
)
SQL_PART_R = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes WHERE name LIKE :pat ESCAPE '!' AND rid = :rid ORDER BY degree DESC LIMIT :lim"
)
SQL_GOD_ALL = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes WHERE source_file IS NOT NULL AND source_file != '' AND loc IS NOT 'L1' AND label NOT LIKE '%.%' ORDER BY degree DESC LIMIT :lim"
)
SQL_GOD_REPO = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes WHERE source_file IS NOT NULL AND source_file != '' AND loc IS NOT 'L1' AND label NOT LIKE '%.%' AND rid = :rid ORDER BY degree DESC LIMIT :lim"
)
SQL_NODE = (
    "SELECT nid, rid, id, label, source_file, loc, file_type, community, degree FROM nodes WHERE nid = :nid"
)
SQL_ADJ_OUT = (
    "SELECT e.dst, n.degree FROM edges e JOIN nodes n ON n.nid = e.dst WHERE e.src = :nid LIMIT :lim"
)
SQL_ADJ_IN = (
    "SELECT e.src, n.degree FROM edges e JOIN nodes n ON n.nid = e.src WHERE e.dst = :nid LIMIT :lim"
)
SQL_OUT = (
    "SELECT dst, relation, confidence, context, source_file, loc FROM edges WHERE src = :nid AND (:rel IS NULL OR lower(relation) LIKE :rel) LIMIT :lim"
)
SQL_IN = (
    "SELECT src, relation, confidence, context, source_file, loc FROM edges WHERE dst = :nid AND (:rel IS NULL OR lower(relation) LIKE :rel) LIMIT :lim"
)

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_TERM = re.compile(r"[\w.\-/]+", re.UNICODE)
# Слова вопроса, которые не имена в коде: «кто вызывает CreateUser» -> CreateUser
STOP = set("""
a an and are as at be by call calls called caller callers calling code does do find for from
function functions graph how in is it method of on or show the this to use used uses using what
where which who why with depend depends dependency dependencies import imports
кто что где как какие какой какая зачем почему вызывает вызывают вызов вызовы вызовет
использует используют зависит зависят от из в во на по для и или это эта этот
функция функции метод методы класс код коде графе граф релиз релизе релиза найди покажи
сломается сломает если изменить поменять
""".split())


def clean(text) -> str:
    """Без управляющих символов и не длиннее MAX_LABEL: текст идёт модели."""
    if text is None:
        return ""
    text = _CTRL.sub("", str(text))
    return text[:MAX_LABEL]


def cut(lines: list[str], budget, hint: str) -> str:
    """Обрезать вывод по бюджету токенов (~3 символа на токен), как Graphify."""
    budget = max(100, min(int(budget or 2000), MAX_BUDGET))
    out = "\n".join(lines)
    limit = budget * 3
    if len(out) <= limit:
        return out
    at = out[:limit].rfind("\n")
    at = at if at > 0 else limit
    kept = out[:at]
    shown = kept.count("\n") + 1
    return (f"[!] ОБРЕЗАНО: показано {shown} из {len(lines)} строк (бюджет ~{budget} токенов). {hint}\n\n"
            + kept + f"\n... (ещё {len(lines) - shown} строк. {hint})")


class UnknownTool(Exception):
    pass


class Graph:
    """Запросы к базе. Одно соединение на поток: sqlite3 так требует."""

    def __init__(self, db: Path):
        self.db = db
        self.local = threading.local()

    # --- соединение ---

    def _open(self) -> sqlite3.Connection:
        """Соединение потока. Базу заменили целиком (другой inode) — переоткрыть."""
        try:
            st = os.stat(self.db)
        except OSError:
            raise LookupError(
                f"Граф не построен: нет базы {self.db}. На сервере: ./update-cb.sh "
                "(или docker compose exec -T cb-graph /app/sync.sh)")
        c = getattr(self.local, "con", None)
        if c is None or getattr(self.local, "ino", None) != st.st_ino:
            if c is not None:
                c.close()
            # isolation_level=None: транзакциями управляем сами (snapshot)
            c = sqlite3.connect(str(self.db), check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA query_only=ON")
            self.local.con, self.local.ino = c, st.st_ino
        return c

    @contextmanager
    def snapshot(self):
        """Один вызов инструмента — один снимок базы. Пока graph_store пишет
        новую версию репозитория, запрос видит либо старую целиком, либо новую
        (WAL), и узел, найденный первым запросом, не пропадёт ко второму."""
        c = self._open()
        c.execute("BEGIN")
        try:
            self.local.part_timeout = False
            self.local.repos = {rid: (name, hub) for rid, name, hub in
                                c.execute("SELECT rid, name, hub FROM repos")}
            yield
        finally:
            c.execute("COMMIT")

    def con(self) -> sqlite3.Connection:
        return self.local.con

    def repos(self) -> dict[int, tuple[str, int]]:
        """rid -> (имя, порог хаба) — из текущего снимка."""
        return self.local.repos

    def repo_id(self, repo: str | None) -> int | None:
        """Имя репозитория -> rid. Точное, иначе единственное по части имени."""
        if not repo:
            return None
        repos = self.repos()
        q = repo.strip().lower()
        exact = [rid for rid, (n, _) in repos.items() if n.lower() == q]
        if exact:
            return exact[0]
        part = [rid for rid, (n, _) in repos.items() if q in n.lower()]
        if len(part) == 1:
            return part[0]
        if not part:
            raise LookupError(f"Репозитория '{clean(repo)}' в графе нет. Список — {self.prefix}graph_stats.")
        names = ", ".join(sorted(repos[r][0] for r in part)[:20])
        raise LookupError(f"'{clean(repo)}' подходит к нескольким репозиториям: {names}. Уточните repo.")

    prefix = ""

    # --- карточки репозиториев (repo_cards.py) ---

    def cards(self) -> dict:
        """cards.json рядом с базой; перечитывается, когда файл заменили."""
        path = self.db.with_name("cards.json")
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            raise LookupError(
                f"Карточек репозиториев нет: нет {path}. На сервере: ./update-cb.sh --cards "
                "(или docker compose exec -T cb-graph python /app/repo_cards.py build /data "
                "--db /data/graph/graph.sqlite)")
        with self._cards_lock:
            if self._cards is None or self._cards[0] != mtime:
                self._cards = (mtime, repo_cards.load(path))
            return self._cards[1]

    _cards: tuple[int, dict] | None = None
    _cards_lock = threading.Lock()

    # --- узлы ---

    def node(self, nid: int) -> dict:
        r = self.con().execute(SQL_NODE, {"nid": nid}).fetchone()
        return self._row(r)

    def _row(self, r) -> dict:
        d: dict = dict(zip(NODE_COLS, r))
        d["repo"] = self.repos().get(d["rid"], ("?", 50))[0]
        return d

    def where(self, n: dict) -> str:
        loc = f":{n['loc']}" if n.get("loc") else ""
        return clean(f"{n['repo']}/{n.get('source_file') or ''}{loc}")

    def find(self, text: str, rid: int | None = None) -> list[list[dict]]:
        """Узлы по имени — ярусы по убыванию точности: точное имя или id,
        начало имени, часть имени. Внутри яруса — по степени."""
        c = self.con()
        q = text.strip()
        name = q.lower().removesuffix("()").lstrip(".")
        p = {"q": q, "name": name, "rid": rid, "lim": CANDIDATES}
        r = rid is not None
        exact = c.execute(SQL_ID_R if r else SQL_ID, p).fetchall()
        seen = {row[0] for row in exact}
        exact += [row for row in c.execute(SQL_NAME_R if r else SQL_NAME, p) if row[0] not in seen]
        if "/" in q:
            # Путь к файлу: узел файла подписан именем файла, путь — в source_file.
            # Фильтр по пути — в самом запросе: main.go в каждом репозитории, и
            # LIMIT до фильтра отрезал бы нужный. Часть имени для пути не ищем
            path = q.replace("\\", "/").lower()
            esc = path.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            fp = {**p, "name": path.rsplit("/", 1)[-1], "path": path, "suffix": "%/" + esc}
            rows = c.execute(SQL_FILE_R if r else SQL_FILE, fp).fetchall() or exact
            return [[self._row(row) for row in rows]]
        tiers = [exact]
        if name:
            # Диапазон по индексу вместо LIKE: LIKE 'x%' индекс не берёт. Короткое
            # начало (get, new) — сотни тысяч строк на сортировку, поэтому тоже с пределом
            tiers.append(self._timed(SQL_PREFIX_R if r else SQL_PREFIX, {**p, "hi": name + "\U0010ffff"}))
            if not tiers[0] and not tiers[1] and len(name) >= 3:
                pat = "%" + name.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
                # Часть имени — проход по всем узлам: на всём релизе это секунды
                tiers.append(self._timed(SQL_PART_R if r else SQL_PART, {**p, "pat": pat}))
        return [[self._row(r) for r in t] for t in tiers]

    def _timed(self, sql: str, params: dict) -> list:
        """Запрос не дольше PART_SECONDS; не успел — пусто и подсказка сузить."""
        c = self.con()
        deadline = time.monotonic() + PART_SECONDS
        c.set_progress_handler(lambda: time.monotonic() > deadline, PART_STEP)
        try:
            return c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            self.local.part_timeout = True
            return []
        finally:
            c.set_progress_handler(None, 0)

    def resolve(self, text: str, rid: int | None) -> tuple[dict | None, str]:
        """Один узел по имени или текст ошибки. Лучший ярус из нескольких файлов —
        неоднозначность: вернуть кандидатов, а не молча выбрать первый."""
        for tier in self.find(text, rid):
            if not tier:
                continue
            files = {}
            for n in tier:
                files.setdefault((n["rid"], n.get("source_file")), n)
            if len(files) == 1:
                return tier[0], ""
            lines = [f"Неоднозначно: '{clean(text)}' есть в {len(files)} местах"
                     + (" (показаны первые 15)" if len(files) > 15 else "") + ":"]
            for n in list(files.values())[:15]:
                lines.append(f"  {self.where(n)}  repo={clean(n['repo'])} id={clean(n['id'])}")
            lines.append("Повторите с repo=<репозиторий> и/или точным id узла.")
            return None, "\n".join(lines)
        scope = f" в репозитории {self.repos()[rid][0]}" if rid is not None else ""
        if getattr(self.local, "part_timeout", False):
            return None, (f"Узла '{clean(text)}'{scope} с таким именем или его началом нет, а поиск по "
                          "части имени по всему графу слишком долгий. Задайте repo или начало имени.")
        return None, f"Узла '{clean(text)}'{scope} нет. Попробуйте часть имени или {self.prefix}query_graph."

    def hub(self, n: dict) -> int:
        return self.repos().get(n["rid"], ("?", 50))[1]

    def edges_of(self, nid: int, limit: int, relation: str = "") -> list[tuple]:
        """(направление, соседний nid, relation, confidence, context, source_file, loc)."""
        c = self.con()
        p = {"nid": nid, "rel": f"%{relation.lower()}%" if relation else None, "lim": limit}
        out = [("out",) + r for r in c.execute(SQL_OUT, p)]
        out += [("in",) + r for r in c.execute(SQL_IN, p)]
        return out

    def adjacent(self, nid: int, limit: int, direction: str = "both") -> list[tuple[int, int]]:
        """(сосед, его степень)."""
        c = self.con()
        p = {"nid": nid, "lim": limit}
        out = []
        if direction in ("both", "out"):
            out += c.execute(SQL_ADJ_OUT, p).fetchall()
        if direction in ("both", "in"):
            out += c.execute(SQL_ADJ_IN, p).fetchall()
        return out


class Tools:
    def __init__(self, graph: Graph, prefix: str, label: str):
        self.g = graph
        self.prefix = prefix
        self.label = label
        graph.prefix = prefix

    # --- описания ---

    def list(self) -> list[dict]:
        repo = {"type": "string", "description":
                "Optional: repository name (exact or unique part). Same names exist in many repos — "
                "pass repo when the answer says 'ambiguous'"}
        budget = {"type": "integer", "default": 2000, "description": "Max output tokens"}
        tools = [
            ("query_graph",
             "Search the code graph by keywords and walk around the matches (BFS or DFS). Returns nodes "
             "and edges as text context: who calls what, what imports what.",
             {"question": {"type": "string", "description": "Function/class/file names or keywords"},
              "mode": {"type": "string", "enum": ["bfs", "dfs"], "default": "bfs",
                       "description": "bfs=broad context, dfs=trace a specific path"},
              "depth": {"type": "integer", "default": 3, "description": f"Traversal depth (1-{MAX_DEPTH})"},
              "token_budget": budget, "repo": repo},
             ["question"]),
            ("get_node", "Get full details for a specific node by label or ID: file, line, degree.",
             {"label": {"type": "string", "description": "Node label or ID to look up"}, "repo": repo},
             ["label"]),
            ("get_neighbors",
             "Get all direct neighbors of a node with edge details: '-->' what it calls/imports, "
             "'<--' who calls/imports it. Use for 'who calls X' and 'what breaks if X changes'.",
             {"label": {"type": "string"},
              "relation_filter": {"type": "string", "description": "Optional: filter by relation type, e.g. calls"},
              "token_budget": budget, "repo": repo},
             ["label"]),
            ("shortest_path",
             "Find the shortest path between two code symbols. Follows edge direction (caller -> callee) "
             "by default; set undirected=true to ignore it. Both ends must be in one repository.",
             {"source": {"type": "string", "description": "Source symbol label or ID"},
              "target": {"type": "string", "description": "Target symbol label or ID"},
              "max_hops": {"type": "integer", "default": MAX_HOPS, "description": f"Maximum hops (1-{MAX_HOPS})"},
              "undirected": {"type": "boolean", "default": False,
                             "description": "Ignore stored edge direction when searching"},
              "repo": repo},
             ["source", "target"]),
            ("graph_stats",
             "Summary: node and edge counts. Without repo — also the list of repositories with their sizes.",
             {"repo": repo}, []),
            ("god_nodes", "Return the most connected nodes - the core abstractions of the code.",
             {"top_n": {"type": "integer", "default": 10}, "repo": repo}, []),
            ("repos",
             "START HERE for general questions about the product: what parts it consists of, what a "
             "service/repository does, what it depends on and who uses it. Returns repository cards: "
             "purpose from README, kind (service/library/deploy), languages and size, main modules and "
             "functions, dependencies between repositories. Without query - the list of all repositories. "
             "With a repository name - its full card. With keywords (e.g. 'alerts', 'secrets') - the "
             "matching repositories. Then use the other tools with repo=<name> for details.",
             {"query": {"type": "string", "description":
                        "Optional: repository name or keywords. Empty - list of all repositories"},
              "token_budget": {"type": "integer", "default": 4000, "description": "Max output tokens"}},
             []),
        ]
        tag = f"[{self.label}] Code graph of {self.label} only, not of our regular code. " if self.label else ""
        return [{"name": self.prefix + name, "description": tag + desc,
                 "inputSchema": {"type": "object", "properties": props, "required": req}}
                for name, desc, props, req in tools]

    # --- вызов ---

    def call(self, name: str, args: dict) -> str:
        if name.startswith(self.prefix):
            name = name[len(self.prefix):]
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            raise UnknownTool(name)
        if not isinstance(args, dict):
            raise ValueError("arguments должны быть объектом")
        if name == "repos":  # карточки — отдельный файл, база не нужна
            return fn(args)
        with self.g.snapshot():
            return fn(args)

    def _rid(self, args):
        repo = args.get("repo")
        return self.g.repo_id(str(repo) if repo is not None else None)

    def t_get_node(self, a):
        n, err = self.g.resolve(str(a.get("label", "")), self._rid(a))
        if not n:
            return err
        comm = self.g.con().execute("SELECT name FROM communities WHERE rid=? AND cid=?",
                                    (n["rid"], n["community"])).fetchone()
        return "\n".join([
            f"Node: {clean(n['label'])}",
            f"  ID: {clean(n['id'])}",
            f"  Repo: {clean(n['repo'])}",
            f"  Source: {clean(n.get('source_file'))} {clean(n.get('loc'))}",
            f"  Type: {clean(n.get('file_type'))}",
            f"  Community: {clean(comm[0] if comm and comm[0] else n.get('community'))}",
            f"  Degree: {n['degree']}",
        ])

    def t_get_neighbors(self, a):
        n, err = self.g.resolve(str(a.get("label", "")), self._rid(a))
        if not n:
            return err
        rel = str(a.get("relation_filter") or "")
        rows = self.g.edges_of(n["nid"], NEIGHBOR_ROWS, rel)
        lines = [f"Neighbors of {clean(n['label'])} ({self.g.where(n)}, repo={clean(n['repo'])}):"]
        for direction, other, relation, conf, _, sf, loc in sorted(rows, key=lambda r: r[0] != "out"):
            m = self.g.node(other)
            arrow = "-->" if direction == "out" else "<--"
            src_file = sf if sf is not None else (n if direction == "out" else m).get("source_file")
            at = f" at={clean(src_file)}:{clean(loc)}" if loc else ""
            lines.append(f"  {arrow} {clean(m['label'])} [{clean(relation)}] [{clean(conf)}]{at}")
        if len(lines) == 1:
            lines.append("  (связей нет" + (f" с relation '{clean(rel)}'" if rel else "") + ")")
        if n["degree"] > NEIGHBOR_ROWS:
            lines.append(f"  ... всего связей {n['degree']}, показаны не все")
        return cut(lines, a.get("token_budget"),
                   f"Сузьте relation_filter или смотрите конкретный узел через {self.prefix}get_node")

    def _seeds(self, question: str, rid: int | None) -> list[dict]:
        terms = []
        for t in _TERM.findall(question):
            t = t.strip(".-/")
            if len(t) >= 3 and t.lower() not in STOP and t not in terms:
                terms.append(t)
        seeds: list[dict] = []
        for t in terms[:6]:
            for tier in self.g.find(t, rid):
                if tier:
                    # Точное имя в нескольких репозиториях — берём до трёх самых связанных
                    for n in tier[:3]:
                        if all(n["nid"] != s["nid"] for s in seeds):
                            seeds.append(n)
                    break
        return seeds[:8]

    def t_query_graph(self, a):
        question = str(a.get("question", ""))
        rid = self._rid(a)
        depth = max(1, min(int(a.get("depth") or 3), MAX_DEPTH))
        mode = "dfs" if a.get("mode") == "dfs" else "bfs"
        seeds = self._seeds(question, rid)
        if not seeds:
            return "Подходящих узлов нет. Назовите функцию, класс или файл точнее."
        seed_ids = {s["nid"] for s in seeds}
        info = {s["nid"]: s for s in seeds}
        dist = {s["nid"]: 0 for s in seeds}
        edges: set[tuple[int, int]] = set()

        def expand(nid: int) -> list[int]:
            n = info.get(nid) or self.g.node(nid)
            info[nid] = n
            if nid not in seed_ids and n["degree"] >= self.g.hub(n):
                return []  # хаб: через него не идём, иначе ответ — пол-репозитория
            out = []
            for direction, other, *_ in self.g.edges_of(nid, FANOUT):
                edges.add((nid, other) if direction == "out" else (other, nid))
                out.append(other)
            return out

        if mode == "bfs":
            frontier = [s["nid"] for s in seeds]
            for level in range(1, depth + 1):
                nxt = []
                for nid in frontier:
                    for other in expand(nid):
                        if other not in dist and len(dist) < VISIT_CAP:
                            dist[other] = level
                            nxt.append(other)
                frontier = nxt
                if not frontier:
                    break
        else:
            stack = [(s["nid"], 0) for s in reversed(seeds)]
            while stack and len(dist) < VISIT_CAP:
                nid, d = stack.pop()
                if d >= depth:
                    continue
                for other in expand(nid):
                    if other not in dist:
                        dist[other] = d + 1
                        stack.append((other, d + 1))

        for nid in dist:
            if nid not in info:
                info[nid] = self.g.node(nid)
        order = sorted(dist, key=lambda x: (dist[x], -info[x]["degree"], x))
        lines = []
        for nid in order:
            n = info[nid]
            lines.append(f"NODE {clean(n['label'])} [repo={clean(n['repo'])} src={clean(n.get('source_file'))} "
                         f"loc={clean(n.get('loc'))}]")
        rel = {}
        c = self.g.con()
        for s, d in edges:
            if s in dist and d in dist and (s, d) not in rel:
                r = c.execute("SELECT relation, confidence FROM edges WHERE src=? AND dst=? LIMIT 1", (s, d)).fetchone()
                rel[(s, d)] = r or ("", "")
        for (s, d), (relation, conf) in rel.items():
            lines.append(f"EDGE {clean(info[s]['label'])} --{clean(relation)} [{clean(conf)}]--> {clean(info[d]['label'])}")
        header = (f"Traversal: {mode.upper()} depth={depth} | Start: "
                  f"{[clean(s['label']) + ' (' + clean(s['repo']) + ')' for s in seeds]} | {len(dist)} nodes found"
                  + (f" (лимит {VISIT_CAP})" if len(dist) >= VISIT_CAP else ""))
        return header + "\n\n" + cut(lines, a.get("token_budget"),
                                     f"Сузьте вопрос, задайте repo или смотрите узел через {self.prefix}get_neighbors")

    def t_shortest_path(self, a):
        rid = self._rid(a)
        src, err = self.g.resolve(str(a.get("source", "")), rid)
        if not src:
            return "source: " + err
        dst, err = self.g.resolve(str(a.get("target", "")), rid if rid is not None else src["rid"])
        if not dst and rid is None:
            # В репозитории источника цели нет — может быть, она в другом
            dst, err = self.g.resolve(str(a.get("target", "")), None)
        if not dst:
            return "target: " + err
        if src["nid"] == dst["nid"]:
            return f"'{clean(a.get('source'))}' и '{clean(a.get('target'))}' — один и тот же узел. Уточните имена."
        if src["rid"] != dst["rid"]:
            return (f"Концы в разных репозиториях ({clean(src['repo'])}, {clean(dst['repo'])}): связей между "
                    "репозиториями в графе нет, каждый репозиторий — свой граф.")
        max_hops = max(1, min(int(a.get("max_hops") or MAX_HOPS), MAX_HOPS))
        undirected = bool(a.get("undirected"))
        fwd_dir, bwd_dir = ("both", "both") if undirected else ("out", "in")
        # Поиск с двух концов: на большом графе в разы меньше перебора. Хабы
        # (узлы-файлы, логгеры — сотни связей) могут быть точкой встречи, но
        # через них дальше не идём: иначе один шаг — пол-репозитория
        hub = self.g.hub(src)
        ends = {src["nid"], dst["nid"]}
        prev = {src["nid"]: None}
        nxt = {dst["nid"]: None}
        fa, fb = [src["nid"]], [dst["nid"]]
        meet = None
        hops = 0
        capped = skipped = False
        while fa and fb and hops < max_hops and meet is None and not capped:
            hops += 1
            grow_a = len(fa) <= len(fb)
            frontier, seen, other, direction = (fa, prev, nxt, fwd_dir) if grow_a else (fb, nxt, prev, bwd_dir)
            new = []
            for nid in frontier:
                # У концов пути соседей берём все: конец может быть хабом
                limit = PATH_CAP if nid in ends else FANOUT * 10
                for m, degree in self.g.adjacent(nid, limit, direction):
                    if m in seen:
                        continue
                    seen[m] = nid
                    if m in other:
                        meet = m
                        break
                    if degree >= hub and m not in ends:
                        skipped = True
                    else:
                        new.append(m)
                if meet is not None:
                    break
                if len(prev) + len(nxt) >= PATH_CAP:
                    capped = True
                    break
            if grow_a:
                fa = new
            else:
                fb = new
        if meet is None:
            if capped:
                return (f"Поиск прерван: перебрано {len(prev) + len(nxt)} узлов, путь не найден. "
                        "Возьмите концы ближе друг к другу или посмотрите соседей через "
                        f"{self.prefix}get_neighbors.")
            note = " (через узлы-хабы с сотнями связей путь не ищется)" if skipped else ""
            if hops >= max_hops and fa and fb:
                return f"Пути не длиннее {max_hops} шагов нет{note}."
            tail = "" if undirected else " Повторите с undirected=true — без учёта направления."
            return f"Пути от '{clean(src['label'])}' к '{clean(dst['label'])}' нет{note}.{tail}"
        path = []
        x = meet
        while x is not None:
            path.append(x)
            x = prev[x]
        path.reverse()
        x = nxt[meet]
        while x is not None:
            path.append(x)
            x = nxt[x]
        c = self.g.con()
        parts = [clean(self.g.node(path[0])["label"])]
        for u, v in zip(path, path[1:]):
            fw = c.execute("SELECT relation, confidence FROM edges WHERE src=? AND dst=?", (u, v)).fetchall()
            bw = [] if fw else c.execute("SELECT relation, confidence FROM edges WHERE src=? AND dst=?", (v, u)).fetchall()
            rows = fw or bw
            rel = "/".join(sorted({r[0] for r in rows if r[0]})) or "related"
            conf = "/".join(sorted({r[1] for r in rows if r[1]}))
            conf = f" [{clean(conf)}]" if conf else ""
            label = clean(self.g.node(v)["label"])
            parts.append(f"--{clean(rel)}{conf}--> {label}" if fw else f"<--{clean(rel)}{conf}-- {label}")
        return (f"Shortest path ({len(path) - 1} hops, repo {clean(src['repo'])}):\n  " + " ".join(parts))

    def t_graph_stats(self, a):
        c = self.g.con()
        rid = self._rid(a)
        if rid is not None:
            name, n, e, dropped, built = c.execute(
                "SELECT name, nodes, edges, dropped, built_at FROM repos WHERE rid=?", (rid,)).fetchone()
            comms = c.execute("SELECT count(*) FROM communities WHERE rid=?", (rid,)).fetchone()[0]
            # Разбивка по типам связей — проход по всем связям репозитория, у main
            # это миллионы строк: не дольше PART_SECONDS, иначе без неё
            rels = self.g._timed(SQL_RELATIONS, {"rid": rid})
            return "\n".join([f"Repo: {clean(name)} (построен {built})", f"Nodes: {n}", f"Edges: {e}",
                              f"Communities: {comms}"] + [f"  {clean(r)}: {k}" for r, k in rels]
                             + ([f"Связей без узла на конце (отброшены): {dropped}"] if dropped else []))
        rows = c.execute("SELECT name, nodes, edges FROM repos ORDER BY nodes DESC").fetchall()
        lines = [f"Repositories: {len(rows)}", f"Nodes: {sum(r[1] for r in rows)}",
                 f"Edges: {sum(r[2] for r in rows)}", "", "Репозитории (узлов / связей):"]
        lines += [f"  {clean(n)}: {k} / {e}" for n, k, e in rows]
        return cut(lines, 3000, "Задайте repo, чтобы увидеть один репозиторий")

    def t_god_nodes(self, a):
        top = max(1, min(int(a.get("top_n") or 10), 100))
        rid = self._rid(a)
        c = self.g.con()
        # Как Graphify: без узлов-файлов и узлов без файла — у них степень
        # набирается механически (contains/imports), архитектуры они не показывают
        sql = SQL_GOD_ALL if rid is None else SQL_GOD_REPO
        rows = c.execute(sql, {"rid": rid, "lim": top}).fetchall()
        lines = ["God nodes (most connected):"]
        for i, r in enumerate(rows, 1):
            n = self.g._row(r)
            lines.append(f"  {i}. {clean(n['label'])} - {n['degree']} edges ({self.g.where(n)})")
        return "\n".join(lines)

    def t_repos(self, a):
        data = self.g.cards()
        # README — чужой текст: без управляющих символов, как в остальных ответах
        text = "\n".join(repo_cards.answer(data, str(a.get("query") or "")[:500]))
        lines = [_CTRL.sub("", x) for x in text.split("\n")]
        return cut(lines, a.get("token_budget") or 4000,
                   "Задайте query — имя репозитория или слова, тогда ответ короче")


class Rpc:
    """JSON-RPC MCP: разбор одного сообщения -> ответ или None (уведомление)."""

    def __init__(self, tools: Tools, name: str):
        self.tools = tools
        self.name = name

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._err(None, -32600, "Invalid Request")
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if mid is None:
            return None  # уведомление: notifications/initialized и т.п.
        if not isinstance(params, dict):
            return self._err(mid, -32602, "params должны быть объектом")
        if method == "initialize":
            asked = params.get("protocolVersion")
            return self._ok(mid, {
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": "1.0"},
            })
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": self.tools.list()})
        if method == "tools/call":
            name = str(params.get("name", ""))
            try:
                text = self.tools.call(name, params.get("arguments") or {})
                return self._ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except UnknownTool:
                return self._err(mid, -32602, f"Unknown tool: {name}")
            except (LookupError, ValueError, TypeError) as e:
                return self._ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            except sqlite3.Error as e:
                return self._ok(mid, {"content": [{"type": "text", "text": f"Ошибка базы графа: {e}"}],
                                      "isError": True})
            except Exception as e:  # ошибка в самом сервере — ответ, а не оборванное соединение
                print(f"graph_server: {name}: {e!r}", file=sys.stderr, flush=True)
                return self._err(mid, -32603, f"Внутренняя ошибка: {e}")
        return self._err(mid, -32601, f"Method not found: {method}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def make_handler(rpc: Rpc):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self._send(413, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32600, "message": "Запрос слишком большой"}})
            try:
                msg = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}})
            if isinstance(msg, list):
                out = [r for r in (rpc.handle(m) for m in msg) if r is not None]
            else:
                out = rpc.handle(msg)
            if not out:
                return self._send(202, None)
            self._send(200, out)

        def do_GET(self):
            # Потока SSE от сервера нет. 405 — так по спецификации, клиент
            # тогда просто не слушает (а висящий GET вешает проверки)
            self._send(405, None)

        def do_DELETE(self):
            self._send(200, None)  # сессий нет — закрывать нечего

        def _send(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002 — имя как у базового класса
            pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP-сервер графа кода по базе SQLite")
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("GRAPH_DIR", "/data/graph")) / "graph.sqlite")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--prefix", default="cb_")
    ap.add_argument("--label", default="РЕЛИЗ CB18.5")
    ap.add_argument("--name", default="cb-graph")
    args = ap.parse_args()

    rpc = Rpc(Tools(Graph(args.db), args.prefix, args.label), args.name)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(rpc))
    state = "" if args.db.is_file() else " (базы пока нет — ./update-cb.sh)"
    print(f"graph_server: :{args.port}, база {args.db}{state}, префикс {args.prefix}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
