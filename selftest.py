"""Самопроверка стека: что живо, что отвечает и где именно рвётся.

Проверяет всю цепочку по HTTP, поэтому годится и там, где нет доступа к
машинам — только к адресам: модель, база векторов, оба MCP-сервера и граф кода.

Отдельно ловит обрыв генерации. Симптом «модель отвечает двумя символами и
замолкает» почти всегда означает, что упёрлись в лимит вывода: Ollama по
умолчанию поднимает модель с num_ctx=4096, сколько бы она ни умела вмещать, а
в промпт уезжают правила, описания инструментов и найденные фрагменты. Тест
делает два одинаковых запроса — с явным контекстом и без — и сравнивает, чем
кончилась генерация. Разные ответы = диагноз.

Запуск (настройки берутся из .env, как у остального стека):

    python selftest.py                  всё, что настроено
    python selftest.py --verbose        плюс подробности ответов
    python selftest.py --insecure       не проверять TLS (внутренний УЦ)

В контейнере адреса соседей другие, поэтому там:

    docker compose exec kb python selftest.py \\
        --qdrant http://qdrant:6333 --kb http://kb:8010 --dojo http://dojo:8012

Код возврата = число провалившихся проверок, так что скрипт годится и для
планировщика: ноль значит «всё в порядке».

Если в PowerShell 5.1 вместо русского каша — это консоль, а не скрипт:
выполните `chcp 65001` перед запуском.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent

# Маркеры только ASCII: PowerShell 5.1 ломает юникодные символы в консоли,
# и вместо отчёта получается каша
OK = "[ ok ]"
BAD = "[FAIL]"
SKIP = "[скип]"


def load_env() -> None:
    """Читает .env рядом со скриптом. Уже заданные переменные важнее файла."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def why(e: Exception) -> str:
    """Короткая причина вместо простыни из urllib3.

    Полный текст исключения занимает пять строк и прячет суть: в отчёте важно
    «не отвечает» или «таймаут», а стек соединения не нужен никому.
    """
    if isinstance(e, requests.ConnectionError):
        return "соединение отклонено — сервис не поднят или закрыт брандмауэром"
    if isinstance(e, requests.Timeout):
        return "таймаут — сервис есть, но не ответил вовремя"
    text = str(e)
    return text if len(text) < 160 else text[:160] + "..."


class Report:
    """Счётчик проверок. Печатает по ходу, а не в конце: если что-то повиснет,
    видно, на чём именно."""

    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.failed = 0
        self.passed = 0
        self.skipped = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  {OK} {name}" + (f" — {detail}" if detail else ""))

    def bad(self, name: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  {BAD} {name}" + (f" — {detail}" if detail else ""))

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"  {SKIP} {name} — {reason}")

    def note(self, text: str) -> None:
        if self.verbose:
            print(f"         {text}")

    def section(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * len(title))


# --------------------------------------------------------------------------
# Модель
# --------------------------------------------------------------------------


def auth_headers() -> dict:
    """Заголовок авторизации к Ollama — ровно тот же, что собирает kb.

    Шлюзы ждут ключ по-разному: Bearer у OpenAI-совместимых, x-api-key у
    прокси. Префикс для x-api-key должен быть ПУСТЫМ, иначе уходит
    «x-api-key: Bearer <ключ>» и шлюз отбивает запрос.
    """
    key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not key:
        return {}
    header = os.getenv("OLLAMA_AUTH_HEADER", "Authorization").strip()
    prefix = os.getenv("OLLAMA_AUTH_PREFIX", "Bearer ")
    return {header: f"{prefix}{key}"}


def ollama_base() -> str:
    """Адрес без /v1: родные пути Ollama живут в корне."""
    url = os.getenv("OLLAMA_URL", "http://localhost:11434/v1").strip().rstrip("/")
    return url[:-3].rstrip("/") if url.endswith("/v1") else url


def check_model(rep: Report, verify: bool) -> None:
    rep.section("Модель")

    base = ollama_base()
    headers = auth_headers()
    gen = os.getenv("GEN_MODEL", "qwen3:8b").strip()
    embed = os.getenv("EMBED_MODEL", "bge-m3").strip()
    native = os.getenv("OLLAMA_API", "openai").strip().lower() == "native"

    if headers:
        rep.note(f"ключ уходит в заголовке {list(headers)[0]}")

    # --- доступность и список моделей
    names: list[str] = []
    try:
        resp = requests.get(
            f"{base}/api/tags", headers=headers, timeout=30, verify=verify
        )
        if resp.status_code in (401, 403):
            rep.bad(
                "Ollama отвечает",
                f"{resp.status_code}: ключ не принят. Проверьте OLLAMA_API_KEY "
                "и OLLAMA_AUTH_HEADER",
            )
            return
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        rep.ok("Ollama отвечает", f"моделей: {len(names)}")
        rep.note(", ".join(names[:10]))
    except requests.RequestException as e:
        rep.bad("Ollama отвечает", f"{base}: {why(e)}")
        return

    for label, wanted in (("генеративная", gen), ("эмбеддер", embed)):
        # Ollama хранит теги вида «модель:latest», в .env их часто пишут без
        # хвоста — сравниваем по префиксу
        if any(n == wanted or n.startswith(wanted.split(":")[0]) for n in names):
            rep.ok(f"модель на месте ({label})", wanted)
        else:
            rep.bad(f"модель на месте ({label})", f"{wanted} нет в списке")

    # --- эмбеддинги: та же ручка, которой пользуется индексатор
    try:
        if native:
            url = f"{base}/api/embed"
            payload = {"model": embed, "input": ["проверка"]}
        else:
            url = f"{base}/v1/embeddings"
            payload = {"model": embed, "input": ["проверка"]}
        resp = requests.post(
            url, json=payload, headers=headers, timeout=60, verify=verify
        )
        if resp.status_code == 404:
            rep.bad(
                "эмбеддинги считаются",
                f"404 на {url}. Шлюз пробрасывает только одно из API — "
                "попробуйте OLLAMA_API=native (или наоборот)",
            )
        else:
            resp.raise_for_status()
            data = resp.json()
            vectors = data.get("embeddings") if native else [
                item["embedding"] for item in data.get("data", [])
            ]
            size = len(vectors[0]) if vectors else 0
            if size == 1024:
                rep.ok("эмбеддинги считаются", f"размерность {size}")
            else:
                rep.bad(
                    "эмбеддинги считаются",
                    f"размерность {size}, а коллекция ждёт 1024 — "
                    "похоже, модель не та, которой индексировали",
                )
    except requests.RequestException as e:
        rep.bad("эмбеддинги считаются", why(e))

    # --- генерация и, главное, чем она закончилась
    check_truncation(rep, base, headers, gen, native, verify)
    check_long_answer(rep, base, headers, gen, native, verify)


def _chat(base, headers, model, native, verify, options: dict | None, prompt: str | None = None):
    """Один запрос к модели. Возвращает (текст, причина остановки, секунды)."""
    prompt = prompt or "Ответь тремя предложениями: что такое дежурство в ИТ-команде?"
    started = time.monotonic()

    if native:
        payload = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
        if options:
            payload["options"] = options
        resp = requests.post(
            f"{base}/api/chat", json=payload, headers=headers, timeout=180, verify=verify
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("message", {}).get("content", ""),
            data.get("done_reason", ""),
            time.monotonic() - started,
        )

    payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if options:
        # У OpenAI-совместимого API нет num_ctx, но есть предел вывода —
        # проверяем хотя бы его
        payload["max_tokens"] = options.get("num_predict", 1024)
    resp = requests.post(
        f"{base}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=180,
        verify=verify,
    )
    resp.raise_for_status()
    choice = resp.json()["choices"][0]
    return (
        choice["message"]["content"],
        choice.get("finish_reason", ""),
        time.monotonic() - started,
    )


def check_truncation(rep, base, headers, model, native, verify) -> None:
    """Обрывается ли ответ на умолчаниях.

    Два одинаковых запроса: как ходит Continue (без явных пределов) и с
    заданными контекстом и лимитом вывода. Если первый кончается по лимиту, а
    второй нет — виноваты умолчания, и лечится это в конфиге клиента.
    """
    try:
        plain, reason, spent = _chat(base, headers, model, native, verify, None)
    except requests.RequestException as e:
        rep.bad("модель отвечает", why(e))
        return

    words = len(plain.split())
    if reason in ("stop", "", None) and words >= 5:
        rep.ok("модель отвечает целиком", f"{words} слов за {spent:.0f} с, причина: {reason or 'stop'}")
        rep.note(plain[:160].replace("\n", " "))
        return

    rep.bad(
        "модель отвечает целиком",
        f"оборвано на {words} словах, причина: {reason or 'неизвестна'}",
    )
    rep.note(f"ответ: {plain[:160]!r}")

    try:
        fixed, _, _ = _chat(
            base, headers, model, native, verify,
            {"num_ctx": 32768, "num_predict": 1024},
        )
    except requests.RequestException as e:
        rep.note(f"повторная попытка не удалась: {why(e)}")
        return

    if len(fixed.split()) > words:
        rep.bad(
            "ДИАГНОЗ",
            "с явными num_ctx=32768 и num_predict=1024 ответ полный. Значит "
            "дело в умолчаниях: пропишите в конфиге Continue "
            "defaultCompletionOptions.contextLength и maxTokens",
        )
    else:
        rep.note(
            "с явными пределами короче не стало — причина не в контексте, "
            "смотрите в сторону шлюза или самой модели"
        )



LONG_PROMPT = (
    "Подробно, по пунктам, опиши пятнадцать этапов разбора найденной "
    "уязвимости в веб-приложении. Каждый пункт — два-три предложения."
)


def check_long_answer(rep, base, headers, model, native, verify) -> None:
    """Обрывается ли ДЛИННЫЙ ответ, и по какому пределу.

    Обрыв на середине бывает двух природ, и лечатся они в разных местах.
    По длине — упёрлись в лимит вывода, поднимается maxTokens у клиента.
    По времени — поток режет прокси или шлюз, и это не к нам.
    Отличить можно только замером: смотрим, сколько слов успело выйти и за
    сколько секунд.
    """
    try:
        text, reason, spent = _chat(
            base, headers, model, native, verify,
            {"num_ctx": 32768, "num_predict": 4096},
            LONG_PROMPT,
        )
    except requests.RequestException as e:
        rep.bad("длинный ответ доходит целиком", why(e))
        return

    words = len(text.split())
    tail = " ".join(text.rstrip()[-40:].split())

    if reason in ("stop", "", None):
        rep.ok("длинный ответ доходит целиком", f"{words} слов за {spent:.0f} с")
        return

    rep.bad(
        "длинный ответ доходит целиком",
        f"оборвано: {words} слов за {spent:.0f} с, причина: {reason}",
    )
    rep.note(f"кончилось на: ...{tail}")
    if reason == "length":
        rep.note(
            "упёрлись в лимит вывода — поднимите maxTokens в конфиге Continue "
            "(num_predict здесь был 4096)"
        )
    else:
        rep.note(
            "причина не в лимите вывода. Если обрыв повторяется примерно на той "
            "же СЕКУНДЕ, а не на том же объёме, поток режет прокси или шлюз"
        )


# --------------------------------------------------------------------------
# Векторная база
# --------------------------------------------------------------------------


def check_qdrant(rep: Report, url: str, verify: bool) -> None:
    rep.section("База векторов")

    try:
        resp = requests.get(f"{url}/collections", timeout=30, verify=verify)
        resp.raise_for_status()
        names = [c["name"] for c in resp.json()["result"]["collections"]]
        rep.ok("Qdrant отвечает", ", ".join(names) or "коллекций нет")
    except requests.RequestException as e:
        rep.bad("Qdrant отвечает", f"{url}: {why(e)}")
        return

    collection = os.getenv("KB_COLLECTION", "knowledge")
    if collection not in names:
        rep.bad(
            f"коллекция {collection}",
            "не создана — ничего не проиндексировано",
        )
        return

    try:
        info = requests.get(
            f"{url}/collections/{collection}", timeout=30, verify=verify
        ).json()["result"]
        rep.ok(f"коллекция {collection}", f"точек: {info.get('points_count', 0)}")
    except requests.RequestException as e:
        rep.bad(f"коллекция {collection}", why(e))
        return

    # По источникам: сразу видно, что проиндексировано, а что забыли
    for source, human in (
        ("confluence", "документация Confluence"),
        ("local", "локальные документы"),
        ("jira", "задачи Jira"),
        ("dojo", "находки DefectDojo"),
    ):
        try:
            resp = requests.post(
                f"{url}/collections/{collection}/points/count",
                json={
                    "filter": {"must": [{"key": "source", "match": {"value": source}}]},
                    "exact": True,
                },
                timeout=60,
                verify=verify,
            )
            count = resp.json()["result"]["count"]
            if count:
                rep.ok(f"источник {source}", f"{human}: {count} чанков")
            else:
                rep.skip(f"источник {source}", f"{human} не проиндексирована")
        except (requests.RequestException, KeyError) as e:
            rep.bad(f"источник {source}", why(e))

    if "code" in names:
        try:
            info = requests.get(
                f"{url}/collections/code", timeout=30, verify=verify
            ).json()["result"]
            rep.ok("индекс кода", f"точек: {info.get('points_count', 0)}")
        except requests.RequestException as e:
            rep.bad("индекс кода", why(e))
    else:
        rep.skip("индекс кода", "коллекция code не создана")


# --------------------------------------------------------------------------
# MCP-серверы
# --------------------------------------------------------------------------


def mcp_call(url: str, method: str, params: dict | None, verify: bool) -> dict:
    """Запрос к MCP по streamable-http. Заголовок Accept обязателен."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    resp = requests.post(
        f"{url.rstrip('/')}/mcp",
        json=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=120,
        verify=verify,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "неизвестная ошибка"))
    return data["result"]


def tool_payload(result: dict) -> dict:
    """Ответ инструмента приходит текстом внутри content — разворачиваем."""
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def check_mcp(rep: Report, name: str, url: str, expected: list[str], verify: bool) -> list[str]:
    rep.section(f"MCP: {name} ({url})")
    try:
        tools = [t["name"] for t in mcp_call(url, "tools/list", None, verify)["tools"]]
    except (requests.RequestException, RuntimeError, KeyError) as e:
        rep.bad("сервер отвечает", why(e))
        return []

    rep.ok("сервер отвечает", f"инструментов: {len(tools)}")
    rep.note(", ".join(tools))

    for wanted in expected:
        if wanted in tools:
            rep.ok(f"инструмент {wanted}", "есть")
        else:
            rep.bad(
                f"инструмент {wanted}",
                "отсутствует — образ собран из старого кода, "
                "нужен docker compose up -d --build",
            )
    return tools


def check_kb_tools(rep: Report, url: str, verify: bool) -> None:
    """Живой вызов поиска: сервер может отвечать, а искать при этом нечем."""
    try:
        payload = tool_payload(
            mcp_call(
                url,
                "tools/call",
                {"name": "kb_search", "arguments": {"query": "требования к паролям"}},
                verify,
            )
        )
    except (requests.RequestException, RuntimeError, KeyError) as e:
        rep.bad("вызов kb_search", why(e))
        return

    if "error" in payload:
        rep.bad("вызов kb_search", payload["error"][:120])
    else:
        rep.ok("вызов kb_search", f"найдено фрагментов: {payload.get('found', 0)}")


def check_dojo_tool(rep: Report, url: str, verify: bool) -> None:
    try:
        payload = tool_payload(
            mcp_call(
                url,
                "tools/call",
                {"name": "dojo_findings", "arguments": {"product": os.getenv("DOJO_PRODUCTS", "").split(",")[0].strip()}},
                verify,
            )
        )
    except (requests.RequestException, RuntimeError, KeyError) as e:
        rep.bad("вызов dojo_findings", why(e))
        return

    if "error" in payload:
        rep.bad("вызов dojo_findings", payload["error"][:140])
    else:
        summary = {k: v for k, v in payload.get("summary", {}).items() if v}
        rep.ok(
            "вызов dojo_findings",
            f"продукт {payload.get('product')}, сводка: {summary or 'пусто'}",
        )


# --------------------------------------------------------------------------


def main() -> int:
    load_env()

    ap = argparse.ArgumentParser(description="Самопроверка стека LOCAL-AI")
    ap.add_argument("--qdrant", default=os.getenv("SELFTEST_QDRANT", "http://localhost:6333"))
    ap.add_argument("--kb", default=os.getenv("SELFTEST_KB", "http://localhost:8010"))
    ap.add_argument("--code-graph", default=os.getenv("SELFTEST_CODE_GRAPH", "http://localhost:8011"))
    ap.add_argument("--dojo", default=os.getenv("SELFTEST_DOJO", "http://localhost:8012"))
    ap.add_argument("--verbose", action="store_true", help="показывать подробности")
    ap.add_argument("--insecure", action="store_true", help="не проверять TLS")
    ap.add_argument("--skip-model", action="store_true", help="не трогать модель (долго)")
    args = ap.parse_args()

    verify = not args.insecure
    rep = Report(args.verbose)

    print("Самопроверка стека. Проверки идут по порядку; если что-то повиснет,")
    print("последняя напечатанная строка покажет, на чём именно.")

    if args.skip_model:
        rep.section("Модель")
        rep.skip("проверка модели", "отключена ключом --skip-model")
    else:
        check_model(rep, verify)

    check_qdrant(rep, args.qdrant.rstrip("/"), verify)

    kb_tools = check_mcp(
        rep, "поиск", args.kb, ["kb_search", "code_search", "jira_search"], verify
    )
    if "kb_search" in kb_tools:
        check_kb_tools(rep, args.kb, verify)

    check_mcp(rep, "граф кода", args.code_graph, [], verify)

    dojo_tools = check_mcp(rep, "уязвимости", args.dojo, ["dojo_findings"], verify)
    if "dojo_findings" in dojo_tools and os.getenv("DOJO_PRODUCTS", "").strip():
        check_dojo_tool(rep, args.dojo, verify)
    elif "dojo_findings" in dojo_tools:
        rep.skip("вызов dojo_findings", "не задан DOJO_PRODUCTS, нечего спросить")

    print(f"\nИтог: успешно {rep.passed}, провалено {rep.failed}, пропущено {rep.skipped}")
    if rep.failed:
        print("Провалившиеся строки помечены " + BAD + " — в них написано, что чинить.")
    return rep.failed


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано")
        sys.exit(130)
