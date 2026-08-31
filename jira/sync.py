"""Выгрузка задач Jira Server/DC в файлы для последующей индексации.

Охват задаётся тремя рамками, и все они сужают выборку:
  - JIRA_PROJECTS — ключи проектов. Что не перечислено, не выгружается;
  - JIRA_TEAM / JIRA_TEAM_GROUP — состав команды. Внутри общего проекта
    берутся только задачи её людей;
  - JIRA_SINCE — глубина истории. Задачи, которые не трогали год, в поиске
    в основном шумят, а в индексации стоят дороже всего.
Это и есть граница: что не попало в выгрузку, того нет и в базе.
Аутентификации на портах пока нет, поэтому ограничивать надо не доступ к
индексу, а его содержимое.

Почему не через markdown и doc_index, как Confluence: у задачи важны не
только буквы, но и поля — исполнитель, статус, проект. Вопрос «какие задачи
на Иванове» решается фильтром по полю, а не близостью векторов: фамилия
исполнителя в тексте задачи обычно вообще не встречается. Поэтому здесь
выгружается структура (JSON на задачу), а kb/jira_index.py раскладывает её
по payload Qdrant.

Рассчитан на запуск в закрытом контуре без посторонней помощи, поэтому:
  - сначала проверяет связь и печатает, что видит (--check)
  - показывает, что будет сделано, ничего не записывая (--dry-run)
  - сохраняет сырой ответ API для разбора проблем (--dump-raw)
  - объясняет ошибки словами, а не трейсбеком

Настройки берутся из .env в корне проекта — того же, где живёт весь
остальной стек.

    JIRA_URL        https://jira.company.local
    JIRA_TOKEN      personal access token (профиль -> Personal Access Tokens)
    JIRA_USER       только для старых Jira без PAT: логин
    JIRA_PASSWORD   только для старых Jira без PAT: пароль
    JIRA_PROJECTS   DEVSEC,PLAT   ключи проектов через запятую
    JIRA_TEAM       логины команды через запятую: только их задачи
    JIRA_TEAM_GROUP имя группы Jira — то же самое, но составом рулит Jira
    JIRA_SINCE      глубина истории: 30d, 6M или 2026-08-01
    JIRA_JQL        дополнительное условие, если нужно что-то своё
    JIRA_COMMENTS   1 — тянуть комментарии (по умолчанию), 0 — только описания
    JIRA_OUT        куда складывать (по умолчанию ./jira/issues)

Использование:
    python jira/sync.py --check       проверить связь и посчитать задачи
    python jira/sync.py --dry-run     показать, что будет выгружено
    python jira/sync.py               выгрузить изменившееся
    python jira/sync.py --full        игнорировать даты, выгрузить всё

Дальше выгруженное индексируется:
    docker compose exec kb python -m kb.jira_index /docs/jira

Чего НЕ делает: не замечает удалённые и перенесённые задачи — они останутся
в выгрузке и в индексе. Лечится полной переиндексацией (--full плюс очистка
каталога) до тех пор, пока это не станет мешать.
"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).parent

# Поля, которые забираем у задачи. Меньше полей — меньше трафика и меньше
# шансов упереться в ограничения старых Jira на размер ответа
FIELDS = [
    "summary",
    "description",
    "status",
    "assignee",
    "reporter",
    "issuetype",
    "priority",
    "resolution",
    "labels",
    "components",
    "created",
    "updated",
]

# Сколько задач за один запрос. У Data Center потолок обычно 100 и настраивается
# администратором; с комментариями ответ тяжёлый, поэтому берём с запасом вниз
PAGE_SIZE = 50


def load_env() -> None:
    """Читает настройки из .env проекта.

    Ищет рядом со скриптом (jira/.env) и в корне проекта (../.env). Достаточно
    одного файла — обычно корневого, там же лежат остальные настройки стека.
    Уже заданные переменные окружения не перетираются: они важнее файла.
    """
    for env_file in (HERE / ".env", HERE.parent / ".env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class JiraError(RuntimeError):
    """Ошибка с человеческим объяснением, а не с трейсбеком."""


def wiki_to_text(raw: str) -> str:
    """Разметка Jira -> обычный текст.

    Jira Server отдаёт описание в своей вики-разметке. Полноценный конвертер
    здесь не нужен: текст идёт в эмбеддинги, где важны слова, а не оформление.
    Убираем то, что мешает читать — служебные макросы и обёртки ссылок, —
    и не трогаем остальное. Пытаться разбирать *жирный* и -зачёркнутый- вредно:
    те же символы сплошь и рядом стоят в тексте просто так.
    """
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n")
    # {{моноширинный}} -> моноширинный. Строго ПЕРВЫМ: если сначала вычистить
    # макросы, то из {{code}} останется макрос {code}, а от него — рваные скобки
    text = re.sub(r"\{\{(.+?)\}\}", r"\1", text)
    # Блоки кода и цитат: содержимое ценно, обёртка — нет
    text = re.sub(r"\{code(:[^}]*)?\}", "\n", text)
    text = re.sub(r"\{noformat\}|\{quote\}|\{panel(:[^}]*)?\}", "\n", text)
    text = re.sub(r"\{color(:[^}]*)?\}", "", text)
    # [подпись|ссылка] -> подпись (ссылка), [ссылка] -> ссылка
    text = re.sub(r"\[([^\]|]+)\|([^\]]+)\]", r"\1 (\2)", text)
    text = re.sub(r"\[([^\]|]+)\]", r"\1", text)
    # h2. Заголовок -> Заголовок
    text = re.sub(r"^h[1-6]\.\s*", "", text, flags=re.MULTILINE)
    # !картинка.png! — в текстовом виде бесполезна
    text = re.sub(r"!([^!\n]+)!", "", text)
    # Пустые строки пачками появляются после вырезанных макросов
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def person(field: dict | None) -> dict:
    """Пользователь Jira -> имя и логин.

    displayName — то, чем человека называют вслух («Иванов Иван»), по нему и
    будут спрашивать. name/key — логин, он же попадает в latin-написание
    вопросов вроде «задачи i.ivanov». Храним оба.
    """
    if not field:
        return {"name": "", "login": ""}
    return {
        "name": (field.get("displayName") or "").strip(),
        # В DC логин лежит в name, в более новых сборках — в key
        "login": (field.get("name") or field.get("key") or "").strip(),
    }


class Client:
    """HTTP-клиент Jira Server/DC.

    По умолчанию токен уходит заголовком `Authorization: Bearer <token>` —
    так его ждёт Jira Data Center начиная с 8.14. Если PAT в вашей сборке нет,
    задайте JIRA_USER и JIRA_PASSWORD: тогда используется Basic auth.
    У Jira Cloud схема третья (почта + API-токен, путь /rest/api/3, описание в
    формате ADF) — она здесь не реализована намеренно.
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        user: str = "",
        password: str = "",
        timeout: int = 60,
    ):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.last_raw: str = ""

        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        elif user:
            pair = base64.b64encode(f"{user}:{password}".encode()).decode()
            self.session.headers["Authorization"] = f"Basic {pair}"

        # Повторы на сетевых сбоях и 5xx: при выгрузке тысяч задач одна
        # случайная ошибка не должна ронять весь прогон. 4xx не повторяем —
        # неверный токен или путь от повтора не исправится
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def get(self, path: str, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}

        try:
            resp = self.session.get(
                f"{self.base}{path}", params=clean, timeout=self.timeout
            )
        except requests.exceptions.SSLError as e:
            raise JiraError(
                f"Ошибка TLS при обращении к {self.base}: {e}\n"
                "  - во внутреннем контуре часто самоподписанный сертификат\n"
                "  - добавьте корневой сертификат компании в доверенные либо\n"
                "    укажите путь к нему в REQUESTS_CA_BUNDLE"
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise JiraError(
                f"Не удалось соединиться с {self.base}: {e}\n"
                "  - проверьте, что адрес доступен с этой машины (ping, curl)\n"
                "  - если сеть за прокси, задайте HTTPS_PROXY"
            ) from e
        except requests.exceptions.Timeout as e:
            raise JiraError(
                f"Таймаут {self.timeout} с при запросе {path}. "
                "Jira отвечает медленно или недоступна"
            ) from e

        if resp.status_code == 401:
            raise JiraError(
                "401: токен не принят.\n"
                "  - Jira Server/DC ждёт Personal Access Token в заголовке "
                "Bearer (профиль -> Personal Access Tokens)\n"
                "  - в сборках старше 8.14 персональных токенов нет: задайте "
                "JIRA_USER и JIRA_PASSWORD, пойдём через Basic auth\n"
                "  - если у вас Jira Cloud, там другая схема и другой путь "
                "(/rest/api/3), этот скрипт её не умеет"
            )
        if resp.status_code == 403:
            raise JiraError(
                f"403: доступ запрещён на {path}\n"
                "  Токен принят (иначе был бы 401), но прав не хватает.\n"
                "  - проверьте, что учётная запись видит эти проекты в вебе\n"
                "  - частая причина у Jira DC: включён CAPTCHA-фильтр после\n"
                "    неудачных входов, зайдите в веб-интерфейс и разблокируйте\n"
                "  - проверьте тем же токеном вручную:\n"
                f'    curl -H "Authorization: Bearer <токен>" '
                f'"{self.base}/rest/api/2/myself"'
            )
        if resp.status_code == 404:
            raise JiraError(
                f"404: путь {path} не найден.\n"
                "  - проверьте URL: у Jira часто есть префикс, "
                "например https://host/jira\n"
                "  - в Cloud путь другой: /rest/api/3/..."
            )
        if resp.status_code == 400:
            # Почти всегда это синтаксис JQL или несуществующий ключ проекта
            raise JiraError(
                f"400: Jira не приняла запрос.\n  {resp.text[:400]}\n"
                "  Чаще всего дело в JQL: проверьте JIRA_PROJECTS и JIRA_JQL"
            )
        if not resp.ok:
            raise JiraError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        self.last_raw = resp.text
        try:
            return resp.json()
        except ValueError as e:
            raise JiraError(
                "Ответ не является JSON. Обычно это страница логина: "
                "значит запрос ушёл неаутентифицированным или URL ведёт не в API"
            ) from e

    def myself(self) -> dict:
        return self.get("/rest/api/2/myself")

    def count(self, jql: str) -> int:
        """Сколько задач под условие. maxResults=0 — Jira отдаёт только total."""
        return int(self.get("/rest/api/2/search", jql=jql, maxResults=0).get("total", 0))

    def search(self, jql: str, with_comments: bool, page_size: int = PAGE_SIZE):
        """Задачи под условие, постранично.

        Сортировка по updated ASC не случайна: если во время выгрузки кто-то
        правит задачи, при сортировке по убыванию они переезжают на первую
        страницу и сдвигают всё остальное — часть задач тогда пропускается.
        """
        fields = list(FIELDS)
        if with_comments:
            fields.append("comment")

        start = 0
        while True:
            data = self.get(
                "/rest/api/2/search",
                jql=f"{jql} ORDER BY updated ASC",
                startAt=start,
                maxResults=page_size,
                fields=",".join(fields),
            )
            issues = data.get("issues", [])
            for issue in issues:
                yield issue
            start += len(issues)
            if not issues or start >= int(data.get("total", 0)):
                return

    def comments(self, key: str, page_size: int = 100) -> list[dict]:
        """Все комментарии задачи отдельным запросом.

        Нужен, когда в общей выдаче комментарии обрезаны: Jira возвращает их
        порцией, а у болтливых задач порция не покрывает всё обсуждение.
        """
        out: list[dict] = []
        start = 0
        while True:
            data = self.get(
                f"/rest/api/2/issue/{key}/comment", startAt=start, maxResults=page_size
            )
            batch = data.get("comments", [])
            out.extend(batch)
            start += len(batch)
            if not batch or start >= int(data.get("total", 0)):
                return out


def team_condition(logins: list[str], group: str) -> str:
    """Условие «задачи моей команды».

    Команду можно задать двумя способами, они складываются через OR: списком
    логинов (JIRA_TEAM) и группой Jira (JIRA_TEAM_GROUP). Группа удобнее —
    состав меняется в Jira, а не в .env, — но право видеть состав группы есть
    не у всех учётных записей; тогда остаётся список.

    ВНИМАНИЕ: условие по исполнителю отсекает задачи БЕЗ исполнителя, то есть
    весь бэклог команды. Если он нужен, добавьте его в JIRA_JQL явно.
    """
    parts = []
    if logins:
        parts.append("assignee in (" + ", ".join(f'"{u}"' for u in logins) + ")")
    if group:
        parts.append(f'assignee in membersOf("{group}")')
    return " OR ".join(parts)


def window_condition(value: str) -> str:
    """JIRA_SINCE -> ограничение по глубине истории.

    Принимает и относительный срок в понятиях JQL («30d», «-30d», «6M»), и
    обычную дату («2026-08-01»). Старые задачи в поиске в основном шумят, а
    в индексации стоят дороже всего — окно в месяц-два обычно и есть то, что
    нужно на самом деле.

    ОСТОРОЖНО с буквами: в JQL `m` — это МИНУТЫ, а месяцы — заглавная `M`.
    «1m» вместо «1M» даёт окно в одну минуту и пустую выгрузку, поэтому на
    строчную `m` предупреждаем вслух.
    """
    v = value.strip()
    if not v:
        return ""
    if re.fullmatch(r"-?\d+[mhdwM]", v):
        if v.rstrip("-").endswith("m"):
            print(
                f"    [!] JIRA_SINCE={v}: строчная 'm' в JQL значит МИНУТЫ. "
                "Месяцы — заглавная 'M', дни — 'd'"
            )
        return f"updated >= {v if v.startswith('-') else '-' + v}"
    return f'updated >= "{v}"'


def build_jql(
    projects: list[str], team: str, window: str, extra: str, since: str | None
) -> str:
    """Условие выборки: проекты, состав команды, окно, своё условие и даты."""
    quoted = ", ".join(f'"{p}"' for p in projects)
    parts = [f"project in ({quoted})"]
    if team:
        parts.append(f"({team})")
    if window:
        parts.append(window)
    if extra:
        parts.append(f"({extra})")
    if since:
        # Инкрементальность поверх окна: обе границы нижние, побеждает
        # ближняя, поэтому конфликта нет
        parts.append(f'updated >= "{since}"')
    return " AND ".join(parts)


def normalize(issue: dict, base_url: str, comments: list[dict]) -> dict:
    """Ответ Jira -> плоская запись, из которой строится индекс."""
    f = issue.get("fields", {}) or {}
    key = issue.get("key", "")
    status = f.get("status") or {}
    assignee = person(f.get("assignee"))
    reporter = person(f.get("reporter"))

    return {
        "key": key,
        "project": key.split("-")[0] if "-" in key else "",
        "url": f"{base_url}/browse/{key}",
        "summary": (f.get("summary") or "").strip(),
        "description": wiki_to_text(f.get("description") or ""),
        "status": (status.get("name") or "").strip(),
        # Категория статуса не зависит от того, как в проекте назвали колонки:
        # new / indeterminate / done. По ней отвечаем на «что не закрыто»
        "status_category": ((status.get("statusCategory") or {}).get("key") or ""),
        "type": ((f.get("issuetype") or {}).get("name") or "").strip(),
        "priority": ((f.get("priority") or {}).get("name") or "").strip(),
        "resolution": ((f.get("resolution") or {}).get("name") or "").strip(),
        "assignee": assignee["name"],
        "assignee_login": assignee["login"],
        "reporter": reporter["name"],
        "reporter_login": reporter["login"],
        "labels": list(f.get("labels") or []),
        "components": [c.get("name", "") for c in (f.get("components") or [])],
        "created": f.get("created") or "",
        "updated": f.get("updated") or "",
        "comments": [
            {
                "author": person(c.get("author"))["name"],
                "created": c.get("created", ""),
                "text": wiki_to_text(c.get("body") or ""),
            }
            for c in comments
        ],
    }


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def main() -> int:
    load_env()

    ap = argparse.ArgumentParser(description="Выгрузка задач Jira")
    ap.add_argument("--check", action="store_true", help="проверить связь и выйти")
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    ap.add_argument("--full", action="store_true", help="выгрузить всё, игнорируя даты")
    ap.add_argument("--dump-raw", metavar="FILE", help="сохранить сырой ответ API")
    ap.add_argument("--limit", type=int, help="ограничить число задач (для пробы)")
    ap.add_argument(
        "--since",
        metavar="СРОК",
        help="брать изменённые не раньше: 30d, 6M или 2026-08-01. "
        "Перебивает JIRA_SINCE из .env",
    )
    args = ap.parse_args()

    url = os.getenv("JIRA_URL", "").strip()
    token = os.getenv("JIRA_TOKEN", "").strip()
    user = os.getenv("JIRA_USER", "").strip()
    password = os.getenv("JIRA_PASSWORD", "").strip()
    projects_env = os.getenv("JIRA_PROJECTS", "").strip()
    team_env = os.getenv("JIRA_TEAM", "").strip()
    team_group = os.getenv("JIRA_TEAM_GROUP", "").strip()
    since_env = (args.since or os.getenv("JIRA_SINCE", "")).strip()
    extra_jql = os.getenv("JIRA_JQL", "").strip()
    with_comments = os.getenv("JIRA_COMMENTS", "1").strip() != "0"
    out_dir = Path(os.getenv("JIRA_OUT", str(HERE / "issues")))

    if not url or not (token or user):
        print("Не заданы JIRA_URL и JIRA_TOKEN.")
        print(f"Пропишите их в {HERE.parent / '.env'} — там же, где остальные")
        print("настройки стека. После правки: docker compose up -d kb")
        return 2

    projects = [p.strip().upper() for p in projects_env.split(",") if p.strip()]
    if not projects:
        print("Не задан JIRA_PROJECTS — ключи проектов через запятую.")
        print("Это единственная граница выгрузки: что не перечислено, в базу")
        print("не попадёт. Ключ виден в номере задачи: DEVSEC-412 -> DEVSEC.")
        return 2

    client = Client(url, token=token, user=user, password=password)

    try:
        me = client.myself()
        print(f"Подключились как: {me.get('displayName')} ({me.get('name')})")
    except JiraError as e:
        print(f"Не удалось подключиться.\n{e}")
        return 1

    team_logins = [u.strip() for u in team_env.split(",") if u.strip()]
    team = team_condition(team_logins, team_group)
    window = window_condition(since_env)

    print(f"Проекты: {', '.join(projects)}")
    if team:
        print(f"Команда: {team}")
    else:
        print("Команда: не задана — поедут ВСЕ задачи перечисленных проектов")
    if window:
        print(f"Окно: {window}")
    else:
        print("Окно: не задано — поедет вся история проектов")
    if extra_jql:
        print(f"Доп. условие: {extra_jql}")
    print(f"Комментарии: {'да' if with_comments else 'нет'}")

    if args.check:
        for project in projects:
            try:
                whole = client.count(build_jql([project], "", "", "", None))
                mine = client.count(
                    build_jql([project], team, window, extra_jql, None)
                )
                # Обе цифры сразу: видно, насколько сузила выборка команды.
                # Если они совпали, условие не сработало — например, в JIRA_TEAM
                # указаны отображаемые имена вместо логинов
                print(f"    {project:12} задач всего: {whole:6}   к выгрузке: {mine}")
            except JiraError as e:
                print(f"    {project:12} недоступен.\n{e}")
                return 1
        return 0

    state_path = out_dir / ".sync_state.json"
    state = {} if args.full else load_state(state_path)
    since = None if args.full else state.get("synced_until")

    # Момент начала прогона с запасом назад: пока идёт выгрузка, кто-то
    # правит задачи, и без запаса такие правки провалятся между прогонами
    started = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")

    jql = build_jql(projects, team, window, extra_jql, since)
    print(f"\nJQL: {jql}")

    try:
        total = client.count(jql)
    except JiraError as e:
        print(f"\n{e}")
        return 1

    if since:
        print(f"Изменилось с {since}: {total}")
    else:
        print(f"Задач к выгрузке: {total}")

    if not total:
        print("\nНечего выгружать — с прошлого раза ничего не менялось.")
        return 0

    stats = {"новых": 0, "обновлено": 0, "ошибок": 0}
    processed = 0

    try:
        for issue in client.search(jql, with_comments):
            if args.limit and processed >= args.limit:
                break
            processed += 1

            key = issue.get("key", "")
            fields = issue.get("fields", {}) or {}

            if args.dump_raw and processed == 1:
                Path(args.dump_raw).write_text(client.last_raw, encoding="utf-8")
                print(f"\nСырой ответ сохранён: {args.dump_raw}")

            comments: list[dict] = []
            if with_comments:
                block = fields.get("comment") or {}
                comments = block.get("comments") or []
                # Обсуждение длиннее одной порции — дочитываем отдельно.
                # Иначе у самых обсуждаемых задач (а они обычно и самые
                # интересные) в индекс попадёт только начало разговора
                if int(block.get("total", 0)) > len(comments):
                    try:
                        comments = client.comments(key)
                    except JiraError as e:
                        print(f"  [!] {key}: комментарии не дочитаны ({e})")

            record = normalize(issue, client.base, comments)
            target = out_dir / record["project"] / f"{key}.json"
            action = "обновлено" if target.exists() else "новых"
            stats[action] += 1

            if args.dry_run:
                print(
                    f"  [{action:9}] {key:14} {record['summary'][:60]} "
                    f"(комментариев: {len(comments)})"
                )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            if processed % 100 == 0:
                print(f"    ...{processed} из {total}")
    except JiraError as e:
        print(f"\nВыгрузка прервана: {e}")
        return 1

    print("\nИтог:")
    for name, value in stats.items():
        print(f"    {name:12} {value}")

    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "synced_until": started,
                    "synced_at": datetime.now().isoformat(),
                    "projects": projects,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nФайлы: {out_dir}")
        print(f"Состояние: {state_path} (для инкрементальной синхронизации)")
        print("\nДальше проиндексировать:")
        print(f'    docker compose exec kb python -m kb.jira_index "{out_dir}"')

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано")
        sys.exit(130)
