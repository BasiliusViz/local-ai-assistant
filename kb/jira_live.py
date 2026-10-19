"""Живые запросы к Jira Server/DC: JQL модели -> задачи прямо из Jira.

Индекс (kb/jira_retriever.py) отвечает на «где обсуждали похожее» — по
смыслу, но по снимку и в окне JIRA_SINCE. Здесь — точные вопросы к живой
Jira: «сколько багов Critical», «что сейчас открыто в спринте», статус
задачи на эту минуту, задачи старше окна выгрузки.

Только чтение, и это проверяется кодом, а не честным словом:
  - в Jira уходят только GET: сессия отбивает любой другой метод;
  - JQL модели всегда вкладывается в рамки проектов:
        project in (JIRA_LIVE_PROJECTS или JIRA_PROJECTS) AND (<JQL модели>)
    Команда, окно и JIRA_JQL — только для выгрузки в индекс. «*» в
    JIRA_LIVE_PROJECTS снимает рамки: граница — права токена
    Скобки и кавычки в JQL модели проверяются, чтобы она не могла закрыть
    свою скобку и дописать «OR project = ЧУЖОЙ»;
  - найденные задачи дополнительно сверяются с JIRA_PROJECTS по номеру.
Права в Jira всё равно определяет токен: для этого инструмента лучше
отдельная учётная запись только на чтение.

Клиент, рамки и разбор задачи — из jira/sync.py: один код на выгрузку и
живые запросы, чтобы граница считалась одинаково.
"""

from __future__ import annotations

import os
import re
import threading

from requests.adapters import HTTPAdapter

from jira import sync as jira_sync

# JQL модели длиннее этого — почти наверняка мусор. И URL не резиновый:
# запрос уходит GET'ом, а у Tomcat перед Jira предел на длину строки
MAX_JQL = 1000
MAX_RESULTS = 50
DEFAULT_RESULTS = 20
# Модель ждёт ответа в чате: минута на запрос, как у выгрузки, — слишком
TIMEOUT = 20
# Сколько ждать, пока Jira занята предыдущим живым запросом
WAIT = 30
DESCRIPTION_LIMIT = 800

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
# Номер задачи где угодно в JQL: Jira принимает и строчные (dev-1)
ANY_KEY = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*-\d+\b")
STRING = "§"
# Встроенные функции JQL без подзапросов. Всё прочее (плагины) — отказ
FUNCTIONS = {
    "opensprints", "closedsprints", "futuresprints",
    "membersof", "currentuser", "currentlogin", "lastlogin",
    "now", "startofday", "endofday", "startofweek", "endofweek",
    "startofmonth", "endofmonth", "startofyear", "endofyear",
    "releasedversions", "unreleasedversions",
    "latestreleasedversion", "earliestunreleasedversion",
    "componentsleadbyuser", "projectsleadbyuser",
    "standardissuetypes", "subtaskissuetypes",
    "watchedissues", "votedissues", "issuehistory", "updatedby",
}
# Слова JQL, после которых законна скобка: «status in (...)», «AND (...)»
KEYWORDS = {"in", "and", "or", "not", "was", "during", "by", "from", "to",
            "after", "before", "on", "changed"}
BIG_NUMBER = re.compile(r"(?<![\w.-])\d{5,}(?![\w.-])")
# ORDER BY в конце — единственное, что разрешено вне скобок. Поля и
# направления — только слова, без скобок и кавычек
ORDER_BY = re.compile(r"\s+ORDER\s+BY\s+(.+?)\s*$", re.IGNORECASE)
ORDER_ITEM = re.compile(r"^[\w.\"' -]+?(\s+(ASC|DESC))?$", re.IGNORECASE)

# Без комментариев и без description в кратком ответе: живой запрос должен
# быть лёгким, контекст модели не резиновый
LIST_FIELDS = [f for f in jira_sync.FIELDS]


class JiraLiveError(RuntimeError):
    """Объяснение для модели: она может исправить запрос и позвать снова."""


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def projects() -> list[str]:
    """Проекты живого поиска: JIRA_LIVE_PROJECTS, иначе JIRA_PROJECTS.

    «*» — вся Jira, которую видит токен: тогда граница — только его права.
    """
    raw = _env_list("JIRA_LIVE_PROJECTS") or _env_list("JIRA_PROJECTS")
    return [p.upper() for p in raw]


def all_projects() -> bool:
    return projects() == ["*"]


def available() -> bool:
    """Включается явно: JIRA_LIVE=1, плюс адрес, вход и проекты.

    По умолчанию выключено: после пересборки kb модель не должна молча
    получить живой доступ к Jira с токеном выгрузки.
    """
    if os.getenv("JIRA_LIVE", "0").strip() != "1":
        return False
    has_auth = bool(os.getenv("JIRA_TOKEN") or os.getenv("JIRA_USER"))
    return bool(os.getenv("JIRA_URL") and has_auth and projects())


def scope_jql() -> str:
    """Рамки живого поиска — только проекты.

    Команда (JIRA_TEAM), окно (JIRA_SINCE) и JIRA_JQL сужают выгрузку в
    индекс, чтобы она была быстрой. Живому поиску они мешают: эпики и
    бэклог часто без исполнителя из команды, а старые задачи — вне окна.
    При «*» рамок нет вовсе.
    """
    if all_projects():
        return ""
    return jira_sync.build_jql(projects(), "", "", "", None)


def check_jql(jql: str) -> None:
    """JQL модели не должен выходить из рамок, в которые его вкладывают.

    Дыры закрываются здесь, проверкой строже самой Jira:
      - скобки: «x) OR (project = ЧУЖОЙ» вышла бы из нашей скобки. Скобки и
        кавычки должны закрываться внутри JQL модели;
      - обратная косая вне кавычек: в JQL «\"» — экранированный символ, а не
        кавычка, и на этом расхождении проверка скобок обманывается — отказ;
      - подзапросы: функции плагинов (issueFunction у ScriptRunner,
        subtaskOfQuery у JQL Tricks и т. п.) выполняют свой JQL без наших
        рамок, а наружу выходит число или факт связи. Чёрный список их не
        переловит, поэтому функции — только из белого списка встроенных;
      - номера и id чужих задач: «parent = ЧУЖОЙ-1» говорит, есть ли у чужой
        задачи что-то в наших проектах. Номера — только своих проектов (кроме
        текста после ~), числа от 10000 (внутренние id) — отказ.
    """
    # При «*» чужих проектов нет: номера и id проверять незачем
    allowed = None if all_projects() else set(projects())
    depth = 0
    quote = ""
    escaped = False
    buf = ""  # текст строки в кавычках
    prev = ""  # последний значимый символ перед открытием кавычки
    # JQL вне кавычек, строка заменена на §: так видно и «"поле" = 1», и
    # попытку назвать функцию строкой — «"f"(...)»
    outside: list[str] = []
    strings: list[tuple[str, str]] = []  # (символ перед строкой, её текст)
    for ch in jql:
        if quote:
            if escaped:
                # Только \" \' \\. Остальное (1 и т. п.) Jira раскодирует
                # в другие символы, чем видит проверка: "10..." для
                # неё «u0031», а для Jira — id задачи
                if ch not in "\"'\\":
                    raise JiraLiveError(
                        "В строках JQL допустимо экранировать только кавычки и \\\\. "
                        "Исправь условие."
                    )
                escaped = False
                buf += ch
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
                strings.append((prev, buf))
                outside.append(STRING)
                prev = STRING
            else:
                buf += ch
            continue
        if ch == "\\":
            raise JiraLiveError(
                "Обратная косая вне кавычек в JQL не допускается. Исправь условие."
            )
        if ch in "\"'":
            quote, buf = ch, ""
            continue
        if ch == "(":
            # Перед скобкой — функция из белого списка, слово JQL (in, and,
            # or, not, was...) или начало условия. Иначе это функция плагина
            text = "".join(outside)
            m = re.search(r"([A-Za-z_]\w*)\s*$", text)
            word = m.group(1).lower() if m else ""
            before = text.rstrip()[-1:]
            if m:
                ok = word in FUNCTIONS or word in KEYWORDS
            else:
                ok = before in ("", "(", ",")
            if not ok:
                raise JiraLiveError(
                    f"Функция «{word or before}» не допускается. Можно: "
                    + ", ".join(sorted(FUNCTIONS)) + "."
                )
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise JiraLiveError(
                    "В JQL лишняя закрывающая скобка. Исправь условие."
                )
        outside.append(ch)
        if not ch.isspace():
            prev = ch
    if quote:
        raise JiraLiveError("В JQL не закрыта кавычка. Исправь условие.")
    if depth:
        raise JiraLiveError("В JQL не закрыта скобка. Исправь условие.")

    bare = "".join(outside)
    if allowed is None:
        return
    # Текст после ~ — поиск по словам: «text ~ "CVE-2024-1"» законен
    for text in [bare] + [t for p, t in strings if p != "~"]:
        for key in ANY_KEY.findall(text):
            if key.split("-")[0].upper() not in allowed:
                raise JiraLiveError(
                    f"Задача {key} вне охвата: доступны проекты {', '.join(sorted(allowed))}."
                )
    # Задача по внутреннему id вместо номера — тот же обход: «parent = 10001»,
    # «"Epic Link" in (DEV-1, "10001")», «cf[10014] = 10001». Перечислять
    # поля бесполезно (у эпика и родителя имён много), поэтому правило
    # обратное: id задач в Jira начинаются с 10000, а законным числам в
    # условиях (оценки, приоритеты, сдвиги дат) столько цифр не нужно.
    # Строки подставляем текстом, кроме поиска по словам после ~
    named = bare
    for p, t in strings:
        named = named.replace(STRING, " " if p == "~" else f" {t} ", 1)
    named = re.sub(r"cf\[\d+\]", " ", named, flags=re.IGNORECASE)
    if BIG_NUMBER.search(named):
        raise JiraLiveError(
            "Числа от 10000 в JQL не допускаются: это внутренние id задач. "
            "Задачу указывай номером своего проекта (DEV-1), спринт — "
            "названием или openSprints()."
        )


def split_order(jql: str) -> tuple[str, str]:
    """JQL модели -> (условие, сортировка). Сортировка проверяется отдельно."""
    m = ORDER_BY.search(" " + jql)
    if not m:
        return jql.strip(), ""
    cond = (" " + jql)[: m.start()].strip()
    items = [i.strip() for i in m.group(1).split(",")]
    if not all(i and ORDER_ITEM.match(i) for i in items):
        raise JiraLiveError(
            "Непонятная сортировка. Пиши так: ORDER BY updated DESC, priority DESC."
        )
    return cond, ", ".join(items)


def build_query(jql: str) -> str:
    """JQL модели -> итоговый запрос внутри рамок."""
    jql = (jql or "").strip()
    if len(jql) > MAX_JQL:
        raise JiraLiveError(f"JQL длиннее {MAX_JQL} символов. Упрости условие.")
    cond, order = split_order(jql)
    if re.search(r"\bORDER\s+BY\b", cond, re.IGNORECASE):
        raise JiraLiveError("ORDER BY допустим только в самом конце JQL.")
    check_jql(cond)
    scope = scope_jql()
    parts = [scope] if scope else []
    if cond:
        parts.append(f"({cond})")
    query = " AND ".join(parts)
    tail = f"ORDER BY {order or 'updated DESC'}"
    return f"{query} {tail}" if query else tail


def _get_only(session) -> None:
    """Сессия requests, которая не умеет ничего, кроме GET.

    Client из jira/sync.py и так зовёт только GET, но гарантия должна быть
    здесь, а не в том, что никто не допишет в клиент лишний метод.
    """
    original = session.request

    def request(method, url, *args, **kwargs):
        if str(method).upper() != "GET":
            raise JiraLiveError(f"Запрещено: {method} в Jira. Только чтение.")
        return original(method, url, *args, **kwargs)

    session.request = request


_lock = threading.Lock()
# Один живой запрос за раз: порт без авторизации, и тяжёлые JQL пачкой не
# должны положить Jira. Остальные ждут, а не уходят параллельно
_one_at_a_time = threading.Semaphore(1)
_client: jira_sync.Client | None = None
_fmap: dict | None = None


def client() -> jira_sync.Client:
    global _client
    with _lock:
        if _client is None:
            c = jira_sync.Client(
                os.getenv("JIRA_URL", ""),
                token=os.getenv("JIRA_TOKEN", ""),
                user=os.getenv("JIRA_USER", ""),
                password=os.getenv("JIRA_PASSWORD", ""),
                timeout=TIMEOUT,
            )
            _get_only(c.session)
            # Без повторов: выгрузке они нужны, а здесь тяжёлый JQL модели
            # после таймаута ушёл бы в Jira ещё трижды
            for prefix in ("https://", "http://"):
                c.session.mount(prefix, HTTPAdapter(max_retries=0))
            _client = c
        return _client


def field_ids() -> dict:
    """Номера полей эпика, спринта и JIRA_FIELDS — один раз на процесс."""
    global _fmap
    if _fmap is not None:
        return _fmap
    try:
        all_fields = client().fields()
    except jira_sync.JiraError:
        all_fields = []
    fmap = jira_sync.field_map(all_fields, _env_list("JIRA_FIELDS"))
    # Сбой сети не должен навсегда оставить нас без эпика и спринта
    if all_fields:
        _fmap = fmap
    return fmap


def in_scope(key: str) -> bool:
    return all_projects() or key.split("-")[0].upper() in projects()


def _card(rec: dict, detailed: bool) -> dict:
    out = {
        "key": rec["key"],
        "url": rec["url"],
        "summary": rec["summary"],
        "status": rec["status"],
        "assignee": rec["assignee"] or "не назначена",
        "priority": rec["priority"],
        "updated": rec["updated"][:10],
    }
    if detailed:
        out.update(
            type=rec["type"],
            reporter=rec["reporter"],
            resolution=rec["resolution"],
            created=rec["created"][:10],
            labels=rec["labels"],
            components=rec["components"],
            fix_versions=rec["fix_versions"],
            epic=" ".join(x for x in (rec["epic"], rec["epic_name"]) if x),
            sprints=rec["sprints"],
            fields=rec["fields"],
            description=rec["description"][:DESCRIPTION_LIMIT],
        )
    return {k: v for k, v in out.items() if v not in ("", [], {})}


def query(
    jql: str = "",
    issue_key: str | None = None,
    count_only: bool = False,
    max_results: int = DEFAULT_RESULTS,
    detailed: bool = False,
) -> dict:
    """Выполнить запрос. Ошибки Jira превращаются в JiraLiveError с подсказкой."""
    if issue_key:
        key = issue_key.strip().upper()
        if not ISSUE_KEY.match(key):
            raise JiraLiveError(f"«{issue_key}» не похоже на номер задачи (PROJ-123).")
        if not in_scope(key):
            raise JiraLiveError(
                f"Проект {key.split('-')[0]} вне охвата: доступны {', '.join(projects())}."
            )
        jql = f'key = "{key}"'
        detailed = True

    full = build_query(jql)
    # Ждём недолго: очередь тяжёлых запросов не должна занять потоки kb
    if not _one_at_a_time.acquire(timeout=WAIT):
        raise JiraLiveError("Jira сейчас занята другим запросом — повтори чуть позже.")
    try:
        return _run(full, issue_key, count_only, max_results, detailed)
    finally:
        _one_at_a_time.release()


def _run(
    full: str, issue_key: str | None, count_only: bool, max_results: int, detailed: bool
) -> dict:
    c = client()
    try:
        if count_only:
            cond = full.rsplit("ORDER BY ", 1)[0].strip()
            return {"total": c.count(cond), "jql": full}

        fmap = field_ids()
        extra = [fmap.get("epic_link"), fmap.get("epic_name"), fmap.get("sprint")]
        extra += list((fmap.get("extra") or {}).values())
        fields = LIST_FIELDS + [f for f in extra if f]
        if issue_key:
            fields.append("comment")
        limit = max(1, min(int(max_results or DEFAULT_RESULTS), MAX_RESULTS))
        data = c.get(
            "/rest/api/2/search",
            jql=full,
            maxResults=limit,
            fields=",".join(fields),
        )
    except jira_sync.JiraError as e:
        text = str(e)
        if text.startswith("400"):
            raise JiraLiveError(
                "Jira не приняла JQL — исправь условие и позови снова.\n" + text
            ) from e
        raise JiraLiveError(f"Jira не ответила: {text}") from e

    issues = []
    for raw in data.get("issues", []):
        key = raw.get("key", "")
        # Вторая линия обороны: рамки уже в JQL, но задача из чужого проекта
        # не должна уйти модели ни при каком раскладе
        if not in_scope(key):
            continue
        comments = (((raw.get("fields") or {}).get("comment")) or {}).get("comments", [])
        rec = jira_sync.normalize(raw, c.base, comments if issue_key else [], fmap)
        card = _card(rec, detailed)
        if issue_key:
            card["comments"] = [
                {**cm, "text": cm["text"][:DESCRIPTION_LIMIT]}
                for cm in rec["comments"][-10:]
            ]
        issues.append(card)

    return {"total": int(data.get("total", 0)), "issues": issues, "jql": full}
