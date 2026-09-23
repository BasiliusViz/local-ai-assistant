"""Замер: правильно ли модель выбирает инструменты DefectDojo и их поля.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.dojo_tool_eval --model qwen3.6:35b

Модель видит то же, что в Continue: инструменты всех трёх MCP-серверов
одним списком и правила из continue-rules.yaml системным промптом. На
каждую формулировку проверяется ПЕРВЫЙ вызов: тот ли инструмент и что
лежит в полях. Инструменты НЕ выполняются — DefectDojo не трогается,
проверяется только выбор модели.

Без такого замера «стало лучше» неотличимо от «повезло с формулировкой».
Код возврата — число промахов.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

from kb import config

# (вопрос, допустимые инструменты, проверки полей: поле -> подстрока)
# None в инструментах — модель НЕ должна звать инструменты dojo_*
CASES: list[tuple[str, set[str] | None, dict[str, str]]] = [
    ("dojo какие engagement есть в abinf", {"dojo_engagements"}, {"product": "abinf"}),
    ("dojo покажи engagement'ы продукта abinf", {"dojo_engagements"}, {"product": "abinf"}),
    ("dojo какие ветки есть в abinf", {"dojo_engagements"}, {"product": "abinf"}),
    ("dojo сравни engagement main и feature-x в abinf", {"dojo_compare"},
     {"product": "abinf", "base": "main", "target": "feature-x"}),
    ("dojo сравни main_abinf и feature-x_abinf", {"dojo_compare"},
     {"product": "abinf", "base": "main", "target": "feature-x"}),
    ("dojo что появилось в feature-x по сравнению с main в abinf", {"dojo_compare"},
     {"product": "abinf", "base": "main", "target": "feature-x"}),
    ("dojo чем release-1.2 отличается от release-1.1 в abinf", {"dojo_compare"},
     {"product": "abinf", "base": "release-1.1", "target": "release-1.2"}),
    ("dojo сравни только критичные между main и feature-x в abinf", {"dojo_compare"},
     {"product": "abinf", "base": "main", "target": "feature-x", "severity": "крит|critical"}),
    ("dojo что устранили в release-1.2 относительно release-1.1 в abinf",
     {"dojo_compare", "dojo_release_notes"},
     {"product": "abinf", "base": "release-1.1", "target": "release-1.2"}),
    ("dojo сделай release notes по устранённым между release-1.1 и release-1.2 в abinf",
     {"dojo_release_notes"}, {"product": "abinf", "base": "release-1.1", "target": "release-1.2"}),
    ("dojo release notes для abinf: release-1.1 -> release-1.2, только критичные и высокие",
     {"dojo_release_notes"},
     {"product": "abinf", "base": "release-1.1", "target": "release-1.2", "severity": "крит|critical"}),
    ("dojo подготовь релиз ноутс по закрытым уязвимостям abinf между release-1.1 и release-1.2",
     {"dojo_release_notes"}, {"product": "abinf", "base": "release-1.1", "target": "release-1.2"}),
    ("dojo критичные в abinf", {"dojo_findings"}, {"product": "abinf", "severity": "крит|critical"}),
    ("dojo общая картина", {"dojo_findings"}, {"product": ""}),
    ("dojo где у нас log4j", {"dojo_findings"}, {"query": "log4j"}),
    ("напиши функцию на Go, которая разворачивает строку", None, {}),
    ("поищи в базе, какие требования к паролям", None, {}),
]


def mcp_tools(url: str) -> list[dict]:
    resp = httpx.post(
        f"{url.rstrip('/')}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
        timeout=60,
    )
    resp.raise_for_status()
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {}),
            },
        }
        for t in resp.json()["result"]["tools"]
    ]


def load_rules(path: Path) -> str:
    """Правила из continue-rules.yaml без pyyaml: список строк, продолжения
    с отступом, кавычки YAML снимаются. Continue склеивает их в системный
    промпт — так же и здесь."""
    rules, current, inside = [], [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("rules:"):
            inside = True
            continue
        if not inside or line.lstrip().startswith("#"):
            continue
        if line.startswith("- "):
            if current:
                rules.append(" ".join(current))
            current = [line[2:].strip()]
        elif line.startswith("  ") and current:
            current.append(line.strip())
    if current:
        rules.append(" ".join(current))
    out = []
    for rule in rules:
        if len(rule) >= 2 and rule[0] == rule[-1] and rule[0] in "'\"":
            rule = rule[1:-1].replace("''", "'")
        out.append(rule)
    return "\n".join(f"- {r}" for r in out)


def ask(model: str, system: str, question: str, tools: list[dict]) -> tuple[str | None, dict, str]:
    """Первый вызов инструмента: (имя, аргументы, текст, если вызова нет)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    if config.OLLAMA_API == "native":
        body = {"model": model, "messages": messages, "tools": tools, "stream": False,
                "options": {"temperature": 0.3}}
    else:
        body = {"model": model, "messages": messages, "tools": tools, "temperature": 0.3}
    resp = httpx.post(config.chat_url(), json=body, headers=config.auth_headers(), timeout=600)
    resp.raise_for_status()
    data = resp.json()
    msg = data["message"] if config.OLLAMA_API == "native" else data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        return None, {}, (msg.get("content") or "").strip().replace("\n", " ")[:100]
    fn = calls[0]["function"]
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    return fn["name"], args, ""


def check(tool: str | None, args: dict, want: set[str] | None, fields: dict) -> list[str]:
    problems = []
    if want is None:
        if tool and tool.startswith("dojo_"):
            problems.append(f"звать dojo не надо, а вызван {tool}")
        return problems
    if tool not in want:
        return [f"нужен {' или '.join(sorted(want))}, а {'вызван ' + tool if tool else 'вызова нет'}"]
    for field, expected in fields.items():
        value = str(args.get(field) or "").casefold()
        if expected == "":
            if value and value not in ("*", "all", "все"):
                problems.append(f"{field} должен быть пуст, а «{value}»")
        elif not re.search(expected, value):
            problems.append(f"{field}: ждём «{expected}», а «{value}»")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер выбора инструментов DefectDojo")
    ap.add_argument("--model", default=config.EXPAND_MODEL, help="как в Continue: qwen3.6:35b")
    ap.add_argument("--kb", default="http://kb:8010")
    ap.add_argument("--graph", default="http://code-graph:8011")
    ap.add_argument("--dojo", default="http://dojo:8012")
    ap.add_argument("--rules", default="continue-rules.yaml")
    ap.add_argument("--only", help="прогнать только вопросы, содержащие эту строку")
    args = ap.parse_args()

    tools = []
    for name, url in (("kb", args.kb), ("code-graph", args.graph), ("dojo", args.dojo)):
        try:
            got = mcp_tools(url)
            tools += got
            print(f"{name:10} {url}: {', '.join(t['function']['name'] for t in got)}")
        except Exception as e:
            print(f"{name:10} {url}: недоступен ({e}) — без его инструментов")
    rules_path = Path(args.rules)
    system = load_rules(rules_path) if rules_path.exists() else ""
    print(f"правил: {system.count(chr(10)) + 1 if system else 0}, модель: {args.model}\n")

    cases = [c for c in CASES if not args.only or args.only in c[0]]
    misses = 0
    started = time.time()
    for question, want, fields in cases:
        try:
            tool, call_args, text = ask(args.model, system, question, tools)
        except httpx.TransportError as e:
            # Модель недоступна — дальше каждый вопрос ждал бы таймаута
            print(f"Нет связи с моделью ({config.chat_url()}): {e}")
            return len(cases)
        except Exception as e:
            misses += 1
            print(f"[ОШИБКА] {question}\n         {e}")
            continue
        problems = check(tool, call_args, want, fields)
        misses += bool(problems)
        mark = "OK  " if not problems else "МИМО"
        print(f"[{mark}] {question}")
        shown = json.dumps(call_args, ensure_ascii=False) if tool else f"текст: {text}"
        print(f"         {tool or '—'} {shown}")
        for p in problems:
            print(f"         ! {p}")

    print(f"\nИтог: {len(cases) - misses} из {len(cases)}, {time.time() - started:.0f} с")
    return misses


if __name__ == "__main__":
    sys.exit(main())
