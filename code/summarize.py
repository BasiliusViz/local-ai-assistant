"""Модель пишет README репозиториев по коду — снизу вверх по папкам (guides/PLAN-CB-SUMMARY.md).

    python summarize.py prepare [<каталог с клонами>] --repo svc   # выжимки без модели
    python summarize.py run     [<каталог с клонами>] --repo svc   # пробный прогон на одном
    python summarize.py run                                         # все репозитории

Каталог по умолчанию — CODE_DIR. Модель — GEN_MODEL на OLLAMA_URL (ключ и заголовок —
как у kb: OLLAMA_API_KEY, OLLAMA_AUTH_HEADER, OLLAMA_AUTH_PREFIX, OLLAMA_API=native|openai).
Переменные берутся из окружения, недостающие — из .env рядом с репозиторием (--env).

Что видит модель:
  - файл до --small байт — целиком; больше — скелет: строки узлов графа с диска
    (сигнатуры) плюс докстринги/комментарии под ними и шапка файла; нет узлов —
    первые HEAD_CHARS символов;
  - папка с входом меньше --threshold тыс. токенов сливается в родительскую
    (её файлы читаются в вызове родителя); корень — всегда отдельный вызов;
  - вызов на папку: её файлы + пересказы подпапок -> JSON {"summary", "tags"};
  - вызов на README: пересказы папок + точки входа, манифесты, конфиги целиком
    + подсказка из .summaries/hints.txt («репо: что это»).

Результат: CODE_DIR/.summaries/<репо>.md и state.json (хеш входа -> пересказ):
повторный прогон зовёт модель только там, где вход изменился. prepare кладёт
входы вызовов в .summaries/_prep/<репо>/ — посмотреть глазами, что увидит модель.

Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from graph_store import find_graphs
from summary_survey import (CHARS_PER_TOKEN, HEAD_CHARS, disk_files, nodes_from_graph_json,
                            parent_of, rel_path)

MAX_FAILS = 5               # столько отказов модели подряд — она лежит, прогон останавливается
PROMPT_VERSION = "1"        # правка промптов или выжимок -> увеличить, иначе возьмутся старые пересказы
OUT_DIR = ".summaries"
SKEL_LINES = 200            # строк скелета на файл, не больше
DOC_LINES = 2               # строк докстринга/комментария под сигнатурой
HEAD_COMMENT_LINES = 8      # строк комментария в шапке файла
KEY_FILE_CHARS = 4000       # точка входа/манифест в вызове README — не больше стольких символов
KEY_TOTAL_CHARS = 24000     # и всех вместе
COMMENT = re.compile(r'^\s*(#|//|/\*|\*|"""|\'\'\'|--|;|<!--)')
KEY_FILES = re.compile(
    r"(^|/)(dockerfile[^/]*|docker-compose[^/]*\.ya?ml|jenkinsfile[^/]*|makefile|requirements[^/]*\.txt|"
    r"pyproject\.toml|setup\.py|go\.mod|pom\.xml|build\.gradle(\.kts)?|package\.json|cargo\.toml|"
    r"__main__\.py|main\.go|main\.py|app\.py|[^/]*application\.java|[^/]*\.env\.example|"
    r"application[^/]*\.(ya?ml|properties)|crontab|[^/]*\.service|readme(\.md|\.txt|\.rst)?)$", re.I)

FOLDER_PROMPT = """Ты читаешь код репозитория «{repo}», папку «{folder}».
Ниже — её файлы (маленькие целиком, большие скелетом: сигнатуры и комментарии) и пересказы её подпапок.
{hint}
Напиши по-русски, что делает эта папка: 3–5 фраз — назначение, главные части, с чем она взаимодействует
(сервисы, базы, очереди, файлы). Не выдумывай: только то, что видно из кода. Если назначение угадано
лишь по именам — так и пиши: «предположительно».
Теги: 2–6 коротких тегов строчными (тема, технология, роль).
Верни только JSON: {{"summary": "...", "tags": ["...", "..."]}}

{body}"""

README_PROMPT = """Ты пишешь README репозитория «{repo}» для разработчика, который видит его впервые.
Ниже — пересказы папок (их написала модель по коду), точки входа, манифесты и конфиги.
{hint}
Напиши README по-русски в Markdown, разделы:
## Назначение — 2–4 фразы: что это и зачем;
## Части — по строке на главную папку;
## Вход и выход — API, порты, очереди/топики, файлы, расписание — что видно из кода и конфигов;
## Зависимости — внешние сервисы, базы, библиотеки, другие репозитории;
## Как запускается — точки входа, сборка, деплой.
Не выдумывай: чего нет во входе — не пиши, раздел без сведений опусти. Угаданное по именам —
«предположительно». Только Markdown, без вступлений.

{body}"""


# --- выжимки (без модели) ---------------------------------------------------

def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _loc_line(loc) -> int | None:
    m = re.match(r"L(\d+)", str(loc or ""))
    return int(m.group(1)) if m else None


def skeleton(lines: list[str], locs: list[int]) -> str:
    """Шапка-комментарий файла + строки узлов с диска и докстринги/комментарии под ними."""
    out = []
    shown = set()                          # номера строк, уже попавших в скелет
    in_doc = False                         # шапка — многострочный докстринг
    for i, ln in enumerate(lines[:HEAD_COMMENT_LINES], 1):
        if not (in_doc or COMMENT.match(ln)):
            break
        out.append(ln.rstrip())
        shown.add(i)
        s = ln.strip()
        if s.startswith(('"""', "'''")) and not (len(s) > 3 and s.endswith(s[:3])):
            in_doc = not in_doc
        elif in_doc and s.endswith(('"""', "'''")):
            in_doc = False
    for n in sorted(set(locs))[:SKEL_LINES]:
        if not 1 <= n <= len(lines) or n in shown:
            continue
        out.append(f"L{n}: {lines[n - 1].strip()[:200]}")
        shown.add(n)
        doc = 0
        for k in range(n, min(n + 4, len(lines))):   # декораторы/скобки бывают до докстринга
            if doc >= DOC_LINES:
                break
            if COMMENT.match(lines[k]):
                out.append("      " + lines[k].strip()[:200])
                shown.add(k + 1)
                doc += 1
            elif doc:
                break
    return "\n".join(out)


def file_blocks(repo_dir: Path, repo: str, graph: Path | None, small: int) -> dict[str, str]:
    """{путь от корня: текст для модели} — целиком, скелет или начало файла."""
    locs: dict[str, list[int]] = {}
    if graph:
        for sf, _label, loc in nodes_from_graph_json(graph):
            n = _loc_line(loc)
            if sf and n:
                locs.setdefault(rel_path(str(sf), repo), []).append(n)
    out = {}
    for path, size in sorted(disk_files(repo_dir).items()):
        lines = _lines(repo_dir / path)
        if size <= small:
            text, kind = "\n".join(lines), "целиком"
        elif locs.get(path):
            text, kind = skeleton(lines, locs[path]), "скелет"
        else:
            text, kind = "\n".join(lines)[:HEAD_CHARS], "начало"
        out[path] = f"### {path} ({kind})\n{text}"
    return out


def ktok(text: str) -> float:
    return len(text) / CHARS_PER_TOKEN / 1000


def plan_units(weights: dict[str, float], threshold: float) -> dict[str, str | None]:
    """{папка-пересказ: родительская папка-пересказ} — мелкие папки слиты в предков.

    То же правило, что в summary_survey.folder_units; "" — корень, всегда пересказ.
    Куда ушла слитая папка — unit_of.
    """
    total = dict(weights)
    for folder in list(weights):
        while folder:
            folder = parent_of(folder)
            total.setdefault(folder, 0)
    units = set()
    for folder in sorted(total, key=lambda f: -f.count("/") if f else 1):
        if folder and total[folder] < threshold:
            total[parent_of(folder)] += total[folder]
            continue
        units.add(folder)
    return {u: (None if u == "" else unit_of(parent_of(u), units)) for u in units}


def unit_of(folder: str, units) -> str:
    """Ближайшая папка-пересказ, в которую попадает folder (сама или предок)."""
    while folder not in units:
        folder = parent_of(folder)
    return folder


def prepare_repo(repo_dir: Path, repo: str, graph: Path | None, threshold: float, small: int) -> dict:
    blocks = file_blocks(repo_dir, repo, graph, small)
    weights: dict[str, float] = {}
    for path, text in blocks.items():
        f = parent_of(path)
        weights[f] = weights.get(f, 0) + ktok(text)
    parents = plan_units(weights or {"": 0}, threshold)
    files: dict[str, list[str]] = {u: [] for u in parents}
    for path in blocks:
        files[unit_of(parent_of(path), parents)].append(path)
    key = [p for p in blocks if KEY_FILES.search(p)]
    key.sort(key=lambda p: (p.count("/"), p))           # корневые манифесты первыми
    return {"blocks": blocks, "parents": parents, "files": files, "key": key}


def folder_body(prep: dict, unit: str, child_summaries: dict[str, str], max_ktok: float) -> str:
    """Вход вызова на папку: пересказы подпапок, затем файлы — пока влезают в max_ktok."""
    parts = [f"## Подпапка {c or '/'}\n{s}" for c, s in sorted(child_summaries.items())]
    budget = max_ktok - sum(ktok(p) for p in parts)
    skipped = []
    for path in sorted(prep["files"][unit]):
        text = prep["blocks"][path]
        if ktok(text) <= budget:
            parts.append(text)
            budget -= ktok(text)
        else:
            skipped.append(path)
    if skipped:     # не влезли — хотя бы имена: по ним видно, что ещё есть в папке
        parts.append(f"## Не показаны (не влезли), {len(skipped)} файлов\n" + "\n".join(skipped[:200]))
    return "\n\n".join(parts)


def readme_body(prep: dict, repo_dir: Path, summaries: dict[str, str]) -> str:
    parts = [f"## Папка {u or '/ (корень)'}\n{s}" for u, s in sorted(summaries.items())]
    left = KEY_TOTAL_CHARS
    for path in prep["key"]:
        text = "\n".join(_lines(repo_dir / path))[:KEY_FILE_CHARS]
        if len(text) > left:
            break
        parts.append(f"### {path}\n{text}")
        left -= len(text)
    return "\n\n".join(parts)


def order(parents: dict[str, str | None]) -> list[str]:
    """Глубокие первыми: к вызову родителя пересказы подпапок уже готовы."""
    return sorted(parents, key=lambda u: (-(u.count("/") + 1) if u else 1, u))


def load_hints(out: Path) -> dict[str, str]:
    hints = {}
    for ln in _lines(out / "hints.txt"):
        if ":" in ln and not ln.lstrip().startswith("#"):
            k, v = ln.split(":", 1)
            hints[k.strip()] = v.strip()
    return hints


def hint_line(hint: str | None) -> str:
    return f"Подсказка владельца (это факт): {hint}\n" if hint else ""


def jenkins_first(repos: list[str], root: Path) -> list[str]:
    """Репозитории-библиотеки Jenkins (vars/*.groovy) — первыми (пока только порядок)."""
    return sorted(repos, key=lambda r: (not any((root / r / "vars").glob("*.groovy")), r))


# --- модель ------------------------------------------------------------------

def load_env(path: Path) -> None:
    """KEY=VALUE из .env — только тех, что не заданы в окружении."""
    for ln in _lines(path):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Model:
    def __init__(self, num_ctx: int, timeout: float):
        e = os.environ.get
        self.url = e("OLLAMA_URL", "http://localhost:11434/v1").rstrip("/")
        self.api = e("OLLAMA_API", "openai").strip().lower()
        self.model = e("GEN_MODEL", "qwen3:8b")
        self.headers = {"Content-Type": "application/json"}
        key = e("OLLAMA_API_KEY", "")
        if key:
            prefix = os.environ["OLLAMA_AUTH_PREFIX"] if "OLLAMA_AUTH_PREFIX" in os.environ else "Bearer "
            self.headers[e("OLLAMA_AUTH_HEADER", "Authorization").strip()] = prefix + key
        self.num_ctx, self.timeout = num_ctx, timeout
        self.fails = 0          # отказов подряд

    def chat(self, prompt: str, as_json: bool) -> str:
        msgs = [{"role": "user", "content": prompt}]
        if self.api == "native":
            base = self.url[:-3] if self.url.endswith("/v1") else self.url
            url = base.rstrip("/") + "/api/chat"
            # num_ctx обязателен: по умолчанию 4096, вход папки — до --max-ktok тысяч
            body = {"model": self.model, "messages": msgs, "stream": False, "think": False,
                    "options": {"temperature": 0, "num_ctx": self.num_ctx}}
            if as_json:
                body["format"] = "json"
        else:
            url = self.url + "/chat/completions"
            body = {"model": self.model, "messages": msgs, "temperature": 0, "reasoning_effort": "none"}
            if as_json:
                body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(url, json.dumps(body).encode(), self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            p = json.load(r)
        return p["message"]["content"] if self.api == "native" else p["choices"][0]["message"]["content"]


def parse_folder(raw: str) -> dict:
    """JSON модели -> {"summary", "tags"}; не JSON — весь текст как пересказ."""
    try:
        d = json.loads(raw)
        summary = str(d.get("summary") or "").strip()
        tags = [str(t).strip().lower() for t in d.get("tags") or [] if str(t).strip()][:6]
        if summary:
            return {"summary": summary, "tags": tags}
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"summary": raw.strip(), "tags": []}


# --- прогон ------------------------------------------------------------------

MODEL_ERRORS = (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, KeyError, ValueError)


class ModelDown(Exception):
    """Модель не отвечает MAX_FAILS раз подряд — дальше гнать бессмысленно."""

def digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def run_repo(repo: str, repo_dir: Path, prep: dict, state: dict, model: Model | None,
             hint: str | None, max_ktok: float, save, log=print) -> dict:
    """Пересказы папок снизу вверх и README. model=None — только посчитать, что пойдёт в модель."""
    st = state.setdefault(repo, {"folders": {}})
    done: dict[str, dict] = {}
    calls = reused = failed = 0
    secs: list[float] = []
    mname = model.model if model else "-"

    def ask(prompt: str, as_json: bool) -> str:
        assert model is not None
        t = time.monotonic()
        try:
            raw = model.chat(prompt, as_json)
        except MODEL_ERRORS:
            model.fails += 1
            if model.fails >= MAX_FAILS:
                raise ModelDown(f"{MAX_FAILS} отказов подряд")
            raise
        model.fails = 0
        secs.append(time.monotonic() - t)
        return raw

    for unit in order(prep["parents"]):
        children = {c: done[c]["summary"] for c, p in prep["parents"].items() if p == unit and c in done}
        body = folder_body(prep, unit, children, max_ktok)
        prompt = FOLDER_PROMPT.format(repo=repo, folder=unit or "/ (корень)", hint=hint_line(hint), body=body)
        h = digest(PROMPT_VERSION, mname, prompt)
        old = st["folders"].get(unit)
        if old and old.get("hash") == h:
            done[unit] = old
            reused += 1
            continue
        calls += 1
        if model is None:
            done[unit] = {"hash": h, "summary": f"(пересказ {unit or '/'})", "tags": []}
            log(f"    {unit or '/':<50} вход ~{ktok(prompt):.1f} ктк")
            continue
        try:
            res = parse_folder(ask(prompt, True))
        except MODEL_ERRORS as e:
            log(f"    {unit or '/'}: модель не ответила ({e}) — пропущено")
            failed += 1
            continue
        done[unit] = st["folders"][unit] = {"hash": h, **res}
        log(f"    {unit or '/':<50} вход ~{ktok(prompt):.1f} ктк, {secs[-1]:.0f} с")
        save()

    for gone in set(st["folders"]) - set(prep["parents"]):     # папки, которых больше нет
        del st["folders"][gone]
    body = readme_body(prep, repo_dir, {u: d["summary"] for u, d in done.items()})
    prompt = README_PROMPT.format(repo=repo, hint=hint_line(hint), body=body)
    h = digest(PROMPT_VERSION, mname, prompt)
    if failed and model is not None:    # README по неполным пересказам не писать — следующий прогон допишет
        log(f"    README отложен: не готово папок — {failed}")
    elif st.get("readme_hash") != h:
        calls += 1
        if model is None:
            log(f"    README вход ~{ktok(prompt):.1f} ктк")
        else:
            try:
                st["readme"] = ask(prompt, False).strip()
                st["readme_hash"] = h
                log(f"    README вход ~{ktok(prompt):.1f} ктк, {secs[-1]:.0f} с")
                save()
            except MODEL_ERRORS as e:
                log(f"    README: модель не ответила ({e})")
    return {"calls": calls, "reused": reused, "secs": secs}


def write_md(out: Path, repo: str, st: dict) -> None:
    if not st.get("readme"):
        return
    tags = sorted({t for d in st["folders"].values() for t in d.get("tags", [])})
    lines = [f"# {repo}", "", f"> Сгенерировано моделью по коду ({time.strftime('%d.%m.%Y')}). "
             "Может ошибаться: проверяйте по коду.", "", st["readme"], ""]
    if tags:
        lines += ["", "Теги: " + ", ".join(tags), ""]
    lines += ["", "## Пересказы папок", ""]
    for unit in sorted(st["folders"]):
        d = st["folders"][unit]
        lines += [f"### {unit or '/'}", d["summary"], ""]
    (out / f"{repo}.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["prepare", "run"])
    ap.add_argument("root", nargs="?", type=Path, help="каталог с клонами (по умолчанию CODE_DIR)")
    ap.add_argument("--repo", action="append", help="только этот репозиторий (можно несколько раз)")
    ap.add_argument("--threshold", type=float, default=4, help="порог слияния папок, тыс. токенов (4)")
    ap.add_argument("--small", type=int, default=8000, help="файл до стольких байт — целиком (8000)")
    ap.add_argument("--max-ktok", type=float, default=24, help="вход одного вызова, тыс. токенов (24)")
    ap.add_argument("--num-ctx", type=int, default=32768, help="окно модели при OLLAMA_API=native")
    ap.add_argument("--timeout", type=float, default=600, help="секунд на вызов")
    ap.add_argument("--env", type=Path, default=Path(__file__).resolve().parent.parent / ".env")
    a = ap.parse_args(argv)
    load_env(a.env)
    root = a.root or (Path(os.environ["CODE_DIR"]) if os.environ.get("CODE_DIR") else None)
    if not root or not root.is_dir():
        ap.error("нужен каталог с клонами или CODE_DIR")
    out = root / OUT_DIR
    out.mkdir(exist_ok=True)

    repos = [d.name for d in sorted(root.iterdir())
             if d.is_dir() and not d.name.startswith(".") and d.name != "graph"]
    if a.repo:
        missing = set(a.repo) - set(repos)
        if missing:
            ap.error(f"нет в {root}: {', '.join(sorted(missing))}")
        repos = [r for r in repos if r in a.repo]
    graphs = find_graphs(root)
    hints = load_hints(out)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}

    def save():
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(state_path)

    model = Model(a.num_ctx, a.timeout) if a.cmd == "run" else None
    if model:
        print(f"Модель {model.model} на {model.url} ({model.api})")
    total = {"calls": 0, "reused": 0, "secs": []}
    code = 0
    for i, repo in enumerate(jenkins_first(repos, root), 1):
        try:
            prep = prepare_repo(root / repo, repo, graphs.get(repo), a.threshold, a.small)
            print(f"[{i}/{len(repos)}] {repo}: файлов {len(prep['blocks'])}, пересказов папок "
                  f"{len(prep['parents'])}" + ("" if repo in graphs else " (графа нет — без скелетов)"),
                  flush=True)
            if a.cmd == "prepare":
                dump_prep(out / "_prep" / repo, root / repo, prep, a.max_ktok)
            r = run_repo(repo, root / repo, prep, state, model, hints.get(repo), a.max_ktok, save,
                         log=lambda *x: print(*x, flush=True))
            if model:
                write_md(out, repo, state[repo])
        except ModelDown as e:
            print(f"\nМодель не отвечает ({e}) — остановлено на {repo}. Готовое сохранено, "
                  "повторный запуск продолжит.", flush=True)
            code = 2
            break
        except KeyboardInterrupt:
            print(f"\nПрервано на {repo}. Готовое сохранено, повторный запуск продолжит.", flush=True)
            code = 130
            break
        except Exception as e:      # испорченный граф или файл не должен ронять весь прогон
            print(f"  {repo}: ошибка, пропущен ({type(e).__name__}: {e})", flush=True)
            code = 1
            continue
        for k in total:
            total[k] += r[k]
    if a.cmd == "prepare":
        print(f"\nВызовов модели понадобится: {total['calls']} (уже готово и не изменилось: "
              f"{total['reused']}). Входы — в {out / '_prep'}")
    else:
        save()
        s = total["secs"]
        avg = sum(s) / len(s) if s else 0
        print(f"\nВызовов: {len(s)} из {total['calls']}, взято готовых: {total['reused']}; "
              f"в среднем {avg:.0f} с, макс {max(s, default=0):.0f} с. README — в {out}")
    return code


def dump_prep(d: Path, repo_dir: Path, prep: dict, max_ktok: float) -> None:
    """Входы вызовов на папки (без пересказов подпапок) — посмотреть, что увидит модель."""
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.txt"):
        old.unlink()
    for i, unit in enumerate(order(prep["parents"])):
        name = f"{i:03d}_{(unit or 'root').replace('/', '__')[:80]}.txt"
        (d / name).write_text(folder_body(prep, unit, {}, max_ktok), encoding="utf-8")
    (d / "zz_readme_key_files.txt").write_text(readme_body(prep, repo_dir, {}), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
