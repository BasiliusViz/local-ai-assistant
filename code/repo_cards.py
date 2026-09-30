"""Карточки репозиториев релиза CB18.5: что это, из чего состоит, от кого зависит.

Зачем. Граф (graph.sqlite) отвечает про конкретные функции, cb_search — про
куски кода. На общий вопрос («из каких частей состоит релиз», «что делает
alert-manager», «от чего зависит vault-manager») модели не с чего начать.
Карточка — короткая справка по репозиторию, по ней модель выбирает сервис и
уже потом идёт в граф и поиск. Инструмент — cb_repos в graph_server.py.

Что в карточке:
  - что это: первые абзацы README (нет README — описание из манифеста);
  - вид: сервис (есть Dockerfile), библиотека, деплой (Helm), прочее;
  - языки и объём: файлы и строки по расширениям, без vendor и тестов;
  - модули и главные функции: из graph.sqlite (узлы по папкам, узлы с
    наибольшим числом связей);
  - зависимости внутри релиза: go.mod, pom.xml, package.json, Chart.yaml,
    requirements/pyproject — что один репозиторий объявляет, а другой
    подключает; отдельно, как слабый признак, — упоминания имени другого
    репозитория в конфигах (адрес сервиса в values.yaml, application.yml).

    python repo_cards.py build /data --db /data/graph/graph.sqlite
    python repo_cards.py show vault-manager --cards /data/graph/cards.json
    python repo_cards.py obsidian --cards /data/graph/cards.json --out /data/graph/obsidian

Карточки — cards.json рядом с базой: 81 репозиторий, файл маленький, сервер
перечитывает его сам, когда он меняется. Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

# Каталоги, которые не считаем: то же, что .graphifyignore в sync.sh
SKIP_DIRS = {
    ".git", ".github", ".idea", ".vscode", "vendor", "third_party", "node_modules", "graphify-out",
    "build", "target", "dist", "out", "bin", "obj", ".gradle", "Pods", "coverage", "__pycache__",
    ".venv", "venv", "migrations", "tests", "test", "testing", "e2e", "fixtures", "__tests__",
    "__mocks__", "generated", "__generated__", "testdata",
}
TEST_FILE = re.compile(r"(_test\.go|^test_.*\.py|_test\.py|Tests?\.(java|kt|cs)|IT\.java|"
                       r"\.(spec|test)\.[jt]sx?|\.min\.(js|css)|\.pb\.go|_pb2(_grpc)?\.py)$")
LANGS = {
    ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue", ".cs": "C#", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".sql": "SQL", ".lua": "Lua",
    ".yaml": "YAML", ".yml": "YAML", ".tpl": "Helm-шаблоны", ".tf": "Terraform",
    ".proto": "Protobuf", ".html": "HTML", ".css": "CSS", ".scss": "CSS",
}
CONFIG_LANGS = {"YAML", "Helm-шаблоны"}   # не код: в «вид» и главный язык не идут
MAX_LINES_FILE = 2 << 20                 # файл больше — строки не считаем, только файл
README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme", "readme.adoc")
SUMMARY_CHARS = 300
README_CHARS = 800
TOP_NODES = 10
TOP_MODULES = 8

# Конфиги, в которых ищем имена других репозиториев (адреса сервисов)
CONFIG_EXT = {".yaml", ".yml", ".properties", ".env", ".conf", ".toml", ".ini", ".json", ".tf"}
CONFIG_NAMES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml", "jenkinsfile", "makefile"}
CONFIG_MAX = 256 << 10
JSON_MAX = 64 << 10                      # большие .json — данные, а не настройки
CONFIG_FILES_CAP = 20000
_NAME_TOKEN = re.compile(r"[\w.-]+")    # с точкой: имена вроде cb.core-x
# Имена, которые в тексте встречаются и без отношения к репозиторию
GENERIC = {"main", "core", "common", "config", "configs", "utils", "util", "tools", "docs", "api",
           "web", "ui", "deploy", "helm", "charts", "chart", "infra", "base", "lib", "libs", "scripts",
           "server", "client", "service", "services", "app", "backend", "frontend", "proxy", "gateway",
           "auth", "monitoring", "logging", "test", "tests", "build", "release", "shared"}

KINDS = ("сервис", "библиотека", "деплой", "прочее")
NOTE_MARK = "generated: repo_cards.py"   # метка своих заметок Obsidian


# --- обход файлов ---------------------------------------------------------

def walk(root: Path):
    """Файлы репозитория без vendor, тестов и служебных каталогов."""
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            if not TEST_FILE.search(f):
                yield Path(d) / f


def count_lines(path: Path) -> int:
    try:
        if path.stat().st_size > MAX_LINES_FILE:
            return 0
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# --- README ---------------------------------------------------------------

_MD_JUNK = re.compile(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>|\[!\[.*?\)\]\(.*?\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Раздел с такими словами — уже инструкция, а не «что это»: дальше не читаем
_HOWTO = re.compile(r"^\W*\d*\W*(install|usage|getting started|quick ?start|build|requirements|config|"
                    r"develop|licen|contribut|run|deploy|установ|запуск|настро|сборк|быстрый старт|"
                    r"требован|конфигур|разработ|лиценз|развёрт|разверт|использован)", re.I)


def find_readme(root: Path) -> Path | None:
    try:
        names = {p.name.lower(): p for p in root.iterdir() if p.is_file()}
    except OSError:
        return None
    for n in README_NAMES:
        if n in names:
            return names[n]
    return None


def read_readme(path: Path) -> tuple[str, str]:
    """(заголовок, текст первых абзацев) — без картинок, бейджей, кода и таблиц."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        return "", ""
    title, paras, cur, in_code = "", [], [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        bold = s.startswith("**") and s.endswith("**") and len(s) > 4
        if s.startswith("#") or bold or (s and set(s) <= set("=-") and cur):
            if cur:
                paras.append(" ".join(cur))
                cur = []
            h = s.lstrip("#").strip("* ")
            if h and not title:
                title = h
            elif h and paras and _HOWTO.match(h):
                break
            continue
        s = _MD_LINK.sub(r"\1", _MD_JUNK.sub("", s)).strip()
        if not s or s.startswith(("|", "<!--", "[![", "---")):
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
        if sum(len(p) for p in paras) > README_CHARS:
            break
    if cur:
        paras.append(" ".join(cur))
    # Оглавление и строки из одних ссылок — не описание
    paras = [p for p in paras if len(p) > 20 and not p.lower().startswith(("table of contents", "содержание"))]
    return title, "\n\n".join(paras)[:README_CHARS]


def first_sentences(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    at = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:at + 1] if at > limit // 3 else cut.rsplit(" ", 1)[0] + "…"


# --- манифесты: что репозиторий объявляет и что подключает ----------------

def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child(el, name):
    for c in el:
        if _xml_local(c.tag) == name:
            return c
    return None


def parse_pom(path: Path) -> tuple[list[str], list[str], str]:
    """(объявленные group:artifact, подключаемые, description).

    defusedxml нельзя (только stdlib), поэтому DTD не пускаем вовсе: в pom.xml
    его не бывает, а сущности в нём — это XXE и «миллиард смешков»."""
    try:
        if path.stat().st_size > CONFIG_MAX * 4:
            return [], [], ""
        raw = path.read_bytes()
        # UTF-16 проверку по байтам обошёл бы — такой pom.xml просто не читаем
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw or b"\x00" in raw[:200]:
            return [], [], ""
        root = ET.fromstring(raw)  # nosemgrep: use-defused-xml-parse — DTD отброшен выше
    except (ET.ParseError, OSError):
        return [], [], ""

    def text(el, name):
        c = _xml_child(el, name) if el is not None else None
        return (c.text or "").strip() if c is not None and c.text else ""

    parent = _xml_child(root, "parent")
    group = text(root, "groupId") or text(parent, "groupId")
    art = text(root, "artifactId")
    own = [f"{group}:{art}"] if art else []
    uses = []
    if parent is not None and text(parent, "artifactId"):
        uses.append(f"{text(parent, 'groupId')}:{text(parent, 'artifactId')}")
    for el in root.iter():
        if _xml_local(el.tag) == "dependency":
            a = text(el, "artifactId")
            if a:
                uses.append(f"{text(el, 'groupId')}:{a}")
    return own, uses, text(root, "description")


_GO_REQ = re.compile(r"^\s*(?:require\s+)?([\w.\-~/]+\.[\w.\-~/]+|[\w\-]+/[\w.\-~/]+)\s+v[\d]", re.M)


def parse_gomod(path: Path) -> tuple[list[str], list[str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    m = re.search(r"^\s*module\s+(\S+)", text, re.M)
    own = [m.group(1).strip('"')] if m else []
    uses = [u for u in _GO_REQ.findall(text) if u not in own]
    # replace ../other-repo — прямое указание на соседний репозиторий
    uses += ["path:" + p for p in re.findall(r"=>\s*(\.\.?/[^\s]+)", text)]
    # replace upstream => git.cb/cb/fork v1.2.3 — форк из релиза
    uses += re.findall(r"=>\s*(\w[\w.\-~]*/[\w.\-~/]+)\s+v\d", text)
    return own, uses


def parse_package_json(path: Path) -> tuple[list[str], list[str], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return [], [], ""
    if not isinstance(data, dict):
        return [], [], ""
    own = [data["name"]] if isinstance(data.get("name"), str) else []
    uses = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(data.get(key), dict):
            uses += list(data[key])
    desc = data.get("description")
    return own, uses, desc if isinstance(desc, str) else ""


def parse_chart(path: Path) -> tuple[list[str], list[str], str]:
    """Chart.yaml без pyyaml: name верхнего уровня, имена в dependencies, description."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], [], ""
    own, uses, desc, in_deps = [], [], "", False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = not line[0].isspace()
        if top:
            in_deps = line.startswith("dependencies:")
            m = re.match(r"(name|description):\s*['\"]?(.*?)['\"]?\s*$", line)
            if m and m.group(1) == "name":
                own.append(m.group(2))
            elif m:
                desc = m.group(2)
        elif in_deps:
            m = re.match(r"\s*-?\s*name:\s*['\"]?([\w.\-]+)", line)
            if m:
                uses.append(m.group(1))
    return own, uses, desc


def parse_python(path: Path) -> tuple[list[str], list[str], str]:
    if path.name == "pyproject.toml":
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return [], [], ""
        proj = data.get("project") or data.get("tool", {}).get("poetry") or {}
        own = [proj["name"]] if isinstance(proj.get("name"), str) else []
        deps = proj.get("dependencies") or []
        deps = list(deps) if isinstance(deps, (list, dict)) else []
        uses = [re.split(r"[<>=!~\[; ]", str(d), 1)[0] for d in deps]
        return own, [u for u in uses if u], str(proj.get("description") or "")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], ""
    uses = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            uses.append(re.split(r"[<>=!~\[; ]", line, 1)[0])
    return [], [u for u in uses if u], ""


def is_dockerfile(low: str) -> bool:
    return low == "dockerfile" or low.startswith("dockerfile.") or low.endswith(".dockerfile")


def new_manifests() -> dict:
    # uses: строка или (path:.., папка) — replace ../other в go.mod
    return {"own": {}, "uses": {}, "desc": "", "desc_depth": 99, "has": set()}


def read_manifest(p: Path, acc: dict, depth: int) -> None:
    """Файл-манифест -> что репозиторий объявляет (own) и что подключает (uses)."""
    f, low = p.name, p.name.lower()
    if is_dockerfile(low):
        acc["has"].add("dockerfile")
        return
    kind, o, u, ds = None, [], [], ""
    if f == "go.mod":
        kind = "go.mod"
        o, u = parse_gomod(p)
    elif f == "pom.xml":
        kind = "pom.xml"
        o, u, ds = parse_pom(p)
    elif f == "package.json":
        kind = "package.json"
        o, u, ds = parse_package_json(p)
    elif f == "Chart.yaml":
        kind = "Chart.yaml"
        o, u, ds = parse_chart(p)
    elif f == "pyproject.toml" or (low.startswith("requirements") and low.endswith(".txt")):
        kind = "python"
        o, u, ds = parse_python(p)
    elif f in ("build.gradle", "build.gradle.kts", "settings.gradle"):
        acc["has"].add("gradle")
    if not kind:
        return
    acc["has"].add(kind)
    acc["own"].setdefault(kind, []).extend(o)
    acc["uses"].setdefault(kind, []).extend((x, str(p.parent)) if x.startswith("path:") else x for x in u)
    # Описание — из манифеста поближе к корню
    if ds and depth < acc["desc_depth"]:
        acc["desc"], acc["desc_depth"] = ds, depth


# --- граф -----------------------------------------------------------------

def rel_path(source_file: str, repo: str) -> str:
    """source_file Graphify бывает абсолютным (/data/<репо>/...) — к пути от корня репо."""
    s = (source_file or "").replace("\\", "/")
    mark = f"/{repo}/"
    if mark in s:
        s = s.split(mark, 1)[1]
    return s.lstrip("./")


def graph_facts(con: sqlite3.Connection | None, repo: str) -> dict:
    if con is None:
        return {}
    row = con.execute("SELECT rid, nodes, edges FROM repos WHERE name=?", (repo,)).fetchone()
    if not row:
        return {}
    rid, n_nodes, n_edges = row
    # Модули — узлы по первым двум папкам пути. Узел-файл тоже считается:
    # вес модуля — сколько в нём всего
    mods: dict[str, int] = {}
    for sf, k in con.execute("SELECT source_file, count(*) FROM nodes WHERE rid=? GROUP BY source_file", (rid,)):
        parts = rel_path(sf, repo).split("/")[:-1]
        key = "/".join(parts[:2]) + "/" if parts else "(корень)"
        mods[key] = mods.get(key, 0) + k
    modules = sorted(mods.items(), key=lambda kv: -kv[1])[:TOP_MODULES]
    # Главные функции — как god_nodes: без узлов-файлов и методов через точку
    top = con.execute(
        "SELECT label, degree, source_file FROM nodes WHERE rid=? AND source_file IS NOT NULL"
        " AND source_file != '' AND loc IS NOT 'L1' AND label NOT LIKE '%.%'"
        " ORDER BY degree DESC LIMIT ?", (rid, TOP_NODES)).fetchall()
    return {"nodes": n_nodes, "edges": n_edges,
            "modules": [[m, k] for m, k in modules],
            "top": [[lab, deg, rel_path(sf, repo)] for lab, deg, sf in top]}


# --- сборка ---------------------------------------------------------------

def repo_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and p.name != "graph" and p.name not in SKIP_DIRS)


def scan_repo(path: Path, cand: set[str] | None = None) -> dict:
    """Один обход репозитория: языки и строки, манифесты, упоминания имён
    других репозиториев (cand, в нижнем регистре) в конфигах."""
    langs: dict[str, list[int]] = {}
    files = lines = configs = 0
    man = new_manifests()
    mentions: dict[str, int] = {}
    for f in walk(path):
        low, suf = f.name.lower(), f.suffix.lower()
        read_manifest(f, man, len(f.relative_to(path).parts) - 1)
        lang = LANGS.get(suf)
        if lang:
            n = count_lines(f)
            acc = langs.setdefault(lang, [0, 0])
            acc[0] += 1
            acc[1] += n
            files += 1
            lines += n
        if not cand or (suf not in CONFIG_EXT and low not in CONFIG_NAMES and not is_dockerfile(low)):
            continue
        if low in ("package-lock.json", "go.sum", "composer.lock") or suf == ".lock" or configs >= CONFIG_FILES_CAP:
            continue
        try:
            if f.stat().st_size > (JSON_MAX if suf == ".json" else CONFIG_MAX):
                continue
            text = f.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        configs += 1
        # Слова текста против множества имён: быстрее одного регэкспа из 80 веток
        # Точка на краю — конец предложения или домена: alert-manager.ns.svc
        tokens = set(_NAME_TOKEN.findall(text))
        tokens |= {t.strip(".") for t in tokens} | {p for t in tokens if "." in t for p in t.split(".")}
        for other in tokens & cand:
            mentions[other] = mentions.get(other, 0) + 1
    title, readme = "", ""
    rp = find_readme(path)
    if rp:
        title, readme = read_readme(rp)
    return {"langs": sorted(([k, v[0], v[1]] for k, v in langs.items()), key=lambda x: -x[2]),
            "files": files, "lines": lines, "title": title[:200], "readme": readme,
            "manifests": man, "mention_raw": mentions}


def kind_of(card: dict) -> str:
    has = set(card["manifests"]["has"])
    code_lines = sum(l for lang, _, l in card["langs"] if lang not in CONFIG_LANGS)
    if "dockerfile" in has and code_lines:
        return "сервис"
    if code_lines >= 200 or has & {"go.mod", "pom.xml", "package.json", "python", "gradle"}:
        return "библиотека"
    if "Chart.yaml" in has or card["langs"]:
        return "деплой"
    return "прочее"


def _go_base(module: str) -> str:
    """git.cb/x/vault-manager/v2 -> git.cb/x/vault-manager: major-версия — тот же репозиторий."""
    return re.sub(r"/v\d+$", "", module)


def _owner(hits: list[tuple[str, str]], tail: str) -> str | None:
    """Один объявивший — он. Несколько (монорепозиторий main объявляет то же,
    что и сервис) — тот, чьё имя совпадает с последним сегментом, иначе не угадываем."""
    repos = sorted({r for _, r in hits})
    if len(repos) == 1:
        return repos[0]
    same = [r for r in repos if r.lower() == tail]
    return same[0] if len(same) == 1 else None


def _match_dep(dep: str, kind: str, index: dict[str, list[tuple[str, str]]], names: dict[str, str]) -> str | None:
    """Кто в релизе объявляет эту зависимость. Go — ещё и подпакет модуля."""
    if kind == "go.mod":
        dep = _go_base(dep)
    tail = re.split(r"[/:]", dep)[-1].lower()
    for d in (dep, dep.lower()):
        hits = [h for h in index.get(d, []) if h[0] == kind]
        if hits:
            return _owner(hits, tail)
    if kind == "go.mod":
        # Только объявленные модули: по последнему сегменту Go даёт ложные
        # совпадения (github.com/prometheus/common -> репозиторий common)
        parts = dep.split("/")
        for i in range(len(parts) - 1, 1, -1):
            hits = [h for h in index.get("/".join(parts[:i]), []) if h[0] == kind]
            if hits:
                return _owner(hits, parts[i - 1].lower())
        return None
    # Последний сегмент совпал с именем папки: ru.company:vault-manager,
    # @cb/vault-manager, зависимость чарта. Общие и короткие — нет: bitnami/common
    if tail in GENERIC or len(tail) < 4:
        return None
    return names.get(tail)


def mention_candidates(names: list[str]) -> set[str]:
    """Имена, которые ищем в конфигах. Короткие и общие не ищем: шум."""
    return {n.lower() for n in names if n.lower() not in GENERIC and (len(n) >= 6 or "-" in n)}


def link_repos(cards: dict[str, dict], root: Path) -> None:
    """Зависимости между репозиториями релиза: deps/used_by и mentions/mentioned_by."""
    names = {n.lower(): n for n in cards}
    index: dict[str, list[tuple[str, str]]] = {}
    for name, c in cards.items():
        for kind, ids in c["manifests"]["own"].items():
            for i in ids:
                i = _go_base(i) if kind == "go.mod" else i
                for key in {i, i.lower()}:
                    index.setdefault(key, []).append((kind, name))
    for name, c in cards.items():
        deps: dict[str, set] = {}
        for kind, uses in c["manifests"]["uses"].items():
            for u in uses:
                if isinstance(u, (list, tuple)):  # replace ../other в go.mod
                    rel, base = u
                    try:
                        target = (Path(base) / rel[5:]).resolve().relative_to(root.resolve()).parts[0]
                    except (ValueError, IndexError, OSError):
                        continue
                    other = names.get(target.lower())
                else:
                    other = _match_dep(u, kind, index, names)
                if other and other != name:
                    deps.setdefault(other, set()).add(kind)
        c["deps"] = {k: sorted(v) for k, v in sorted(deps.items())}
    for c in cards.values():
        c["used_by"], c["mentioned_by"] = {}, {}
    for name, c in cards.items():
        for other, why in c["deps"].items():
            cards[other]["used_by"][name] = why
    # Слабый признак — имя другого репозитория в конфигах: адрес сервиса,
    # имя образа, имя чарта. Уже связанные по манифестам не повторяем
    for name, c in cards.items():
        raw = c.pop("mention_raw", None) or {}
        hits = {names[k]: v for k, v in raw.items()
                if k in names and names[k] != name and names[k] not in c["deps"]}
        c["mentions"] = dict(sorted(hits.items(), key=lambda kv: -kv[1]))
    for name, c in cards.items():
        for other, k in c["mentions"].items():
            cards[other]["mentioned_by"][name] = k


def build(root: Path, db: Path | None, out: Path, log=print) -> int:
    dirs = repo_dirs(root)
    if not dirs:
        log(f"В {root} нет репозиториев")
        return 1
    con = None
    if db and db.is_file():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    else:
        log(f"    [!] базы графа {db} нет — карточки без модулей и главных функций")
    t0 = time.time()
    cards: dict[str, dict] = {}
    cand = mention_candidates([d.name for d in dirs])
    for i, d in enumerate(dirs, 1):
        t = time.time()
        c = {"name": d.name, **scan_repo(d, cand), **graph_facts(con, d.name)}
        c["kind"] = kind_of(c)
        about = c["readme"] or c["manifests"]["desc"]
        # Заголовок README часто и есть лучшее описание («Выгрузка Confluence
        # в базу знаний») — если это не просто имя репозитория
        title = c["title"]
        if title and re.sub(r"[\W_]", "", title.lower()) != re.sub(r"[\W_]", "", d.name.lower()):
            about = f"{title.rstrip('.')}. {about}" if about else title
        c["summary"] = first_sentences(about) if about else ""
        cards[d.name] = c
        log(f"    {i}/{len(dirs)} {d.name}: {c['kind']}, файлов {c['files']}, строк {c['lines']}"
            + ("" if c["readme"] else ", README нет") + f" ({time.time() - t:.0f} с)")
    if con:
        con.close()
    link_repos(cards, root)
    for c in cards.values():
        m = c.pop("manifests")
        c["declares"] = sorted({i for ids in m["own"].values() for i in ids})[:20]
        c["has"] = sorted(m["has"])
    data = {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"), "root": str(root), "repos": cards}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".new")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)  # сервер видит либо старый файл, либо новый целиком
    n_deps = sum(len(c["deps"]) for c in cards.values())
    n_ment = sum(len(c["mentions"]) for c in cards.values())
    no_readme = sum(1 for c in cards.values() if not c["readme"])
    log(f"Карточки {out}: репозиториев {len(cards)}, зависимостей по манифестам {n_deps},"
        f" упоминаний в конфигах {n_ment}, без README {no_readme}, {time.time() - t0:.0f} с")
    return 0


# --- вывод ----------------------------------------------------------------

_BAD_FILE = re.compile(r'[\\/:*?"<>|#^\[\]]')


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _num(n: int) -> str:
    return f"{n / 1e6:.1f} млн" if n >= 1_000_000 else f"{n / 1e3:.0f} тыс." if n >= 10_000 else str(n)


def main_lang(c: dict) -> str:
    code = [l for l in c["langs"] if l[0] not in CONFIG_LANGS]
    return (code or c["langs"] or [["—"]])[0][0]


def line(c: dict) -> str:
    """Одна строка списка: имя — вид, язык, объём — о чём."""
    size = f"{_num(c['lines'])} строк" if c["lines"] else "без кода"
    deps = f", зависит от {len(c['deps'])}" if c.get("deps") else ""
    about = first_sentences(c["summary"], 140) if c["summary"] else "(README нет)"
    return f"{c['name']} — {c['kind']}, {main_lang(c)}, {size}{deps}. {about}"


def note_name(name: str) -> str:
    return _BAD_FILE.sub("_", name)


def _link(name: str, wiki: bool) -> str:
    return f"[[{note_name(name)}]]" if wiki else name


def card_text(c: dict, *, wiki: bool = False) -> str:
    """Карточка целиком: для модели (wiki=False) и для Obsidian (ссылки [[...]])."""
    out = [f"## {c['name']}", f"Вид: {c['kind']}. " + (f"Заголовок README: {c['title']}" if c["title"] else "")]
    if c["readme"]:
        out += ["", "Что это (README):", c["readme"]]
    elif c["summary"]:
        out += ["", "Что это (описание из манифеста): " + c["summary"]]
    else:
        out += ["", "README нет — о назначении судите по модулям и функциям ниже."]
    if c["langs"]:
        total = max(1, c["lines"])
        langs = ", ".join(f"{lang} {100 * l // total}% ({f} файл., {_num(l)} строк)"
                          for lang, f, l in c["langs"][:6])
        out += ["", f"Языки: {langs}. Всего файлов {c['files']}, строк {_num(c['lines'])}."]
    if c.get("nodes"):
        out.append(f"Граф: узлов {c['nodes']}, связей {c['edges']}.")
    if c.get("modules"):
        total = max(1, sum(k for _, k in c["modules"]))
        out.append("Модули (доля узлов графа): " + ", ".join(
            f"{m} {100 * k // total}%" for m, k in c["modules"]))
    if c.get("top"):
        out.append("Главные функции (больше всего связей): " + ", ".join(
            f"{lab} ({deg}, {f})" for lab, deg, f in c["top"]))
    if c.get("declares"):
        out.append("Объявляет: " + ", ".join(c["declares"][:8]))

    def deps(title, d, fmt):
        if d:
            out.append(title + ", ".join(fmt(k, v) for k, v in d.items()))
    out.append("")
    deps("Зависит от (манифесты): ", c.get("deps"), lambda k, v: f"{_link(k, wiki)} [{', '.join(v)}]")
    deps("Используется в: ", c.get("used_by"), lambda k, v: f"{_link(k, wiki)} [{', '.join(v)}]")
    deps("Упоминает в конфигах (слабый признак, число файлов): ", c.get("mentions"),
         lambda k, v: f"{_link(k, wiki)} ({v})")
    deps("Упоминается в конфигах: ", c.get("mentioned_by"), lambda k, v: f"{_link(k, wiki)} ({v})")
    if not any(c.get(k) for k in ("deps", "used_by", "mentions", "mentioned_by")):
        out.append("Связей с другими репозиториями релиза по манифестам и конфигам не найдено.")
    return "\n".join(x for x in out if x is not None)


def overview(cards: dict) -> list[str]:
    """Список всего релиза по видам."""
    lines = [f"Релиз: репозиториев {len(cards)}."]
    for kind in KINDS:
        group = sorted((c for c in cards.values() if c["kind"] == kind), key=lambda c: -c["lines"])
        if group:
            lines += ["", f"### {kind} ({len(group)})"] + [f"- {line(c)}" for c in group]
    return lines


# --- поиск для cb_repos ---------------------------------------------------

# Слова вопроса, которые ничего не говорят о репозитории
QUERY_STOP = set("""
a an and are as at be by does do for from how in is it of on or the to what which who with
repo repos repository repositories service services release all list
кто что где как какие какой какая каких чем за от из в во на по для и или это эта этот
отвечает отвечают делает делают зависит зависят состоит части частей релиз релиза релизе
сервис сервиса сервисы сервисов репозиторий репозитория репозитории репозиториев все всех
список покажи расскажи есть нужен нужно про чего чём продукт продукта продукте product products
cb parts part consist consists components component overview whole компоненты компонентов модули
модулей общий общая обзор целиком состав составе опиши описание построен устроен
""".split())
_WORD = re.compile(r"[\w\-]+", re.UNICODE)
STOP_STEMS = {w[:6] for w in QUERY_STOP if len(w) > 6}
MAX_CARDS = 4


def _stem(w: str) -> str:
    """Грубая основа: «уведомлений» и «уведомления» -> «уведом»."""
    return w[:6] if len(w) > 6 else w


def find_repos(cards: dict, query: str) -> tuple[list[str], list[str]]:
    """(названные в вопросе прямо, найденные по словам — по убыванию веса)."""
    q = (query or "").lower()
    words = [w.strip("-") for w in _WORD.findall(q)]
    # «CB18.5» — имя релиза, а не слово о репозитории
    # Стоп-слово и в другой форме: «сервисах» — это «сервис»
    words = [w for w in words if len(w) >= 3 and w not in QUERY_STOP and _stem(w) not in STOP_STEMS
             and not re.fullmatch(r"(cb)?-?\d+", w)]
    if not words:
        return [], []
    names = {n.lower(): n for n in cards}
    # Прямо названный репозиторий: имя целиком в вопросе, или слово —
    # единственная часть одного имени (vault -> vault-manager)
    named = [n for low, n in names.items()
             if re.search(r"(?<![\w-])" + re.escape(low) + r"(?![\w-])", q)]
    for w in words:
        # Часть имени — целиком между дефисами: vault -> vault-manager, но не log -> catalog
        part = [n for low, n in names.items() if w in re.split(r"[-_.]", low)]
        if len(part) == 1 and part[0] not in named:
            named.append(part[0])
    stems = {_stem(w) for w in words}
    scores = {}
    for name, c in cards.items():
        if name in named:
            continue
        fields = [(name.lower(), 5), ((c.get("title") or "").lower(), 3), ((c.get("readme") or c.get("summary") or "").lower(), 2),
                  (" ".join(m for m, _ in c.get("modules", [])).lower(), 1),
                  (" ".join(t[0] for t in c.get("top", [])).lower(), 1),
                  (" ".join(c.get("declares", [])).lower(), 1)]
        s = 0
        for text, w in fields:
            if not text:
                continue
            for st in stems:
                k = text.count(st)
                if k:
                    s += w * min(k, 3)
        if s:
            scores[name] = s
    found = sorted(scores, key=lambda n: -scores[n])
    return named, found


def answer(data: dict, query: str) -> list[str]:
    """Ответ cb_repos строками: список релиза, карточки названных или найденных."""
    cards = data["repos"]
    head = f"(карточки собраны {data.get('built_at', '?')})"
    named, found = find_repos(cards, query)
    if not named and not found:
        lines = overview(cards)
        if query and query.strip():
            lines.insert(0, f"По словам «{query.strip()[:100]}» в карточках ничего нет — весь список, выберите сами.")
        return lines + ["", head, "Подробно об одном — cb_repos с его именем."]
    lines = []
    for n in named[:MAX_CARDS]:
        lines += [card_text(cards[n]), ""]
    rest = found[:MAX_CARDS - min(len(named), MAX_CARDS)] if not named else []
    for n in rest:
        lines += [card_text(cards[n]), ""]
    more = [n for n in found if n not in rest][:10]
    if more:
        lines.append("Ещё по словам вопроса: " + "; ".join(line(cards[n]) for n in more))
    lines += ["", head]
    return lines


# --- Obsidian -------------------------------------------------------------

def obsidian(data: dict, out: Path, log=print) -> int:
    """Хранилище Obsidian: заметка на репозиторий, ссылки — зависимости, плюс
    указатель и services.yaml — черновик каталога сервисов для слоя тегов."""
    cards = data["repos"]
    out.mkdir(parents=True, exist_ok=True)
    # Заметки прошлой выгрузки — долой: репозиторий могли убрать из релиза.
    # Только свои (метка в шапке), заметки пользователя не трогаем
    for p in out.glob("*.md"):
        try:
            if NOTE_MARK in p.read_text(encoding="utf-8", errors="replace")[:500]:
                p.unlink()
        except OSError:
            pass
    for c in cards.values():
        tags = ["cb18-5", c["kind"], main_lang(c).lower().replace("#", "sharp").replace("+", "p")]
        front = ["---", NOTE_MARK, f"kind: {c['kind']}", f"language: {main_lang(c)}", f"lines: {c['lines']}",
                 "tags: [" + ", ".join(t.replace(" ", "-") for t in tags) + "]", "---", ""]
        body = card_text(c, wiki=True).replace(f"## {c['name']}", f"# {c['name']}", 1)
        (out / f"{note_name(c['name'])}.md").write_text("\n".join(front) + body + "\n", encoding="utf-8")
    idx = ["---", NOTE_MARK, "---", "# Релиз CB18.5", "", f"Собрано {data['built_at']} из `{data['root']}`.",
           "Связи между заметками — зависимости по манифестам и упоминания в конфигах:",
           "их видно в Graph view.", ""]
    for kind in KINDS:
        group = sorted((c for c in cards.values() if c["kind"] == kind), key=lambda c: c["name"])
        if group:
            idx += [f"## {kind} ({len(group)})", ""]
            idx += [f"- {_link(c['name'], True)} — {first_sentences(c['summary'], 140) or '(README нет)'}"
                    for c in group] + [""]
    (out / "_Релиз CB18.5.md").write_text("\n".join(idx), encoding="utf-8")
    # services.yaml — руками поправить назначение и владельца, потом — в слой тегов
    def q(v) -> str:  # строка YAML в кавычках: имена on, yes, 1.0, @scope
        return json.dumps(v, ensure_ascii=False)

    ys = ["# Черновик каталога сервисов релиза CB18.5 (code/repo_cards.py). Поля",
          "# purpose и owner — заполнить руками; depends_on — по манифестам.", "services:"]
    for c in sorted(cards.values(), key=lambda c: c["name"]):
        ys += [f"  - name: {q(c['name'])}", f"    kind: {q(c['kind'])}", f"    language: {q(main_lang(c))}",
               "    purpose: " + json.dumps(first_sentences(c["summary"], 200), ensure_ascii=False),
               "    owner: \"\"",
               "    depends_on: [" + ", ".join(q(d) for d in c.get("deps", {})) + "]"]
    (out / "services.yaml").write_text("\n".join(ys) + "\n", encoding="utf-8")
    log(f"Obsidian: {out} — заметок {len(cards)}, указатель «_Релиз CB18.5.md», services.yaml")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Карточки репозиториев релиза")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="собрать cards.json по каталогу клонов")
    b.add_argument("root", type=Path)
    b.add_argument("--db", type=Path, help="graph.sqlite (модули и главные функции)")
    b.add_argument("--out", type=Path, help="по умолчанию — cards.json рядом с базой")
    s = sub.add_parser("show", help="карточка одного репозитория или список (без имени)")
    s.add_argument("name", nargs="?")
    s.add_argument("--cards", type=Path, required=True)
    o = sub.add_parser("obsidian", help="выгрузка в хранилище Obsidian")
    o.add_argument("--cards", type=Path, required=True)
    o.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        if not args.root.is_dir():
            print(f"Нет каталога {args.root}")
            return 1
        out = args.out or (args.db.with_name("cards.json") if args.db else args.root / "graph" / "cards.json")
        return build(args.root, args.db, out)
    data = load(args.cards)
    if args.cmd == "obsidian":
        return obsidian(data, args.out)
    if args.name:
        c = data["repos"].get(args.name)
        print(card_text(c) if c else f"Нет карточки {args.name}")
        return 0 if c else 1
    print("\n".join(overview(data["repos"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
