"""Проверка, что код релиза CB18.5 и обычный код нигде не перемешались.

    python3 check-cb.py

На сервере, из каталога проекта. Только стандартная библиотека и только
чтение: ничего не пишет ни в Qdrant, ни в графы, ни на диск.

Что сверяется (релиз — CB_DIR, обычный код — CODE_DIR из .env):
  1. каталоги     какие репозитории где лежат; одинаковые имена — предупреждение
  2. Qdrant       в code_cb только репозитории релиза, в code — ни одного
                  репозитория, который есть только в релизе
  3. инструменты  :8010 отдаёт code_search и cb_search, :8011 — без префикса,
                  :8013 — только cb_*
  4. графы        в графе обычного кода нет репозиториев релиза и наоборот
  5. живой поиск  cb_search находит только релиз, code_search — только обычный код

Код возврата — число провалов: 0 значит, что всё разделено.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OK, BAD, SKIP, WARN = "[ok]", "[!!]", "[--]", "[! ]"
failed = 0


def say(mark: str, text: str) -> None:
    global failed
    if mark == BAD:
        failed += 1
    print(f"  {mark} {text}")


def load_env() -> dict[str, str]:
    """Как читает compose: export впереди и комментарий после — допустимы."""
    env: dict[str, str] = {}
    path = HERE / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if m:
                value = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip("'\"")
                env[m.group(1)] = value
    # заданное в окружении важнее файла — как у repos/sync.py
    for key in ("CODE_DIR", "CB_DIR", "QDRANT_PORT", "KB_PORT", "CODE_GRAPH_PORT", "CB_GRAPH_PORT"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def repo_dirs(root: Path) -> set[str]:
    """Каталоги репозиториев так же, как их видит kb.code_index."""
    if (root / "repos").is_dir():
        root = root / "repos"
    if not root.is_dir():
        return set()
    return {
        p.name for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "graph"
    }


def http_json(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    if raw.startswith("event:") or raw.startswith("data:"):
        raw = next(l[5:] for l in raw.splitlines() if l.startswith("data:"))
    return json.loads(raw)


def mcp(port: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    data = http_json(f"http://localhost:{port}/mcp", body)
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "ошибка"))
    return data["result"]


def facet_repos(qdrant: str, collection: str) -> set[str] | None:
    """Какие репозитории лежат в коллекции. None — коллекции нет."""
    try:
        data = http_json(f"{qdrant}/collections/{collection}/facet", {"key": "repo", "limit": 10000})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {str(h["value"]) for h in data["result"]["hits"]}


def graph_repos(path: Path, known: set[str]) -> set[str] | None:
    """Какие из известных репозиториев упоминаются в узлах графа. Формат узлов
    у Graphify не зафиксирован, поэтому смотрим все строковые поля с путями.
    Репозиторий — ПЕРВЫЙ сегмент пути от корня кода (/data/<репо>/...,
    /data/repos/<репо>/... или относительный <репо>/... у шагов Jenkins):
    папка api внутри чужого репозитория не должна сойти за репозиторий api."""
    if not path.is_file():
        return None
    graph = json.loads(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        for value in node.values():
            if isinstance(value, str) and "/" in value:
                repo = repo_of(value)
                if repo in known:
                    found.add(repo)
    return found


def repo_of(path: str) -> str:
    path = path.replace("\\", "/")
    for root in ("/data/repos/", "/data/"):
        if path.startswith(root):
            return path[len(root):].split("/", 1)[0]
    if path.startswith("/"):
        return ""
    return path.split("/", 1)[0]


def locations(payload: dict) -> set[str]:
    """Репозитории из ответа поиска: location = репозиторий/путь:строка."""
    return {r["location"].split("/", 1)[0] for r in payload.get("results", []) if "location" in r}


def tool_payload(result: dict) -> dict:
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def main() -> int:
    env = load_env()
    code_dir = Path(env.get("CODE_DIR") or HERE / "data/code")
    cb_dir = Path(env.get("CB_DIR") or HERE / "data/cb")
    qdrant = f"http://localhost:{env.get('QDRANT_PORT', '6333')}"
    kb_port = env.get("KB_PORT", "8010")
    graph_port = env.get("CODE_GRAPH_PORT", "8011")
    cb_port = env.get("CB_GRAPH_PORT", "8013")

    print("=== 1. Каталоги ===")
    code, cb = repo_dirs(code_dir), repo_dirs(cb_dir)
    say(OK if code else WARN, f"обычный код {code_dir}: репозиториев {len(code)}")
    say(OK if cb else BAD, f"релиз {cb_dir}: репозиториев {len(cb)}"
        + ("" if cb else " — ./update-cb.sh --download"))
    both = code & cb
    if both:
        say(WARN, "одинаковые имена в обоих каталогах (по имени их не различить, "
            f"проверки ниже для них не строгие): {', '.join(sorted(both))}")
    cb_only, code_only = cb - code, code - cb

    print("\n=== 2. Qdrant ===")
    try:
        in_cb = facet_repos(qdrant, "code_cb")
        in_code = facet_repos(qdrant, "code")
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        say(BAD, f"Qdrant {qdrant} не ответил: {e}")
        in_cb = in_code = None
    else:
        if in_cb is None:
            say(BAD, "коллекции code_cb нет — ./update-cb.sh")
        else:
            stray = in_cb - cb
            say(BAD if stray else OK, f"code_cb: репозиториев {len(in_cb)}"
                + (f", НЕ из релиза: {', '.join(sorted(stray))}" if stray else ", все из релиза"))
            missing = cb - in_cb
            if missing:
                say(WARN, f"скачаны, но не в индексе релиза (пустые или без кода?): {', '.join(sorted(missing))}")
        if in_code is None:
            say(SKIP, "коллекции code нет — обычный код не индексировался")
        else:
            leaked = in_code & cb_only
            say(BAD if leaked else OK, f"code: репозиториев {len(in_code)}"
                + (f", из РЕЛИЗА: {', '.join(sorted(leaked))}" if leaked else ", релиза среди них нет"))

    print("\n=== 3. Инструменты ===")
    for port, name, check in (
        (kb_port, "поиск kb", lambda t: {"code_search", "cb_search"} <= set(t)),
        (graph_port, "граф кода", lambda t: t and not any(n.startswith("cb_") for n in t)),
        (cb_port, "граф релиза", lambda t: t and all(n.startswith("cb_") for n in t)),
    ):
        try:
            tools = [t["name"] for t in mcp(port, "tools/list")["tools"]]
        except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
            say(BAD, f":{port} {name} не ответил: {e}")
            continue
        say(OK if check(tools) else BAD, f":{port} {name}: {', '.join(tools)}")

    print("\n=== 4. Графы ===")
    for path, own, other_only, label in (
        (code_dir / "graph" / "graph.json", "обычного кода", cb_only, "релиза"),
        (cb_dir / "graph" / "graph.json", "релиза", code_only, "обычного кода"),
    ):
        try:
            found = graph_repos(path, other_only)
        except (OSError, ValueError) as e:
            say(BAD, f"граф {own} {path} не прочитан: {e}")
            continue
        if found is None:
            say(SKIP if own == "обычного кода" else BAD, f"граф {own}: {path} нет")
        elif found:
            say(BAD, f"в графе {own} репозитории {label}: {', '.join(sorted(found))}")
        else:
            say(OK, f"в графе {own} репозиториев {label} нет")

    print("\n=== 5. Живой поиск ===")
    for tool, allowed, label in (("cb_search", cb, "релиз"), ("code_search", code, "обычный код")):
        try:
            payload = tool_payload(mcp(kb_port, "tools/call", {
                "name": tool, "arguments": {"query": "обработка ошибок и логирование", "top_k": 10},
            }))
        except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
            say(BAD, f"{tool} не ответил: {e}")
            continue
        if "error" in payload:
            say(SKIP if tool == "code_search" else BAD, f"{tool}: {payload['error'][:120]}")
            continue
        repos = locations(payload)
        stray = repos - allowed
        say(BAD if stray else OK, f"{tool}: найдено из {', '.join(sorted(repos)) or 'ничего'}"
            + (f" — НЕ {label}: {', '.join(sorted(stray))}" if stray else f" — только {label}"))

    print()
    print("Всё разделено." if failed == 0 else f"Провалов: {failed} — см. строки {BAD}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
