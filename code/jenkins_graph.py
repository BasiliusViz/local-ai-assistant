"""Связи Jenkins в графе кода: какие пайплайны и шаги вызывают какие шаги.

Graphify общую библиотеку Jenkins не видит. Его настройки Groovy знают только
методы классов, а vars/*.groovy — это скрипты с функцией call() на верхнем
уровне; вдобавок грамматика tree-sitter-groovy 0.1.2 (последняя) не разбирает
`def call(cfg)` и `Map args = [:]`. И главное — связь здесь по соглашению
Jenkins, а не Groovy: `abActions(...)` в пайплайне выполняет call() из файла
vars/abActions.groovy. Шаг называется по имени файла.

Поэтому связи строятся здесь и дописываются в уже слитый граф, после
`graphify merge-graphs`. Сам Graphify не правится: его версия в образе не
закреплена, и правка тихо слетела бы при обновлении. Дописывать нужно именно
в слитый граф: merge-graphs даёт узлам каждого репозитория свой префикс, а
пайплайн и шаг обычно живут в РАЗНЫХ репозиториях.

Как находятся вызовы. Не грамматикой — она как раз и спотыкается, — а по
именам: сначала собираются все шаги (vars/*.groovy во всех репозиториях),
затем в коде без комментариев и строк ищутся их имена в позиции вызова:
    abActions(...)    abActions { ... }    abActions env: 'prod'
    abActions.helper(...)    steps.abActions(...)   (из классов в src/)
Чужое `obj.abActions(...)` вызовом шага не считается.

Не находится: шаг, выбранный по имени из переменной (`"${name}"()`), и вызовы
внутри строк — их не отличить от текста.

    python jenkins_graph.py --graph /data/graph/graph.json --repos /data
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ORIGIN = "jenkins"
SKIP_DIRS = {".git", "node_modules", "graphify-out", "target", "build", ".gradle", "vendor"}


# --------------------------------------------------------------- файлы


def is_pipeline(path: Path) -> bool:
    """Jenkinsfile, Jenkinsfile.deploy, deploy.jenkinsfile, deploy.Jenkinsfile."""
    name = path.name
    return name.startswith("Jenkinsfile") or name.lower().endswith(".jenkinsfile")


def is_step(path: Path) -> bool:
    return path.suffix == ".groovy" and path.parent.name == "vars"


def scan(repos_dir: Path) -> tuple[list[tuple[str, Path]], list[tuple[str, Path]], list[tuple[str, Path]]]:
    """(шаги, пайплайны, прочий groovy) как пары (репозиторий, путь)."""
    steps, pipelines, other = [], [], []
    for repo_dir in sorted(p for p in repos_dir.iterdir() if p.is_dir()):
        if repo_dir.name.startswith(".") or repo_dir.name == "graph":
            continue
        for dirpath, dirnames, filenames in os.walk(repo_dir, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for filename in filenames:
                path = Path(dirpath) / filename
                if is_step(path):
                    steps.append((repo_dir.name, path))
                elif is_pipeline(path):
                    pipelines.append((repo_dir.name, path))
                elif path.suffix == ".groovy":
                    other.append((repo_dir.name, path))
    return steps, pipelines, other


# ------------------------------------------------------ разбор текста


def strip_comments_and_strings(src: str) -> str:
    """Комментарии -> пробелы, у строк остаются только кавычки.

    Номера строк остаются верными, а `// abActions(...)` и
    `echo "run abActions(...)"` вызовами не считаются. Кавычки оставлены,
    чтобы опознать вызов без скобок со строкой: `notifySlack "упало"`.
    Интерполяцию ${...} внутри строк тоже выбрасываем: вызов шага из строки —
    редкость, а ложные срабатывания на тексте — нет.
    """
    out = []
    i, n = 0, len(src)

    def blank(chunk: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in chunk)

    def blank_string(chunk: str, quote: str) -> str:
        q = len(quote)
        if len(chunk) < 2 * q:
            return quote + blank(chunk[q:])
        return quote + blank(chunk[q:-q]) + quote

    while i < n:
        c = src[i]
        two = src[i : i + 2]
        three = src[i : i + 3]
        if two == "//":
            end = src.find("\n", i)
            end = n if end == -1 else end
            out.append(blank(src[i:end]))
            i = end
        elif two == "/*":
            end = src.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(blank(src[i:end]))
            i = end
        elif three in ('"""', "'''"):
            end = src.find(three, i + 3)
            end = n if end == -1 else end + 3
            out.append(blank_string(src[i:end], three))
            i = end
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            end = min(j + 1, n)
            out.append(blank_string(src[i:end], c))
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def call_pattern(names: set[str]) -> re.Pattern | None:
    """Имя шага в позиции вызова. Длинные имена первыми: abActionsV2 раньше abActions."""
    if not names:
        return None
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(
        # перед именем — не буква/цифра/$ и не точка; исключение — steps.
        r"(?:(?<![\w$.])|(?<=\bsteps\.))"
        rf"(?P<name>{alternatives})"
        # после — скобка, блок, .метод( или аргумент без скобок:
        # abActions env: 'prod' / notifySlack "текст" / abActions cfg
        r"(?=\s*\(|\s*\{|\.\w+\s*\(|[ \t]+"
        r"(?!(?:in|instanceof|as|and|or|else|then)\b)[\w\"'\[\-])"
    )


def find_calls(src: str, pattern: re.Pattern | None, own: str = "") -> list[tuple[str, int]]:
    """(имя шага, строка) для каждого вызова. own — сам шаг: рекурсию не считаем."""
    if pattern is None:
        return []
    clean = strip_comments_and_strings(src)
    calls = []
    for m in pattern.finditer(clean):
        name = m.group("name")
        if name == own:
            continue
        # Объявление функции с тем же именем внутри файла — не вызов:
        # def abActions(...) {
        before = clean[max(0, m.start() - 12) : m.start()]
        if re.search(r"\b(def|void|static)\s+$", before):
            continue
        calls.append((name, clean.count("\n", 0, m.start()) + 1))
    return calls


def call_line(src: str) -> int:
    """Строка с def call — туда и ведёт узел шага."""
    # [ \t]*, а не \s*: \s захватил бы перевод строки перед def, и номер
    # строки съехал бы на одну вверх
    m = re.search(r"^[ \t]*(def|void|\w+)\s+call\s*\(", strip_comments_and_strings(src), re.M)
    return src.count("\n", 0, m.start()) + 1 if m else 1


# ------------------------------------------------------------- граф


def node_id(kind: str, repo: str, rel: str) -> str:
    return f"{ORIGIN}::{kind}::{repo}/{rel}"


def build(repos_dir: Path, community: int) -> tuple[list[dict], list[dict], dict]:
    steps, pipelines, other = scan(repos_dir)

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def rel(repo: str, path: Path) -> str:
        return path.relative_to(repos_dir / repo).as_posix()

    nodes: dict[str, dict] = {}
    # имя шага -> id узлов. Больше одного — шаг с таким именем есть в двух
    # библиотеках, и какой из них вызывается, решает подключение @Library
    by_name: dict[str, list[str]] = {}

    def add_node(nid: str, label: str, source_file: str, line: int, callable_: bool) -> None:
        nodes[nid] = {
            "id": nid,
            "label": label,
            "norm_label": label.lower(),
            "source_file": source_file,
            "source_location": f"L{line}",
            "file_type": "code",
            "_origin": ORIGIN,
            "_callable": callable_,
            "community": community,
            "community_name": "Jenkins",
        }

    sources: dict[str, str] = {}
    for repo, path in steps:
        src = read(path)
        r = rel(repo, path)
        nid = node_id("step", repo, r)
        add_node(nid, path.stem, f"{repo}/{r}", call_line(src), True)
        by_name.setdefault(path.stem, []).append(nid)
        sources[nid] = src

    callers: list[tuple[str, str, str]] = []  # (id, own step name, source)
    for nid, src in sources.items():
        callers.append((nid, nodes[nid]["label"], src))
    for repo, path in pipelines:
        r = rel(repo, path)
        nid = node_id("pipeline", repo, r)
        # Подпись — репозиторий и путь: «Jenkinsfile» есть в каждом проекте,
        # и по одному имени пайплайн не найти
        add_node(nid, f"{repo}/{r}", f"{repo}/{r}", 1, False)
        callers.append((nid, "", read(path)))

    pattern = call_pattern(set(by_name))
    links: list[dict] = []
    seen: set[tuple[str, str]] = set()
    classes_calling = 0
    for repo, path in other:
        # Классы в src/ зовут шаги через steps.abActions(...); узел для файла
        # заводим, только если такой вызов в нём есть — иначе граф забьют
        # сотни groovy-файлов, к Jenkins отношения не имеющих
        src = read(path)
        if find_calls(src, pattern):
            r = rel(repo, path)
            nid = node_id("groovy", repo, r)
            add_node(nid, f"{repo}/{r}", f"{repo}/{r}", 1, False)
            callers.append((nid, "", src))
            classes_calling += 1

    ambiguous = 0
    for caller, own, src in callers:
        for name, line in find_calls(src, pattern, own):
            targets = by_name[name]
            for target in targets:
                if (caller, target) in seen:
                    continue
                seen.add((caller, target))
                exact = len(targets) == 1
                ambiguous += 0 if exact else 1
                links.append({
                    "source": caller,
                    "target": target,
                    "_src": caller,
                    "_tgt": target,
                    "relation": "calls",
                    "context": "jenkins step",
                    "_origin": ORIGIN,
                    "confidence": "EXTRACTED" if exact else "AMBIGUOUS",
                    "confidence_score": 1.0 if exact else 0.5,
                    "source_file": nodes[caller]["source_file"],
                    "source_location": f"L{line}",
                    "weight": 1.0,
                })

    called = {l["target"] for l in links}
    stats = {
        "шагов": len(steps),
        "пайплайнов": len(pipelines),
        "groovy-классов, зовущих шаги": classes_calling,
        "вызовов": len(links),
        "из них неоднозначных": ambiguous,
        "шагов, которые никто не зовёт": sum(1 for ids in by_name.values() for i in ids if i not in called),
    }
    duplicates = {n: ids for n, ids in by_name.items() if len(ids) > 1}
    if duplicates:
        stats["одноимённых шагов в разных библиотеках"] = len(duplicates)
    return list(nodes.values()), links, stats


def apply(graph_path: Path, repos_dir: Path) -> dict:
    """Дописать связи Jenkins в graph.json. Повторный запуск не плодит дублей."""
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    links_key = "links" if "links" in data or "edges" not in data else "edges"
    old_nodes = [n for n in data.get("nodes", []) if n.get("_origin") != ORIGIN]
    old_links = [l for l in data.get(links_key, []) if l.get("_origin") != ORIGIN]
    communities = [n.get("community") for n in old_nodes if isinstance(n.get("community"), int)]
    community = max(communities, default=-1) + 1

    nodes, links, stats = build(repos_dir, community)
    data["nodes"] = old_nodes + nodes
    data[links_key] = old_links + links

    # Запись через временный файл: MCP-сервер перечитывает graph.json по
    # времени изменения, и недописанный файл он прочитал бы битым
    tmp = graph_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, graph_path)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Связи Jenkins в графе кода")
    ap.add_argument("--graph", required=True, help="graph.json после merge-graphs")
    ap.add_argument("--repos", required=True, help="каталог с репозиториями")
    args = ap.parse_args(argv)

    graph, repos = Path(args.graph), Path(args.repos)
    if not graph.is_file():
        print(f"Нет графа: {graph}")
        return 1
    if not repos.is_dir():
        print(f"Нет каталога: {repos}")
        return 1
    stats = apply(graph, repos)
    for key, value in stats.items():
        print(f"    {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
