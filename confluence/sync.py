"""Выгрузка страниц Confluence в markdown-файлы.

Что выгружать, задаётся так (способы можно совмещать):
  - CONFLUENCE_SPACES=DEV,OPS — пространства целиком, по ключу. Список всех
    пространств при этом НЕ запрашивается: право на него отдельное, его часто
    закрывают, и раньше это давало 403 на ровном месте. Ключ виден в адресе:
    .../display/DEV/... или .../spaces/DEV/...
  - CONFLUENCE_SPACES=* — все общие пространства. Вот здесь список нужен;
    закрыт — скрипт скажет и предложит перечислить ключи. Личные (~логин) не
    берутся: там черновики и чужие заметки
  - CONFLUENCE_PAGES — корневые страницы: сама страница и всё поддерево
Лишнее убирается CONFLUENCE_EXCLUDE: ключ пространства или номер страницы
(страница исключается вместе с поддеревом — удобно для «Архива»).

Рассчитан на запуск в закрытом контуре без посторонней помощи, поэтому:
  - сначала проверяет связь и печатает, что видит (--check)
  - показывает, что будет сделано, ничего не записывая (--dry-run)
  - сохраняет сырой ответ API для разбора проблем (--dump-raw)
  - объясняет ошибки словами, а не трейсбеком

Настройки берутся из .env в корне проекта — того же, где живёт весь остальной
стек. Отдельный confluence/.env тоже читается, если он есть, но обычно не нужен.

    CONFLUENCE_URL     https://wiki.company.local
    CONFLUENCE_TOKEN   personal access token
    CONFLUENCE_SPACES  DEV,OPS или *   пространства целиком
    CONFLUENCE_PAGES   123456,789012   идентификаторы корневых страниц
    CONFLUENCE_EXCLUDE ARCH,123456     что пропустить: ключи и номера страниц
    CONFLUENCE_OUT     куда складывать (по умолчанию ./confluence/pages)

Использование:
    python confluence/sync.py --check       проверить связь и посчитать страницы
    python confluence/sync.py --dry-run     показать, что будет выгружено
    python confluence/sync.py               выгрузить
    python confluence/sync.py --full        игнорировать даты, выгрузить всё

Дальше выгруженное индексируется:
    docker compose exec kb python -m kb.doc_index /docs/confluence --source confluence
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, str(Path(__file__).parent))
from storage_to_md import convert  # noqa: E402

HERE = Path(__file__).parent


def state_file(out_dir: Path) -> Path:
    """Файл состояния лежит РЯДОМ С ВЫГРУЗКОЙ, а не рядом со скриптом.

    Иначе в контейнере он остаётся внутри образа: теряется при пересборке, а
    если случайно попадёт в образ — синхронизация решит, что всё уже выгружено,
    и не запишет ни строчки.
    """
    return out_dir / ".sync_state.json"


def load_env() -> None:
    """Читает настройки из .env проекта.

    Ищет в двух местах, в таком порядке: рядом со скриптом (confluence/.env)
    и в корне проекта (../.env). Достаточно ОДНОГО файла — обычно корневого,
    там же лежат остальные настройки стека, и не приходится держать токены
    в двух местах.

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


class ConfluenceError(RuntimeError):
    """Ошибка с человеческим объяснением, а не с трейсбеком."""


class Client:
    """HTTP-клиент Confluence.

    Токен уходит заголовком `Authorization: Bearer <token>` — так его ждёт
    Confluence Server и Data Center. У Cloud схема другая (почта + токен через
    Basic auth), она здесь не реализована.
    """

    def __init__(self, base_url: str, token: str, timeout: int = 60, page_size: int = 25):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.page_size = page_size
        self.last_raw: str = ""

        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        # Повторы на сетевых сбоях и 5xx: при выгрузке тысячи страниц одна
        # случайная ошибка не должна ронять весь прогон. 4xx не повторяем -
        # неверный токен или путь от повтора не исправится
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def get(self, path: str, not_found: str | None = None, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}

        try:
            resp = self.session.get(
                f"{self.base}{path}", params=clean, timeout=self.timeout
            )
        except requests.exceptions.SSLError as e:
            raise ConfluenceError(
                f"Ошибка TLS при обращении к {self.base}: {e}\n"
                "  - во внутреннем контуре часто самоподписанный сертификат\n"
                "  - добавьте корневой сертификат компании в доверенные либо\n"
                "    укажите путь к нему в REQUESTS_CA_BUNDLE"
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise ConfluenceError(
                f"Не удалось соединиться с {self.base}: {e}\n"
                "  - проверьте, что адрес доступен с этой машины (ping, curl)\n"
                "  - если сеть за прокси, задайте HTTPS_PROXY"
            ) from e
        except requests.exceptions.Timeout as e:
            raise ConfluenceError(
                f"Таймаут {self.timeout} с при запросе {path}. "
                "Confluence отвечает медленно или недоступен"
            ) from e

        if resp.status_code == 401:
            raise ConfluenceError(
                "401: токен не принят.\n"
                "  - Confluence Server/DC ждёт Personal Access Token в "
                "заголовке Bearer (профиль -> Personal Access Tokens)\n"
                "  - если у вас Confluence Cloud, там другая схема: "
                "email + API-токен через Basic auth, этот скрипт её не умеет"
            )
        if resp.status_code == 403:
            raise ConfluenceError(
                f"403: доступ запрещён на {path}\n"
                "  Токен принят (иначе был бы 401), но прав на эту операцию нет.\n"
                "  - если путь /rest/api/space: просмотр списка пространств\n"
                "    часто закрыт администратором. Перечислите ключи явно:\n"
                "    CONFLUENCE_SPACES=DEV,OPS — список тогда не запрашивается\n"
                "  - если путь /rest/api/content/<id>: нет доступа к этой\n"
                "    странице или она в закрытом спейсе\n"
                "  - проверьте тем же токеном вручную:\n"
                f"    curl -H \"Authorization: Bearer <токен>\" {self.base}{path}"
            )
        if resp.status_code == 404 and not_found:
            raise ConfluenceError(not_found)
        if resp.status_code == 404:
            raise ConfluenceError(
                f"404: путь {path} не найден.\n"
                "  - проверьте URL: у Confluence часто есть префикс, "
                "например https://host/confluence\n"
                "  - в Cloud путь другой: /wiki/rest/api/..."
            )
        if not resp.ok:
            raise ConfluenceError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        self.last_raw = resp.text
        try:
            return resp.json()
        except ValueError as e:
            raise ConfluenceError(
                "Ответ не является JSON. Обычно это страница логина: "
                "значит запрос ушёл неаутентифицированным или URL ведёт не в API"
            ) from e

    @staticmethod
    def expand(with_body: bool) -> str:
        # ancestors — чтобы исключать разделы вроде «Архива» вместе с поддеревом
        base = "version,space,ancestors"
        return f"body.storage,{base}" if with_body else base

    def page(self, page_id: str, with_body: bool = True) -> dict:
        return self.get(f"/rest/api/content/{page_id}", expand=self.expand(with_body))

    def _paged(self, path: str, **params):
        start = 0
        while True:
            data = self.get(path, start=start, limit=self.page_size, **params)
            results = data.get("results", [])
            yield from results
            if len(results) < self.page_size:
                return
            start += self.page_size

    def descendants(self, page_id: str, with_body: bool):
        """Все страницы под указанной, на любой глубине.

        CQL `ancestor` даёт именно поддерево, а не только прямых потомков —
        то есть один идентификатор раздела забирает весь его материал.
        """
        yield from self._paged(
            "/rest/api/content/search",
            cql=f"ancestor={page_id} and type=page",
            expand=self.expand(with_body),
        )

    def space(self, key: str) -> dict:
        return self.get(
            f"/rest/api/space/{key}",
            not_found=(
                f"Пространства «{key}» нет или у токена нет к нему доступа — "
                "Confluence отвечает на оба случая одинаково.\n"
                "  - ключ виден в адресе страницы: .../display/КЛЮЧ/... или "
                ".../spaces/КЛЮЧ/...\n"
                "  - нужен ключ, а не название: «Разработка» -> DEV"
            ),
        )

    def spaces(self):
        """Все общие пространства. Личные (~логин) не берём: там черновики."""
        yield from self._paged("/rest/api/space", type="global", status="current")

    def space_pages(self, key: str, with_body: bool):
        """Все страницы пространства.

        Обычный список содержимого, а не CQL: CQL идёт через поисковый индекс
        Confluence, и если тот отстал или перестраивается, части страниц не
        будет. Список содержимого берётся из базы напрямую.
        """
        yield from self._paged(
            "/rest/api/content",
            spaceKey=key,
            type="page",
            status="current",
            expand=self.expand(with_body),
        )

def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Exclusions:
    """CONFLUENCE_EXCLUDE: числа — страницы (вместе с поддеревом), остальное —
    ключи пространств."""

    def __init__(self, value: str):
        items = split_list(value)
        self.pages = {i for i in items if i.isdigit()}
        self.spaces = {i for i in items if not i.isdigit()}

    def hit(self, page: dict) -> bool:
        if str(page.get("id")) in self.pages:
            return True
        if page.get("space", {}).get("key") in self.spaces:
            return True
        # Поддерево: страница исключена, если исключён любой её предок
        return any(str(a.get("id")) in self.pages for a in page.get("ancestors") or [])

    def __bool__(self) -> bool:
        return bool(self.pages or self.spaces)

    def __str__(self) -> str:
        parts = []
        if self.spaces:
            parts.append("пространства " + ", ".join(sorted(self.spaces)))
        if self.pages:
            parts.append("страницы с поддеревом " + ", ".join(sorted(self.pages)))
        return "; ".join(parts)


# Как часто печатать ход выгрузки
PROGRESS_EVERY = 50

# Сколько страниц просматривать в последнем, самом медленном способе поиска
# пространств. Хватает, чтобы увидеть все живые пространства, и не
# превращает проверку в полную выгрузку
SCAN_PAGES_LIMIT = 5000


def discover_spaces(client: Client, personal: bool) -> tuple[list[dict], str]:
    """Какие пространства видит токен. Три способа, от быстрого к медленному.

    Список пространств (/rest/api/space) часто закрыт администратором, а
    посмотреть, что можно вписать в CONFLUENCE_SPACES, нужно всё равно.
    Поэтому при 403 пробуем поиск по CQL, а если закрыт и он — обходим
    страницы и собираем пространства, в которых они лежат.
    """
    def keep(key: str) -> bool:
        return personal or not key.startswith("~")

    try:
        params = {} if personal else {"type": "global"}
        listed = list(client._paged("/rest/api/space", status="current", **params))
        return [s for s in listed if keep(s.get("key", ""))], "список пространств"
    except ConfluenceError as e:
        print(f"Список пространств закрыт ({str(e).splitlines()[0]}), пробую поиск...")

    try:
        found: dict[str, dict] = {}
        for item in client._paged("/rest/api/search", cql="type=space"):
            space = item.get("space") or {}
            key = space.get("key", "")
            if key and keep(key):
                found[key] = {"key": key, "name": space.get("name") or item.get("title", "")}
        return list(found.values()), "поиск CQL"
    except ConfluenceError as e:
        print(f"Поиск закрыт ({str(e).splitlines()[0]}), обхожу страницы...")

    found = {}
    pages = client._paged("/rest/api/content/search", cql="type=page", expand="space")
    for number, page in enumerate(pages, 1):
        space = page.get("space") or {}
        key = space.get("key", "")
        if key and keep(key):
            found.setdefault(key, {"key": key, "name": space.get("name", "")})
        if number >= SCAN_PAGES_LIMIT:
            return list(found.values()), (
                f"обход страниц — просмотрены первые {SCAN_PAGES_LIMIT}, "
                "список может быть неполным"
            )
    return list(found.values()), "обход страниц"


def list_spaces(client: Client, personal: bool, count: bool) -> int:
    """--list-spaces: что можно вписать в CONFLUENCE_SPACES."""
    try:
        spaces, how = discover_spaces(client, personal)
    except ConfluenceError as e:
        print(f"Не удалось узнать пространства ни одним способом.\n{e}")
        return 1

    spaces.sort(key=lambda s: s.get("key", "").casefold())
    print(f"\nДоступно пространств: {len(spaces)} (способ: {how})\n")
    if not spaces:
        print("Токен не видит ни одного пространства — проверьте его права.")
        return 1

    width = max(len("КЛЮЧ"), *(len(s.get("key", "")) for s in spaces))
    print(f"  {'КЛЮЧ':{width}}  {'СТРАНИЦ':>7}  НАЗВАНИЕ" if count else f"  {'КЛЮЧ':{width}}  НАЗВАНИЕ")
    for s in spaces:
        key = s.get("key", "")
        if count:
            try:
                pages = sum(1 for _ in client.space_pages(key, with_body=False))
                shown = str(pages)
            except ConfluenceError:
                shown = "нет доступа"
            print(f"  {key:{width}}  {shown:>7}  {s.get('name', '')}")
        else:
            print(f"  {key:{width}}  {s.get('name', '')}")

    print("\nСтрока для .env со всеми показанными — лишние удалить:")
    print("  CONFLUENCE_SPACES=" + ",".join(s.get("key", "") for s in spaces))
    if not count:
        print("\nСколько страниц в каждом: добавьте --count (дольше).")
    return 0


def safe_name(title: str) -> str:
    """Заголовок страницы -> имя файла."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title).strip(". ")
    name = re.sub(r"\s+", " ", name)
    return (name or "untitled")[:120]


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def main() -> int:
    load_env()

    ap = argparse.ArgumentParser(description="Выгрузка Confluence в markdown")
    ap.add_argument("--check", action="store_true", help="проверить связь и выйти")
    ap.add_argument(
        "--list-spaces",
        action="store_true",
        help="показать, какие пространства видит токен, и выйти",
    )
    ap.add_argument(
        "--count", action="store_true", help="с --list-spaces: посчитать страницы"
    )
    ap.add_argument(
        "--personal",
        action="store_true",
        help="с --list-spaces: показать и личные пространства (~логин)",
    )
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    ap.add_argument("--full", action="store_true", help="выгрузить всё, игнорируя даты")
    ap.add_argument("--dump-raw", metavar="FILE", help="сохранить сырой ответ API")
    ap.add_argument("--limit", type=int, help="ограничить число страниц (для пробы)")
    ap.add_argument(
        "--force-prune",
        action="store_true",
        help="удалить пропавшие страницы, даже если их подозрительно много",
    )
    args = ap.parse_args()

    url = os.getenv("CONFLUENCE_URL", "").strip()
    token = os.getenv("CONFLUENCE_TOKEN", "").strip()
    out_dir = Path(os.getenv("CONFLUENCE_OUT", str(HERE / "pages")))
    roots = split_list(os.getenv("CONFLUENCE_PAGES", ""))
    spaces_env = os.getenv("CONFLUENCE_SPACES", "").strip()
    excluded = Exclusions(os.getenv("CONFLUENCE_EXCLUDE", ""))

    if not url or not token:
        print("Не заданы CONFLUENCE_URL и CONFLUENCE_TOKEN.")
        print(f"Пропишите их в {HERE.parent / '.env'} — там же, где остальные")
        print("настройки стека. После правки: docker compose up -d kb")
        return 2

    if args.list_spaces:
        page_size = int(os.getenv("CONFLUENCE_PAGE_SIZE", "50"))
        return list_spaces(
            Client(url, token, page_size=page_size), args.personal, args.count
        )

    if not roots and not spaces_env:
        print("Не задано, что выгружать. Посмотреть доступные пространства:")
        print("  python confluence/sync.py --list-spaces")
        print("Потом в .env одно из двух (или оба):")
        print("  CONFLUENCE_SPACES=DEV,OPS  — пространства целиком, по ключу;")
        print("                               ключ виден в адресе: .../display/DEV/...")
        print("  CONFLUENCE_SPACES=*        — все общие пространства")
        print("  CONFLUENCE_PAGES=123456    — страница и всё под ней;")
        print("                               номер виден в адресе: ...?pageId=123456")
        return 2

    client = Client(
        url, token, page_size=int(os.getenv("CONFLUENCE_PAGE_SIZE", "50"))
    )

    # --- пространства
    space_keys: list[str] = []
    if spaces_env == "*":
        try:
            found = list(client.spaces())
        except ConfluenceError as e:
            print("Список пространств получить не удалось — режим «все» недоступен.")
            print("Перечислите нужные ключи явно: CONFLUENCE_SPACES=DEV,OPS\n")
            print(e)
            return 1
        print(f"Пространства (все общие, найдено {len(found)}):")
        for info in found:
            key = info.get("key", "")
            mark = "  пропускаю (CONFLUENCE_EXCLUDE)" if key in excluded.spaces else ""
            print(f"    {key:12} «{info.get('name', '')}»{mark}")
            if not mark:
                space_keys.append(key)
    elif spaces_env:
        print("Пространства:")
        for key in split_list(spaces_env):
            if key in excluded.spaces:
                print(f"    {key:12} пропускаю (CONFLUENCE_EXCLUDE)")
                continue
            try:
                info = client.space(key)
            except ConfluenceError as e:
                print(f"    {key:12} недоступно.\n{e}")
                return 1
            print(f"    {key:12} «{info.get('name', '')}»")
            space_keys.append(key)

    # --- корневые страницы
    if roots:
        print("Страницы:")
    for page_id in roots:
        try:
            info = client.page(page_id, with_body=False)
            print(f"    {page_id:12} «{info.get('title')}» "
                  f"(пространство {info.get('space', {}).get('key')})")
        except ConfluenceError as e:
            print(f"    {page_id:12} недоступна.\n{e}")
            return 1

    if excluded:
        print(f"Исключено: {excluded}")

    if args.check:
        print("\nСтраниц к выгрузке (без исключённых):")
        try:
            for key in space_keys:
                pages = client.space_pages(key, with_body=False)
                print(f"    {key:12} {sum(1 for p in pages if not excluded.hit(p))}")
            for page_id in roots:
                pages = client.descendants(page_id, with_body=False)
                # сама корневая страница тоже выгружается
                print(f"    {page_id:12} {sum(1 for p in pages if not excluded.hit(p)) + 1}")
        except ConfluenceError as e:
            print(f"    ошибка: {e}")
            return 1
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_file(out_dir)
    state = {} if args.full else load_state(state_path)
    new_state = dict(state)
    stats = {"новых": 0, "обновлено": 0, "без изменений": 0, "удалено": 0, "ошибок": 0}
    processed = 0

    def source_pages():
        """Пространства целиком, потом корневые страницы и всё под ними."""
        for key in space_keys:
            print(f"\n== пространство {key}", flush=True)
            yield from client.space_pages(key, with_body=True)
        for root_id in roots:
            print(f"\n== страница {root_id} и всё под ней", flush=True)
            # Корневая страница тоже нужна: в выдаче CQL ancestor её нет
            yield client.page(root_id, with_body=True)
            yield from client.descendants(root_id, with_body=True)

    seen_ids: set[str] = set()

    for page in source_pages():
        if args.limit and processed >= args.limit:
            break

        page_id = str(page.get("id"))
        # Исключённое не попадает в seen_ids: выгруженное раньше считается
        # пропавшим, и его файл уберётся — так «Архив» уходит из поиска
        if excluded.hit(page):
            continue
        # Поддеревья могут пересекаться: вложенные разделы, страница из
        # CONFLUENCE_PAGES внутри пространства из CONFLUENCE_SPACES
        if page_id in seen_ids:
            continue
        seen_ids.add(page_id)
        # Ход выполнения: без него тысяча страниц — это минуты тишины, и
        # прогон выглядит зависшим. Печатаем ДО учёта текущей страницы — тогда
        # числа в строке относятся ровно к уже обработанным
        if processed and processed % PROGRESS_EVERY == 0:
            print(
                f"  обработано {processed}: новых {stats['новых']}, "
                f"обновлено {stats['обновлено']}, "
                f"без изменений {stats['без изменений']}",
                flush=True,
            )
        processed += 1

        space = page.get("space", {}).get("key", "unknown")
        title = page.get("title", "")
        version = page.get("version", {})
        updated = version.get("when", "")

        if args.dump_raw and processed == 1:
            Path(args.dump_raw).write_text(client.last_raw, encoding="utf-8")
            print(f"\nСырой ответ сохранён: {args.dump_raw}")

        target = out_dir / space / f"{safe_name(title)}.md"
        previous = state.get(page_id)
        # Версия не изменилась — ещё не повод пропустить. Переименование и
        # перенос в другое пространство версию не меняют, а файл должен
        # переехать; и файл могли удалить с диска руками
        if (
            previous
            and previous.get("updated") == updated
            and previous.get("file") == str(target)
            and target.is_file()
        ):
            stats["без изменений"] += 1
            continue

        storage = (page.get("body", {}).get("storage", {}) or {}).get("value", "")
        try:
            md = convert(storage, title)
        except Exception as e:  # конвертер не должен ронять выгрузку
            print(f"  [!] {space}/{title}: ошибка конвертации ({e})")
            stats["ошибок"] += 1
            continue

        action = "обновлено" if previous else "новых"
        stats[action] += 1
        old_file = Path(previous["file"]) if previous and previous.get("file") else None

        if args.dry_run:
            print(f"  [{action:9}] {target.relative_to(out_dir.parent)} "
                  f"({len(md)} символов)")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8")
            # Переименовали или перенесли: старый файл убираем, иначе в поиске
            # висели бы обе версии, и старая — навсегда
            if old_file and old_file != target and old_file.is_file():
                old_file.unlink()

        new_state[page_id] = {
            "updated": updated,
            "title": title,
            "space": space,
            "file": str(target),
        }

    # Страницы, которые были в прошлый раз, а теперь в дереве не нашлись, —
    # их удалили или перенесли за пределы выгружаемых разделов. Файл убираем:
    # индексатор увидит, что он пропал, и вычистит его чанки из поиска. Без
    # этого удалённая в Confluence страница оставалась бы в ответах ассистента
    # навсегда.
    #
    # Только после полного обхода: при --limit дерево пройдено не целиком, и
    # всё непройденное выглядело бы удалённым. Прервётся обход на ошибке —
    # до этого места выполнение не дойдёт вовсе
    if not args.limit:
        known = [pid for pid in state if not pid.startswith("_")]
        vanished = [pid for pid in known if pid not in seen_ids]

        # Защита от смены прав. Если у токена отобрали доступ к разделу, его
        # страницы разом пропадут из выдачи — и это не удаление, а повод
        # разобраться. Молча стереть половину базы знаний из-за одной
        # настройки прав хуже, чем оставить устаревшие страницы на час
        suspicious = known and len(vanished) > max(10, len(known) // 2)
        if suspicious and not args.force_prune:
            print(
                f"\n[!] Из дерева пропало {len(vanished)} страниц из {len(known)} — "
                "это похоже на смену прав токена, а не на удаление.\n"
                "    Файлы не трогаю. Проверьте доступ к разделам; если страницы "
                "действительно удалены, запустите с --force-prune"
            )
        else:
            for pid in vanished:
                info = state.get(pid) or {}
                path = Path(info.get("file", ""))
                stats["удалено"] += 1
                if args.dry_run:
                    print(f"  [удалено   ] {info.get('title', pid)}")
                    continue
                if path.is_file():
                    path.unlink()
                new_state.pop(pid, None)

    print("\nИтог:")
    for key, value in stats.items():
        print(f"    {key:14} {value}")

    if not args.dry_run:
        state_path.write_text(
            json.dumps(
                {**new_state, "_synced_at": datetime.now().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nФайлы: {out_dir}")
        print(f"Состояние: {state_path} (для инкрементальной синхронизации)")
        print("\nДальше проиндексировать:")
        print(f'    docker compose exec kb python -m kb.doc_index "{out_dir}" --source confluence')

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано")
        sys.exit(130)
