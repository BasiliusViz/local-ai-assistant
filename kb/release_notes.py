"""Release notes по устранённым уязвимостям: сравнение двух engagement'ов.

    docker compose exec kb python -m kb.release_notes --product abinf --from release-1.1 --to release-1.2
    docker compose exec kb python -m kb.release_notes --product abinf --from main --to release-1.2 --severity critical,high --with-new

Устранённая — открыта в --from (прошлый релиз, основная ветка) и не открыта
в --to (новый релиз): её закрыли, или сканер её больше не находит. Сравнение
то же, что у «dojo сравни engagement ...» в чате (kb/dojo_compare.py): живой
запрос к DefectDojo, с дубликатами, сопоставление по hash_code.

Модель не участвует: документ собирается из данных DefectDojo, поэтому
воспроизводим и ничего не выдумывает — для релизного документа по
безопасности это обязательно.

Файл ложится в /docs/reports/ — смонтированный каталог, виден на хосте.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from kb import dojo, dojo_compare

LEVEL_RU = {
    "Critical": "Критичные",
    "High": "Высокие",
    "Medium": "Средние",
    "Low": "Низкие",
    "Info": "Информационные",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """3 уязвимости, 5 уязвимостей: в документе для релиза это замечают."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def only(group: dict, levels: list[str]) -> dict:
    """Оставить в группе только нужные уровни и пересчитать сводку."""
    if not levels:
        return group
    findings = [f for f in group["findings"] if f["severity"] in levels]
    return {
        "total": len(findings),
        "summary": {lvl: n for lvl, n in group["summary"].items() if lvl in levels},
        "findings": findings,
    }


def line(f: dict) -> str:
    details = [x for x in (f.get("cwe"), f.get("component")) if x]
    if f.get("location"):
        details.append(f"`{f['location']}`")
    tail = f" — {', '.join(details)}" if details else ""
    return f"- **{f['title']}**{tail} ([#{f['id']}]({f['url']}))"


def section(title: str, group: dict, max_items: int | None = None) -> list[str]:
    """Раздел по уровням. max_items — потолок строк: в чате длинный список
    съел бы контекст модели, поэтому там показываем начало и пишем, сколько
    всего. Сортировка — худшие первыми, так что обрезается хвост из низких."""
    out = [f"## {title}", ""]
    if not group["total"]:
        return out + ["Нет.", ""]
    shown = 0
    for level in dojo.SEVERITIES:
        items = [f for f in group["findings"] if f["severity"] == level]
        if not items:
            continue
        out += [f"### {LEVEL_RU[level]} ({len(items)})", ""]
        if max_items is not None:
            items = items[: max(max_items - shown, 0)]
        out += [line(f) for f in items] + [""]
        shown += len(items)
    if max_items is not None and shown < group["total"]:
        out += [
            f"_Показано {shown} из {group['total']}. Полный список — командой "
            "`python -m kb.release_notes` на сервере._",
            "",
        ]
    return out


def summary_line(group: dict) -> str:
    parts = [f"{LEVEL_RU[lvl].lower()} — {n}" for lvl, n in group["summary"].items() if n]
    return ", ".join(parts) if parts else "нет"


def render(
    result: dict, levels: list[str], with_new: bool, max_items: int | None = None
) -> str:
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
    out += section("Устранено", fixed, max_items)
    if with_new:
        out += section(f"Новые в {result['second']}", new, max_items)
    out += [
        "## Остаются открытыми",
        "",
        f"{remain['total']} ({summary_line(remain)}) — открыты и в "
        f"{result['first']}, и в {result['second']}.",
        "",
    ]
    return "\n".join(out)


def parse_levels(value: str | None) -> list[str]:
    """«critical,high», «критичные и высокие» -> ["Critical", "High"]."""
    parts = re.split(r"[,;/]|\s+и\s+", value or "")
    return [dojo.normalize_severity(p) for p in parts if p.strip()]


def build(
    product: str,
    first: str,
    second: str,
    severity: str | None = None,
    with_new: bool = False,
    max_items: int | None = None,
) -> tuple[str, dict]:
    """Документ целиком: одинаковый для команды и для чата."""
    levels = parse_levels(severity)
    # Сравниваем всё открытое, уровни отбираем уже в документе: сводка
    # «остаются открытыми» иначе считалась бы не по тем же правилам
    result = dojo_compare.compare(product, first, second, "open", None, limit=100000)
    return render(result, levels, with_new, max_items), result


def main() -> int:
    ap = argparse.ArgumentParser(description="Release notes по устранённым уязвимостям")
    ap.add_argument("--product", required=True, help="продукт, можно неполно: abinf")
    ap.add_argument("--from", dest="first", required=True, help="прошлый релиз / ветка")
    ap.add_argument("--to", dest="second", required=True, help="новый релиз / ветка")
    ap.add_argument("--severity", default="", help="уровни через запятую: critical,high")
    ap.add_argument("--with-new", action="store_true", help="добавить раздел новых уязвимостей")
    ap.add_argument("--out", help="куда записать (по умолчанию /docs/reports/...)")
    args = ap.parse_args()

    if not dojo.configured():
        print("Не заданы DOJO_URL и DOJO_TOKEN.")
        return 2
    try:
        text, result = build(
            args.product, args.first, args.second, args.severity, args.with_new
        )
    except dojo.DojoError as e:
        print(e)
        return 1

    if args.out:
        path = Path(args.out)
    else:
        safe = f"{result['product']}-{result['second']}".replace("/", "-").replace(" ", "_")
        path = Path("/docs/reports") / f"release-notes-{safe}-{date.today().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nЗаписано: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
