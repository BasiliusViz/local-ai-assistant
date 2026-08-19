"""Выгрузка страниц Confluence в markdown-файлы.

Работает по идентификаторам КОРНЕВЫХ страниц: забирается сама страница и всё
поддерево под ней, на любой глубине. Выгрузки спейсов целиком нет намеренно —
в спейсе обычно много лишнего (черновики, архив), а право на просмотр списка
спейсов отдельное, и администраторы его часто закрывают: получался 403 на
ровном месте.

Рассчитан на запуск в закрытом контуре без посторонней помощи, поэтому:
  - сначала проверяет связь и печатает, что видит (--check)
  - показывает, что будет сделано, ничего не записывая (--dry-run)
  - сохраняет сырой ответ API для разбора проблем (--dump-raw)
  - объясняет ошибки словами, а не трейсбеком

Настройка (переменные окружения или .env рядом со скриптом):
    CONFLUENCE_URL     https://wiki.company.local
    CONFLUENCE_TOKEN   personal access token
    CONFLUENCE_PAGES   123456,789012   идентификаторы корневых страниц
    CONFLUENCE_OUT     ./confluence/pages

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
    """Читает .env рядом со скриптом, не перетирая уже заданные переменные."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
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

    def __init__(self, base_url: str, token: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
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

    def get(self, path: str, **params) -> dict:
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
                "  - если путь /rest/api/space: просмотр списка спейсов часто\n"
                "    закрыт администратором. Укажите CONFLUENCE_PAGES с id\n"
                "    страницы — список спейсов тогда не запрашивается\n"
                "  - если путь /rest/api/content/<id>: нет доступа к этой\n"
                "    странице или она в закрытом спейсе\n"
                "  - проверьте тем же токеном вручную:\n"
                f"    curl -H \"Authorization: Bearer <токен>\" {self.base}{path}"
            )
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

    def page(self, page_id: str, with_body: bool = True) -> dict:
        return self.get(
            f"/rest/api/content/{page_id}",
            expand="body.storage,version,space" if with_body else "version,space",
        )

    def descendants(self, page_id: str, with_body: bool, page_size: int = 25):
        """Все страницы под указанной, на любой глубине.

        CQL `ancestor` даёт именно поддерево, а не только прямых потомков —
        то есть один идентификатор раздела забирает весь его материал.
        """
        start = 0
        while True:
            data = self.get(
                "/rest/api/content/search",
                cql=f"ancestor={page_id} and type=page",
                expand="body.storage,version,space" if with_body else "version,space",
                start=start,
                limit=page_size,
            )
            results = data.get("results", [])
            for item in results:
                yield item
            if len(results) < page_size:
                return
            start += page_size

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
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не писать")
    ap.add_argument("--full", action="store_true", help="выгрузить всё, игнорируя даты")
    ap.add_argument("--dump-raw", metavar="FILE", help="сохранить сырой ответ API")
    ap.add_argument("--limit", type=int, help="ограничить число страниц (для пробы)")
    args = ap.parse_args()

    url = os.getenv("CONFLUENCE_URL", "").strip()
    token = os.getenv("CONFLUENCE_TOKEN", "").strip()
    out_dir = Path(os.getenv("CONFLUENCE_OUT", str(HERE / "pages")))
    pages_env = os.getenv("CONFLUENCE_PAGES", "").strip()

    if not url or not token:
        print("Не заданы CONFLUENCE_URL и CONFLUENCE_TOKEN.")
        print("  - в развёрнутой системе: пропишите их в .env проекта, они")
        print("    передаются в контейнер (docker compose up -d kb после правки)")
        print(f"  - при запуске вручную: создайте {HERE / '.env'} по образцу")
        return 2

    client = Client(url, token)

    roots = [p.strip() for p in pages_env.split(",") if p.strip()]
    if not roots:
        print("Не задан CONFLUENCE_PAGES — идентификаторы корневых страниц.")
        print("Идентификатор виден в адресе страницы: ...?pageId=123456")
        print("Забирается сама страница и всё поддерево под ней.")
        return 2

    print(f"Страницы: {', '.join(roots)}")
    for page_id in roots:
        try:
            info = client.page(page_id, with_body=False)
            print(f"    {page_id:10} «{info.get('title')}» "
                  f"(спейс {info.get('space', {}).get('key')})")
        except ConfluenceError as e:
            print(f"    {page_id:10} недоступна.\n{e}")
            return 1

    if args.check:
        for page_id in roots:
            try:
                count = sum(1 for _ in client.descendants(page_id, with_body=False))
                # сама корневая страница тоже выгружается
                print(f"    {page_id:10} страниц в поддереве: {count + 1}")
            except ConfluenceError as e:
                print(f"    {page_id:10} ошибка: {e}")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_file(out_dir)
    state = {} if args.full else load_state(state_path)
    new_state = dict(state)
    stats = {"новых": 0, "обновлено": 0, "без изменений": 0, "ошибок": 0}
    processed = 0

    def source_pages():
        """Корневые страницы и всё, что под ними."""
        for root_id in roots:
            # Корневая страница тоже нужна: в выдаче CQL ancestor её нет
            yield client.page(root_id, with_body=True)
            yield from client.descendants(root_id, with_body=True)

    seen_ids: set[str] = set()

    for page in source_pages():
        if args.limit and processed >= args.limit:
            break

        page_id = str(page.get("id"))
        # Поддеревья могут пересекаться, если указать вложенные разделы
        if page_id in seen_ids:
            continue
        seen_ids.add(page_id)
        processed += 1

        space = page.get("space", {}).get("key", "unknown")
        title = page.get("title", "")
        version = page.get("version", {})
        updated = version.get("when", "")

        if args.dump_raw and processed == 1:
            Path(args.dump_raw).write_text(client.last_raw, encoding="utf-8")
            print(f"\nСырой ответ сохранён: {args.dump_raw}")

        previous = state.get(page_id)
        if previous and previous.get("updated") == updated:
            stats["без изменений"] += 1
            continue

        storage = (page.get("body", {}).get("storage", {}) or {}).get("value", "")
        try:
            md = convert(storage, title)
        except Exception as e:  # конвертер не должен ронять выгрузку
            print(f"  [!] {space}/{title}: ошибка конвертации ({e})")
            stats["ошибок"] += 1
            continue

        target = out_dir / space / f"{safe_name(title)}.md"
        action = "обновлено" if previous else "новых"
        stats[action] += 1

        if args.dry_run:
            print(f"  [{action:9}] {target.relative_to(out_dir.parent)} "
                  f"({len(md)} символов)")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8")

        new_state[page_id] = {
            "updated": updated,
            "title": title,
            "space": space,
            "file": str(target),
        }

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
