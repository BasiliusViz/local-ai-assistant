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
    JIRA_FIELDS     свои поля через запятую: «Стрим заказчика,customfield_12345».
                    Эпик и спринт находятся сами. Какие поля есть — --fields
    JIRA_OUT        куда складывать (по умолчанию ./jira/issues)

Использование:
    python jira/sync.py --check       проверить связь и посчитать задачи
    python jira/sync.py --dry-run     показать, что будет выгружено
    python jira/sync.py               выгрузить изменившееся
    python jira/sync.py --full        игнорировать даты, выгрузить всё
    python jira/sync.py --fields      какие доп. поля заполнены (для JIRA_FIELDS)

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
    "fixVersions",
    "parent",
]

# Поля Jira Software узнаём по типу, а не по имени: имя зависит от языка
# интерфейса («Epic Link» / «Ссылка на эпик»), а тип — нет. Номер
# customfield_NNNNN у каждой Jira свой, поэтому зашивать его нельзя
EPIC_LINK_TYPE = "com.pyxis.greenhopper.jira:gh-epic-link"
EPIC_NAME_TYPE = "com.pyxis.greenhopper.jira:gh-epic-label"
SPRINT_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"

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

    def fields(self) -> list[dict]:
        """Все поля Jira: id, отображаемое имя и тип (schema.custom)."""
        data = self.get("/rest/api/2/field")
        return data if isinstance(data, list) else []

    def count(self, jql: str) -> int:
        """Сколько задач под условие. maxResults=0 — Jira отдаёт только total."""
        return int(self.get("/rest/api/2/search", jql=jql, maxResults=0).get("total", 0))

    def keys(self, jql: str, page_size: int = 500):
        """Только номера задач под условие — для сверки.

        Без описаний и комментариев ответ в десятки раз легче обычной выгрузки,
        поэтому весь охват проходится быстро. Jira сама урезает размер страницы
        до своего предела, так что шаг считаем по фактически пришедшему, а не по
        запрошенному. Сортировка по номеру — чтобы страницы не съезжали, если
        кто-то правит задачи прямо во время сверки.
        """
        start = 0
        while True:
            data = self.get(
                "/rest/api/2/search",
                jql=f"{jql} ORDER BY key ASC",
                startAt=start,
                maxResults=page_size,
                fields="key",
            )
            issues = data.get("issues", [])
            for issue in issues:
                if issue.get("key"):
                    yield issue["key"]
            start += len(issues)
            if not issues or start >= int(data.get("total", 0)):
                return

    def search(
        self,
        jql: str,
        with_comments: bool,
        page_size: int = PAGE_SIZE,
        extra_fields: list[str] | None = None,
    ):
        """Задачи под условие, постранично.

        Сортировка по updated ASC не случайна: если во время выгрузки кто-то
        правит задачи, при сортировке по убыванию они переезжают на первую
        страницу и сдвигают всё остальное — часть задач тогда пропускается.
        """
        fields = list(FIELDS) + list(extra_fields or [])
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


def field_map(all_fields: list[dict], wanted: list[str]) -> dict:
    """Какие дополнительные поля забирать и как они называются.

    Эпик и спринт находятся сами, по типу поля. Остальное перечисляется в
    JIRA_FIELDS отображаемыми именами («Стрим заказчика») или номерами
    (customfield_12345): имя удобнее, номер надёжнее, если имён два одинаковых.

    Возвращает {"epic_link": id, "epic_name": id, "sprint": id,
    "extra": {имя: id}, "missing": [чего не нашли]}.
    """
    out: dict = {"epic_link": "", "epic_name": "", "sprint": "", "extra": {}, "missing": []}
    for fld in all_fields:
        kind = (fld.get("schema") or {}).get("custom", "")
        if kind == EPIC_LINK_TYPE and not out["epic_link"]:
            out["epic_link"] = fld.get("id", "")
        elif kind == EPIC_NAME_TYPE and not out["epic_name"]:
            out["epic_name"] = fld.get("id", "")
        elif kind == SPRINT_TYPE and not out["sprint"]:
            out["sprint"] = fld.get("id", "")

    by_id = {f.get("id", ""): f for f in all_fields}
    by_name: dict[str, list[dict]] = {}
    for fld in all_fields:
        by_name.setdefault((fld.get("name") or "").strip().casefold(), []).append(fld)

    for item in wanted:
        if item in by_id:
            fld = by_id[item]
        else:
            same = by_name.get(item.casefold(), [])
            if not same:
                out["missing"].append(item)
                continue
            if len(same) > 1:
                ids = ", ".join(f.get("id", "") for f in same)
                print(
                    f"    [!] Полей с именем «{item}» несколько ({ids}), беру первое. "
                    "Укажите в JIRA_FIELDS нужный номер"
                )
            fld = same[0]
        fid = fld.get("id", "")
        # Эпик и спринт и так едут отдельными полями, второй раз не нужен
        if fid in (out["epic_link"], out["epic_name"], out["sprint"]):
            continue
        out["extra"][(fld.get("name") or fid).strip()] = fid
    return out


def field_text(value) -> list[str]:
    """Значение поля любого типа -> список строк.

    Поля Jira бывают строкой, числом, выбором из списка ({"value": ...}),
    каскадным выбором (value + child), пользователем, версией, компонентом
    и списком всего этого. Для поиска нужно одно: как значение читается.
    """
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, bool):
        return ["да" if value else "нет"]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            out.extend(field_text(v))
        return out
    if isinstance(value, dict):
        if "value" in value:
            text = str(value.get("value") or "").strip()
            child = (value.get("child") or {}).get("value")
            if child:
                text = f"{text} / {child}"
            return [text] if text else []
        for k in ("displayName", "name", "key"):
            if value.get(k):
                return [str(value[k]).strip()]
    return []


# Старые Jira отдают спринт не объектом, а строкой toString() Java:
# com.atlassian.greenhopper.service.sprint.Sprint@1f[id=12,state=ACTIVE,
# name=Спринт 5, команда А,startDate=...]. В имени бывают запятые, поэтому
# имя режем до следующего известного ключа, а не до первой запятой
SPRINT_NAME = re.compile(
    r"name=(.*?),(?:goal|startDate|endDate|completeDate|activatedDate|sequence|"
    r"rapidViewId|autoStartStop|synced|incompleteIssuesDestinationId)="
)
SPRINT_STATE = re.compile(r"state=(\w+)")


def sprints(value) -> list[dict]:
    """Поле «Спринт» -> [{"name": ..., "state": "active|closed|future"}]."""
    out = []
    for item in value or []:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            state = (item.get("state") or "").strip().lower()
        else:
            text = str(item)
            m = SPRINT_NAME.search(text)
            if not m:
                m = re.search(r"name=([^,\]]*)", text)
            name = m.group(1).strip() if m else ""
            s = SPRINT_STATE.search(text)
            state = s.group(1).lower() if s else ""
        if name:
            out.append({"name": name, "state": state})
    return out


def normalize(
    issue: dict,
    base_url: str,
    comments: list[dict],
    fmap: dict | None = None,
    epic_names: dict | None = None,
) -> dict:
    """Ответ Jira -> плоская запись, из которой строится индекс.

    epic_names — {номер эпика: название}: у задачи в поле эпика только номер,
    а спрашивают по названию («задачи эпика Импорт»).
    """
    f = issue.get("fields", {}) or {}
    fmap = fmap or {}
    epic_names = epic_names or {}
    key = issue.get("key", "")
    status = f.get("status") or {}
    assignee = person(f.get("assignee"))
    reporter = person(f.get("reporter"))

    # Эпик: у обычной задачи — ссылка на него, а у самого эпика есть
    # «Имя эпика». Эпик считаем частью самого себя: «задачи эпика X» без него
    # выглядели бы обрезанными
    own_epic_name = ""
    if fmap.get("epic_name"):
        own_epic_name = " ".join(field_text(f.get(fmap["epic_name"])))
    epic = ""
    if fmap.get("epic_link"):
        epic = " ".join(field_text(f.get(fmap["epic_link"])))
    if own_epic_name and not epic:
        epic = key
    epic_name = own_epic_name if epic == key else epic_names.get(epic, "")

    sprint_list = sprints(f.get(fmap["sprint"])) if fmap.get("sprint") else []
    parent = f.get("parent") or {}

    extra = {}
    for name, fid in (fmap.get("extra") or {}).items():
        vals = field_text(f.get(fid))
        if vals:
            extra[name] = vals

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
        "fix_versions": field_text(f.get("fixVersions")),
        "parent": parent.get("key", ""),
        "parent_summary": ((parent.get("fields") or {}).get("summary") or "").strip(),
        "epic": epic,
        "epic_name": epic_name,
        # Все спринты задачи: переходящая задача бывает в нескольких
        "sprints": [s["name"] for s in sprint_list],
        "active_sprints": [s["name"] for s in sprint_list if s["state"] == "active"],
        # Поля из JIRA_FIELDS: {отображаемое имя: [значения]}
        "fields": extra,
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


def epic_titles(
    client: "Client", keys: set[str], fmap: dict, cache: dict
) -> dict:
    """Названия эпиков по номерам, с запоминанием между прогонами.

    Эпик часто лежит в другом проекте или вне окна JIRA_SINCE, поэтому из
    самой выгрузки его название не взять. Спрашиваем Jira одним запросом на
    пачку номеров. Эпик, который токену не виден, запоминаем пустым, чтобы не
    спрашивать про него каждый прогон.
    """
    todo = sorted(k for k in keys if k and k not in cache)
    fields = "summary" + (f",{fmap['epic_name']}" if fmap.get("epic_name") else "")
    for start in range(0, len(todo), 50):
        part = todo[start : start + 50]
        jql = "key in (" + ", ".join(part) + ")"
        try:
            data = client.get(
                "/rest/api/2/search", jql=jql, maxResults=len(part), fields=fields
            )
        except JiraError:
            # Один невидимый номер валит весь запрос (400) — тогда по одному
            data = {"issues": []}
            for k in part:
                try:
                    data["issues"].append(
                        client.get(f"/rest/api/2/issue/{k}", fields=fields)
                    )
                except JiraError:
                    pass
        for issue in data.get("issues", []):
            f = issue.get("fields") or {}
            name = ""
            if fmap.get("epic_name"):
                name = " ".join(field_text(f.get(fmap["epic_name"])))
            cache[issue.get("key", "")] = name or (f.get("summary") or "").strip()
        for k in part:
            cache.setdefault(k, "")
    return cache


def show_fields(client: "Client", jql: str, fmap: dict, sample: int = 100) -> int:
    """--fields: какие поля реально заполнены в выгружаемых задачах.

    Полей в Jira сотни, и по названию из интерфейса не всегда понятно, какое
    из них нужно. Берём пробную порцию задач со всеми полями и показываем
    заполненные — с примером значения. Отсюда имя и идёт в JIRA_FIELDS.
    """
    all_fields = {f.get("id", ""): f for f in client.fields()}
    data = client.get(
        "/rest/api/2/search",
        jql=f"{jql} ORDER BY updated DESC",
        maxResults=sample,
        fields="*all",
    )
    issues = data.get("issues", [])
    filled: dict[str, list] = {}
    for issue in issues:
        for fid, value in (issue.get("fields") or {}).items():
            if not fid.startswith("customfield_"):
                continue
            text = sprints(value) if fid == fmap.get("sprint") else field_text(value)
            if text:
                filled.setdefault(fid, []).append(text)

    auto = {fmap.get("epic_link"): "эпик", fmap.get("epic_name"): "имя эпика",
            fmap.get("sprint"): "спринт"}
    chosen = set((fmap.get("extra") or {}).values())
    print(f"\nЗаполненные дополнительные поля в {len(issues)} последних задачах:\n")
    rows = sorted(filled.items(), key=lambda kv: -len(kv[1]))
    for fid, vals in rows:
        name = (all_fields.get(fid) or {}).get("name", fid)
        mark = f"[{auto[fid]}, берётся само]" if fid in auto else (
            "[в JIRA_FIELDS]" if fid in chosen else ""
        )
        example = vals[0]
        if isinstance(example, list) and example and isinstance(example[0], dict):
            example = [s["name"] for s in example]
        example = ", ".join(map(str, example))[:60]
        print(f"  {len(vals):4}  {fid:20} {name[:40]:40} {mark}")
        print(f"        пример: {example}")
    print(
        "\nНужные поля перечислите в .env через запятую, по имени или номеру:\n"
        "    JIRA_FIELDS=Стрим заказчика,customfield_12345\n"
        "Потом полная выгрузка и индексация: sync.py --full, jira_index --full"
    )
    return 0


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def prune(client: Client, out_dir: Path, jql: str, dry_run: bool, force: bool) -> int:
    """Убрать с диска задачи, вышедшие из охвата.

    Обычная выгрузка берёт только изменившееся с прошлого раза и потому не
    видит, что задача пропала: её удалили, перенесли в чужой проект,
    переназначили на человека вне команды или она выпала из окна JIRA_SINCE.
    Сверка запрашивает номера ВСЕХ задач в охвате — лёгкий запрос, без
    описаний — и удаляет файлы тех, кого там нет. Индексатор затем вычистит их
    чанки из поиска.

    Запускать раз в сутки: полный проход обычному прогону не нужен, а чаще
    смысла нет — устаревшая задача провисит в поиске не дольше суток.
    """
    print(f"\nСверка с Jira: {jql}")
    try:
        in_scope = set(client.keys(jql))
    except JiraError as e:
        # Недочитанный список нельзя использовать для удаления: всё, что не
        # успело прийти, выглядело бы вышедшим из охвата
        print(f"\nСверка прервана, ничего не удалено: {e}")
        return 1

    on_disk = {
        p.stem: p for p in out_dir.rglob("*.json") if not p.name.startswith(".")
    }
    vanished = sorted(set(on_disk) - in_scope)
    print(
        f"В охвате Jira: {len(in_scope)}, на диске: {len(on_disk)}, "
        f"вне охвата: {len(vanished)}"
    )

    # Защита от ошибки вместо удаления. Если Jira вернула пустой список, а на
    # диске задачи есть, или «пропало» больше половины разом, это почти всегда
    # не удалённые задачи, а отобранные у токена права, поменявшийся JIRA_TEAM
    # или JQL, который стал отбирать не то. Молча стереть индекс из-за этого
    # хуже, чем подержать устаревшее ещё сутки
    suspicious = bool(on_disk) and (
        not in_scope or len(vanished) > max(10, len(on_disk) // 2)
    )
    if suspicious and not force:
        print(
            f"\n[!] Вне охвата оказалось {len(vanished)} задач из {len(on_disk)} — "
            "это похоже на смену прав или условий выборки, а не на удаление.\n"
            "    Файлы не трогаю. Проверьте JIRA_PROJECTS, JIRA_TEAM и доступ "
            "токена; если всё верно, запустите с --force-prune"
        )
        return 0

    for key in vanished:
        if dry_run:
            print(f"  [вне охвата] {key}")
            continue
        on_disk[key].unlink()
        print(f"  удалена: {key}")

    if vanished and not dry_run:
        print("\nДальше проиндексировать — индексатор вычистит их из поиска:")
        print(f'    docker compose exec kb python -m kb.jira_index "{out_dir}"')
    return 0


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
    ap.add_argument(
        "--prune",
        action="store_true",
        help="сверка: убрать с диска задачи, вышедшие из охвата, и выйти",
    )
    ap.add_argument(
        "--fields",
        action="store_true",
        help="показать заполненные дополнительные поля (для JIRA_FIELDS) и выйти",
    )
    ap.add_argument(
        "--force-prune",
        action="store_true",
        help="при сверке удалить, даже если вне охвата подозрительно много",
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
    wanted_fields = [
        x.strip() for x in os.getenv("JIRA_FIELDS", "").split(",") if x.strip()
    ]
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

    # Без списка полей выгрузка всё равно работает — просто без эпиков,
    # спринтов и своих полей. Ронять из-за этого весь прогон незачем
    try:
        fmap = field_map(client.fields(), wanted_fields)
    except JiraError as e:
        print(f"    [!] Список полей Jira не получен, эпики и спринты не поедут: {e}")
        fmap = field_map([], [])
    found = [
        name
        for name, fid in (("эпик", fmap["epic_link"]), ("спринт", fmap["sprint"]))
        if fid
    ] + list(fmap["extra"])
    print(f"Доп. поля: {', '.join(found) if found else 'нет'}")
    if fmap["missing"]:
        print(
            f"    [!] В Jira нет полей: {', '.join(fmap['missing'])}. "
            "Имена и номера видны в python jira/sync.py --fields"
        )
    extra_ids = [
        fid
        for fid in [fmap["epic_link"], fmap["epic_name"], fmap["sprint"]]
        + list(fmap["extra"].values())
        if fid
    ]

    if args.fields:
        try:
            return show_fields(
                client, build_jql(projects, team, window, extra_jql, None), fmap
            )
        except JiraError as e:
            print(f"\n{e}")
            return 1

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

    if args.prune:
        return prune(
            client,
            out_dir,
            build_jql(projects, team, window, extra_jql, None),
            dry_run=args.dry_run,
            force=args.force_prune,
        )

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

    # Названия эпиков помним между прогонами: иначе каждый прогон заново
    # спрашивал бы про одни и те же эпики. --full сбрасывает — эпик могли
    # переименовать
    epics_path = out_dir / ".epics.json"
    epic_cache: dict = {} if args.full else load_state(epics_path)

    def flush(batch: list[dict]) -> None:
        """Пачка задач -> файлы. Пачкой — чтобы названия эпиков спросить разом."""
        if fmap["epic_link"]:
            wanted = {
                " ".join(field_text((i.get("fields") or {}).get(fmap["epic_link"])))
                for i in batch
            }
            try:
                epic_titles(client, wanted, fmap, epic_cache)
            except JiraError as e:
                print(f"  [!] названия эпиков не получены ({e})")
        for issue in batch:
            save(issue)

    def save(issue: dict) -> None:
        key = issue.get("key", "")
        fields = issue.get("fields", {}) or {}
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

        record = normalize(issue, client.base, comments, fmap, epic_cache)
        # Эпик выгрузился сам — его название свежее запомненного
        if record["epic"] == key and record["epic_name"]:
            epic_cache[key] = record["epic_name"]
        target = out_dir / record["project"] / f"{key}.json"
        action = "обновлено" if target.exists() else "новых"
        stats[action] += 1

        if args.dry_run:
            extra = ""
            if record["epic"]:
                extra += f" эпик {record['epic']}"
            if record["sprints"]:
                extra += f" спринт {record['sprints'][-1]}"
            print(
                f"  [{action:9}] {key:14} {record['summary'][:60]} "
                f"(комментариев: {len(comments)}){extra}"
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    batch: list[dict] = []
    try:
        for issue in client.search(jql, with_comments, extra_fields=extra_ids):
            if args.limit and processed >= args.limit:
                break
            processed += 1

            if args.dump_raw and processed == 1:
                Path(args.dump_raw).write_text(client.last_raw, encoding="utf-8")
                print(f"\nСырой ответ сохранён: {args.dump_raw}")

            batch.append(issue)
            if len(batch) >= PAGE_SIZE:
                flush(batch)
                batch = []

            if processed % 100 == 0:
                print(f"    ...{processed} из {total}")
        flush(batch)
    except JiraError as e:
        print(f"\nВыгрузка прервана: {e}")
        return 1

    print("\nИтог:")
    for name, value in stats.items():
        print(f"    {name:12} {value}")

    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        epics_path.write_text(
            json.dumps(epic_cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
