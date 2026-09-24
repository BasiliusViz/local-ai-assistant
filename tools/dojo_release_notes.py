#!/usr/bin/env python3
"""Release notes по устранённым уязвимостям из DefectDojo — один файл.

Запускается где угодно, где есть Python 3.9+: только стандартная
библиотека, ставить ничего не нужно. Ни модель, ни наш сервер не нужны —
скрипт сам спрашивает DefectDojo по API и собирает Markdown.

    python dojo_release_notes.py --url https://dojo.company.local --product abinf --from release-1.1 --to release-1.2

Ключ — ваш API v2 Key из профиля DefectDojo. Берётся из переменной
DOJO_TOKEN, а если её нет — спрашивается со скрытым вводом. В командную
строку его не писать: останется в истории.

Устранённая уязвимость — открыта в --from (прошлый релиз) и не открыта в
--to (новый): её закрыли или сканер её больше не находит. Честно это, когда
в обоих engagement'ах прогнан один и тот же набор сканеров.

ТОЛЬКО ЧТЕНИЕ: скрипт делает исключительно GET-запросы, изменить или
удалить что-то в DefectDojo он не может.

Та же логика, что у команды на сервере (kb/release_notes.py +
kb/dojo_compare.py); тест kb.test_release_notes сверяет, что документы
совпадают, — правки нужно вносить в оба места.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

PAGE_SIZE = 100
SEVERITIES = ("Critical", "High", "Medium", "Low", "Info")
LEVEL_RU = {
    "Critical": "Критичные",
    "High": "Высокие",
    "Medium": "Средние",
    "Low": "Низкие",
    "Info": "Информационные",
}
SEVERITY_WORDS = {
    "критич": "Critical", "critical": "Critical",
    "высок": "High", "high": "High",
    "средн": "Medium", "medium": "Medium",
    "низк": "Low", "low": "Low",
    "инфо": "Info", "info": "Info",
}


class DojoError(RuntimeError):
    pass


def norm(text: str | None) -> str:
    return (text or "").strip().casefold().replace("ё", "е")


# --------------------------------------------------------------------- API


class Dojo:
    """Клиент DefectDojo API v2. Только GET — других методов в нём нет."""

    def __init__(self, url: str, token: str, ca_file: str | None = None, timeout: float = 90):
        self.base = url.rstrip("/") + "/api/v2"
        self.token = token
        self.timeout = timeout
        # Проверку сертификата не отключаем: по этому соединению идёт ключ.
        # Внутренний УЦ — его корневой сертификат передаётся явно
        self.context = ssl.create_default_context(cafile=ca_file)

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(
            f"{self.base}{path}?{query}",
            method="GET",
            headers={"Authorization": f"Token {self.token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DojoError("401: ключ не принят. Нужен API v2 Key из профиля DefectDojo.")
            if e.code == 403:
                raise DojoError("403: ключ принят, но прав на эти данные нет.")
            if e.code == 404:
                raise DojoError(f"404: пути {path} нет. Адрес нужен без /api/v2 на конце.")
            raise DojoError(f"HTTP {e.code} на {path}: {e.read()[:300].decode('utf-8', 'replace')}")
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "CERTIFICATE" in reason.upper():
                raise DojoError(
                    f"Ошибка сертификата: {reason}. Если у DefectDojo сертификат "
                    "внутреннего УЦ — укажите его корневой сертификат: --ca-file ca.crt "
                    "(выгрузить можно из браузера: замок в адресной строке -> "
                    "сертификат -> корневой -> экспорт в Base64 .crt)"
                )
            raise DojoError(f"Нет связи с DefectDojo ({self.base}): {reason}")
        except TimeoutError:
            raise DojoError(f"Таймаут {self.timeout:.0f} с на {path}. Поднимите --timeout.")

    def paged(self, path: str, **params) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            data = self.get(path, limit=PAGE_SIZE, offset=offset, **params)
            results = data.get("results", [])
            out += results
            offset += len(results)
            print(f"  {path} {offset}/{data.get('count', '?')}", file=sys.stderr, end="\r")
            if not results or offset >= int(data.get("count", 0)):
                print(file=sys.stderr)
                return out


def find_product(dojo: Dojo, name: str) -> dict:
    """Точное имя, потом вхождение. Сначала узким запросом по имени: список
    продуктов у DefectDojo тяжёлый, тянуть его целиком — крайний случай."""
    wanted = norm(name)
    exact = [p for p in dojo.get("/products/", name=name).get("results", [])
             if norm(p.get("name")) == wanted]
    if exact:
        return exact[0]
    everything = dojo.paged("/products/")
    exact = [p for p in everything if norm(p.get("name")) == wanted]
    if exact:
        return exact[0]
    partial = [p for p in everything if wanted in norm(p.get("name"))]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(p.get("name", "") for p in partial)
        raise DojoError(f"Под «{name}» подходит несколько продуктов: {names}.")
    names = ", ".join(p.get("name", "") for p in everything[:30])
    raise DojoError(f"Продукта «{name}» нет среди доступных. Есть: {names}.")


def find_engagement(name: str, known: list[dict]) -> dict:
    """Точное имя -> ветка до «_» целиком -> начало -> вхождение.
    «main» среди main_abinf и main-old_abinf выбирает main_abinf."""
    wanted = norm(name)
    for rule in (
        lambda n: n == wanted,
        lambda n: n.split("_")[0] == wanted,
        lambda n: n.startswith(wanted),
        lambda n: wanted in n,
    ):
        hits = [e for e in known if rule(norm(e.get("name")))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(e.get("name", "") for e in hits[:20])
            raise DojoError(f"Под «{name}» подходит несколько engagement'ов: {names}.")
    names = ", ".join(e.get("name", "") for e in known[:30])
    raise DojoError(f"Engagement'а «{name}» нет. Есть: {names or 'ни одного'}.")


# --------------------------------------------------------------- сравнение


def finding_status(item: dict) -> str:
    if item.get("false_p"):
        return "false_positive"
    if item.get("risk_accepted"):
        return "accepted"
    if item.get("is_mitigated") or item.get("mitigated"):
        return "fixed"
    if item.get("active"):
        return "open"
    return "inactive"


def state(item: dict) -> str:
    """Дубликат DefectDojo делает неактивным, но он значит «сканер нашёл это
    и здесь». Никем не закрытый дубликат — открыт, иначе общая для двух
    engagement'ов уязвимость выглядела бы устранённой."""
    status = finding_status(item)
    if item.get("duplicate") and status == "inactive":
        return "open"
    return status


def match_key(item: dict) -> str:
    """hash_code — отпечаток, которым DefectDojo сам ищет дубликаты. Без него
    — заголовок, CWE, файл, компонент, без строки: в новой версии код сдвигается."""
    if item.get("hash_code"):
        return "h:" + str(item["hash_code"])
    return "t:" + "|".join(
        norm(str(item.get(k) or "")) for k in ("title", "cwe", "file_path", "component_name")
    )


def open_findings(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items:
        if state(item) == "open":
            out.setdefault(match_key(item), item)
    return out


def card(item: dict, url: str) -> dict:
    where = item.get("file_path") or ""
    if where and item.get("line"):
        where = f"{where}:{item['line']}"
    out = {
        "id": item.get("id"),
        "title": (item.get("title") or "").strip(),
        "severity": item.get("severity", ""),
        "url": f"{url.rstrip('/')}/finding/{item.get('id')}",
    }
    if where:
        out["location"] = where
    if item.get("cwe"):
        out["cwe"] = f"CWE-{item['cwe']}"
    component = " ".join(
        x for x in (item.get("component_name") or "", item.get("component_version") or "") if x
    ).strip()
    if component:
        out["component"] = component
    return out


def group(items: list[dict], url: str) -> dict:
    order = {s: i for i, s in enumerate(SEVERITIES)}
    items = sorted(items, key=lambda i: (order.get(i.get("severity") or "", 9), i.get("id", 0)))
    counts = {s: 0 for s in SEVERITIES}
    for item in items:
        if item.get("severity") in counts:
            counts[item["severity"]] += 1
    return {"total": len(items), "summary": counts, "findings": [card(i, url) for i in items]}


# ---------------------------------------------------------------- документ


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def parse_levels(value: str | None) -> list[str]:
    out = []
    for part in re.split(r"[,;/]|\s+и\s+", value or ""):
        word = norm(part)
        if not word:
            continue
        level = next((v for k, v in SEVERITY_WORDS.items() if word.startswith(k)), None)
        if not level:
            raise DojoError(f"Уровень «{part.strip()}» непонятен: critical, high, medium, low, info.")
        out.append(level)
    return out


def only(g: dict, levels: list[str]) -> dict:
    if not levels:
        return g
    findings = [f for f in g["findings"] if f["severity"] in levels]
    return {
        "total": len(findings),
        "summary": {lvl: n for lvl, n in g["summary"].items() if lvl in levels},
        "findings": findings,
    }


def line(f: dict) -> str:
    details = [x for x in (f.get("cwe"), f.get("component")) if x]
    if f.get("location"):
        details.append(f"`{f['location']}`")
    tail = f" — {', '.join(details)}" if details else ""
    return f"- **{f['title']}**{tail} ([#{f['id']}]({f['url']}))"


def section(title: str, g: dict) -> list[str]:
    out = [f"## {title}", ""]
    if not g["total"]:
        return out + ["Нет.", ""]
    for level in SEVERITIES:
        items = [f for f in g["findings"] if f["severity"] == level]
        if items:
            out += [f"### {LEVEL_RU[level]} ({len(items)})", ""]
            out += [line(f) for f in items] + [""]
    return out


def summary_line(g: dict) -> str:
    parts = [f"{LEVEL_RU[lvl].lower()} — {n}" for lvl, n in g["summary"].items() if n]
    return ", ".join(parts) if parts else "нет"


def render(result: dict, levels: list[str], with_new: bool) -> str:
    fixed = only(result["only_in_first"], levels)
    new = only(result["only_in_second"], levels)
    remain = only(result["in_both"], levels)
    n = fixed["total"]
    out = [
        f"# Устранённые уязвимости: {result['product']}, {result['second']}",
        "",
        f"Сравнение **{result['first']}** → **{result['second']}**, "
        f"{date.today().isoformat()}. Источник — DefectDojo.",
    ]
    if levels:
        out.append(f"Учтены уровни: {', '.join(LEVEL_RU[lvl].lower() for lvl in levels)}.")
    out += [
        "",
        f"**Устранено: {n} {plural(n, 'уязвимость', 'уязвимости', 'уязвимостей')}**"
        + (f" ({summary_line(fixed)})" if n else "")
        + ".",
        "",
        "Устранённой считается уязвимость, открытая в прошлой версии и не "
        "найденная открытой в новой.",
        "",
    ]
    out += section("Устранено", fixed)
    if with_new:
        out += section(f"Новые в {result['second']}", new)
    out += [
        "## Остаются открытыми",
        "",
        f"{remain['total']} ({summary_line(remain)}) — открыты и в "
        f"{result['first']}, и в {result['second']}.",
        "",
    ]
    return "\n".join(out)


def build(dojo: Dojo, url: str, product: str, first: str, second: str) -> dict:
    prod = find_product(dojo, product)
    print(f"Продукт: {prod.get('name')}", file=sys.stderr)
    known = dojo.paged("/engagements/", product=prod["id"])
    a, b = find_engagement(first, known), find_engagement(second, known)
    if a.get("id") == b.get("id"):
        raise DojoError(f"Оба названия указывают на один engagement: {a.get('name')}.")
    print(f"Сравниваю: {a.get('name')} -> {b.get('name')}", file=sys.stderr)
    in_a = open_findings(dojo.paged("/findings/", test__engagement=a["id"], ordering="id"))
    in_b = open_findings(dojo.paged("/findings/", test__engagement=b["id"], ordering="id"))
    return {
        "product": prod.get("name", ""),
        "first": a.get("name"),
        "second": b.get("name"),
        "only_in_second": group([v for k, v in in_b.items() if k not in in_a], url),
        "only_in_first": group([v for k, v in in_a.items() if k not in in_b], url),
        "in_both": group([v for k, v in in_b.items() if k in in_a], url),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Release notes по устранённым уязвимостям из DefectDojo")
    ap.add_argument("--url", default=os.getenv("DOJO_URL", ""), help="адрес DefectDojo, без /api/v2")
    ap.add_argument("--product", required=True, help="продукт, можно неполно: abinf")
    ap.add_argument("--from", dest="first", required=True, help="прошлый релиз / engagement")
    ap.add_argument("--to", dest="second", required=True, help="новый релиз / engagement")
    ap.add_argument("--severity", default="", help="уровни через запятую: critical,high")
    ap.add_argument("--with-new", action="store_true", help="добавить раздел новых уязвимостей")
    ap.add_argument("--out", help="файл (по умолчанию release-notes-<продукт>-<релиз>-<дата>.md)")
    ap.add_argument("--ca-file", help="корневой сертификат внутреннего УЦ (.crt/.pem)")
    ap.add_argument("--timeout", type=float, default=90, help="секунд на запрос")
    args = ap.parse_args()

    if not args.url:
        print("Нужен адрес DefectDojo: --url https://dojo.company.local (или DOJO_URL)")
        return 2
    token = os.getenv("DOJO_TOKEN") or getpass.getpass("Ключ DefectDojo (API v2 Key, ввод скрыт): ")
    if not token.strip():
        print("Ключ пустой.")
        return 2

    try:
        levels = parse_levels(args.severity)
        dojo = Dojo(args.url, token.strip(), ca_file=args.ca_file, timeout=args.timeout)
        result = build(dojo, args.url, args.product, args.first, args.second)
    except DojoError as e:
        print(f"\n{e}")
        return 1

    text = render(result, levels, args.with_new)
    path = args.out or (
        "release-notes-"
        + re.sub(r"[^\w.-]+", "_", f"{result['product']}-{result['second']}")
        + f"-{date.today().isoformat()}.md"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    fixed = only(result["only_in_first"], levels)["total"]
    print(f"\nУстранено: {fixed}. Записано: {os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
