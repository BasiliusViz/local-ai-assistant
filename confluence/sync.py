"""Выгрузка страниц Confluence в markdown-файлы.

Рассчитан на запуск в закрытом контуре без посторонней помощи, поэтому:
  - сначала проверяет связь и печатает, что видит (--check)
  - показывает, что будет сделано, ничего не записывая (--dry-run)
  - сохраняет сырой ответ API для разбора проблем (--dump-raw)
  - объясняет ошибки словами, а не трейсбеком
  - только стандартная библиотека, ставить ничего не нужно

Настройка (переменные окружения или .env рядом со скриптом):
    CONFLUENCE_URL     https://wiki.company.local
    CONFLUENCE_TOKEN   personal access token
    CONFLUENCE_SPACES  DEV,OPS         (пусто = все доступные)
    CONFLUENCE_OUT     ./confluence/pages

Использование:
    python confluence/sync.py --check       проверить связь и показать спейсы
    python confluence/sync.py --dry-run     показать, что будет выгружено
    python confluence/sync.py               выгрузить
    python confluence/sync.py --full        игнорировать даты, выгрузить всё

Дальше выгруженное индексируется обычным путём:
    .\\index-files.ps1 -SourceDir ".\\confluence\\pages" -Source "confluence"
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from storage_to_md import convert  # noqa: E402

HERE = Path(__file__).parent
STATE_FILE = HERE / ".sync_state.json"


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
    def __init__(self, base_url: str, token: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.last_raw: str = ""

    def get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )

        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.last_raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(self.last_raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 401:
                raise ConfluenceError(
                    "401: токен не принят.\n"
                    "  - Confluence Server/DC ждёт Personal Access Token в "
                    "заголовке Bearer (Profile -> Personal Access Tokens)\n"
                    "  - если у вас Confluence Cloud, там другая схема: "
                    "email + API-токен через Basic auth, этот скрипт её не умеет"
                ) from e
            if e.code == 403:
                raise ConfluenceError(
                    "403: доступ запрещён. Токен рабочий, но у пользователя нет "
                    "прав на этот спейс, либо администратор ограничил REST API"
                ) from e
            if e.code == 404:
                raise ConfluenceError(
                    f"404: путь {path} не найден.\n"
                    "  - проверьте URL: у Confluence часто есть префикс, "
                    "например https://host/confluence\n"
                    "  - в Cloud путь другой: /wiki/rest/api/..."
                ) from e
            raise ConfluenceError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ConfluenceError(
                f"Не удалось соединиться с {self.base}: {e.reason}\n"
                "  - проверьте, что адрес доступен с этой машины (ping, curl)\n"
                "  - если сеть за прокси, задайте HTTPS_PROXY"
            ) from e
        except json.JSONDecodeError as e:
            raise ConfluenceError(
                "Ответ не является JSON. Обычно это страница логина: "
                "значит запрос ушёл неаутентифицированным или URL ведёт не в API"
            ) from e

    def spaces(self) -> list[dict]:
        out, start = [], 0
        while True:
            data = self.get("/rest/api/space", start=start, limit=50)
            results = data.get("results", [])
            out.extend(results)
            if len(results) < 50:
                return out
            start += 50

    def pages(self, space: str | None, with_body: bool, page_size: int = 25):
        """Все страницы спейса, с постраничной выборкой."""
        start = 0
        while True:
            data = self.get(
                "/rest/api/content",
                spaceKey=space,
                type="page",
                status="current",
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


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
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
    spaces_env = os.getenv("CONFLUENCE_SPACES", "").strip()

    if not url or not token:
        print("Не заданы CONFLUENCE_URL и CONFLUENCE_TOKEN.")
        print(f"Создайте {HERE / '.env'} по образцу .env.example")
        return 2

    client = Client(url, token)

    # ---------- проверка связи ----------
    try:
        available = client.spaces()
    except ConfluenceError as e:
        print(f"Связь не установлена.\n{e}")
        return 1

    print(f"Соединение с {url}: OK")
    print(f"Доступно спейсов: {len(available)}")
    for s in available[:20]:
        print(f"    {s.get('key'):12} {s.get('name', '')}")
    if len(available) > 20:
        print(f"    ... и ещё {len(available) - 20}")

    wanted = [s.strip() for s in spaces_env.split(",") if s.strip()]
    if wanted:
        known = {s.get("key") for s in available}
        missing = [s for s in wanted if s not in known]
        if missing:
            print(f"\nСпейсы не найдены или недоступны: {', '.join(missing)}")
            return 1
        targets = wanted
    else:
        targets = [s.get("key") for s in available]

    print(f"\nБудут обработаны: {', '.join(targets)}")

    if args.check:
        # считаем страницы, тела не тянем - быстро
        for space in targets:
            try:
                count = sum(1 for _ in client.pages(space, with_body=False))
                print(f"    {space:12} страниц: {count}")
            except ConfluenceError as e:
                print(f"    {space:12} ошибка: {e}")
        return 0

    state = {} if args.full else load_state()
    new_state = dict(state)
    stats = {"новых": 0, "обновлено": 0, "без изменений": 0, "ошибок": 0}
    processed = 0

    for space in targets:
        for page in client.pages(space, with_body=True):
            if args.limit and processed >= args.limit:
                break
            processed += 1

            page_id = str(page.get("id"))
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
        STATE_FILE.write_text(
            json.dumps(
                {**new_state, "_synced_at": datetime.now().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nФайлы: {out_dir}")
        print(f"Состояние: {STATE_FILE.name} (для инкрементальной синхронизации)")
        print("\nДальше проиндексировать:")
        print(f'    .\\index-files.ps1 -SourceDir "{out_dir}" -Source "confluence"')

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано")
        sys.exit(130)
