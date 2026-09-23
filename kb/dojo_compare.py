"""Engagement'ы DefectDojo: находки одного и сравнение двух — живым запросом.

В индекс engagement не попадает, и для сравнения он не годится по сути:
выгрузка берёт только оригиналы (duplicate=false), а уязвимость, найденная в
обеих ветках, во второй помечена дубликатом первой. По индексу сравнение
показало бы, что она во второй ветке «исчезла», хотя она на месте. Поэтому
здесь — прямые запросы к API, с дубликатами.

Engagement'ы у нас называются «ветка_продукт» (main_abinf, feature-x_abinf),
поэтому ищутся по началу имени: «main» находит «main_abinf».

Одинаковые находки сопоставляются по hash_code — его DefectDojo сам считает
для дедупликации. Нет hash_code (у части сканеров) — по заголовку, CWE, файлу
и компоненту. Строку не берём: в другой ветке код сдвинулся, и одна и та же
находка выглядела бы исчезнувшей и появившейся заново.
"""

from __future__ import annotations

import httpx

from kb import dojo

# Как спрашивают «все engagement'ы»
ALL = {"*", "все", "всё", "all", "list", "список"}


def _norm(text: str) -> str:
    return dojo._norm(text)


def engagements(client: httpx.Client, product_id: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        data = dojo._get(
            client,
            "/engagements/",
            product=product_id,
            limit=dojo.PAGE_SIZE,
            offset=offset,
        )
        results = data.get("results", [])
        out += results
        offset += len(results)
        if not results or offset >= int(data.get("count", 0)):
            return out


def resolve_engagement(name: str, known: list[dict]) -> dict:
    """Имя из вопроса -> engagement. Точное имя, потом ветка, потом вхождение.

    «main» среди main_abinf и main-old_abinf должен выбрать main_abinf:
    совпадение ветки целиком (до «_») важнее вхождения.
    """
    wanted = _norm(name)
    for rule in (
        lambda n: n == wanted,
        lambda n: n.split("_")[0] == wanted,
        lambda n: n.startswith(wanted),
        lambda n: wanted in n,
    ):
        hits = [e for e in known if rule(_norm(e.get("name") or ""))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(e.get("name", "") for e in hits[:20])
            raise dojo.DojoError(
                f"Под «{name}» подходит несколько engagement'ов: {names}. "
                "Уточни, какой нужен."
            )
    names = ", ".join(e.get("name", "") for e in known[:30])
    raise dojo.DojoError(f"Engagement'а «{name}» нет. Есть: {names or 'ни одного'}.")


def engagement_findings(client: httpx.Client, engagement_id: int) -> list[dict]:
    """Все находки engagement'а, ВКЛЮЧАЯ дубликаты — см. шапку модуля."""
    out: list[dict] = []
    offset = 0
    while True:
        data = dojo._get(
            client,
            "/findings/",
            test__engagement=engagement_id,
            limit=dojo.PAGE_SIZE,
            offset=offset,
            ordering="id",
        )
        results = data.get("results", [])
        out += results
        offset += len(results)
        if not results or offset >= int(data.get("count", 0)):
            return out


def match_key(item: dict) -> str:
    if item.get("hash_code"):
        return "h:" + str(item["hash_code"])
    return "t:" + "|".join(
        _norm(str(item.get(k) or ""))
        for k in ("title", "cwe", "file_path", "component_name")
    )


def _card(item: dict) -> dict:
    where = item.get("file_path") or ""
    if where and item.get("line"):
        where = f"{where}:{item['line']}"
    out = {
        "id": item.get("id"),
        "title": (item.get("title") or "").strip(),
        "severity": item.get("severity", ""),
        "status": state(item),
        "url": f"{dojo.URL}/finding/{item.get('id')}",
    }
    if where:
        out["location"] = where
    if item.get("cwe"):
        out["cwe"] = f"CWE-{item['cwe']}"
    component = " ".join(
        filter(None, [item.get("component_name") or "", item.get("component_version") or ""])
    ).strip()
    if component:
        out["component"] = component
    return out


SEVERITY_ORDER = {s: i for i, s in enumerate(dojo.SEVERITIES)}


def state(item: dict) -> str:
    """Состояние находки в ЭТОМ engagement'е.

    Дубликат DefectDojo делает неактивным, но он значит «сканер нашёл это и
    здесь». Без поправки уязвимость, общая для двух веток, во второй выглядела
    бы закрытой, и сравнение записало бы её в «исправлено». Никем не
    закрытый дубликат — открыт.
    """
    status = dojo.finding_status(item)
    if item.get("duplicate") and status == "inactive":
        return "open"
    return status


def _pick(items: list[dict], status: str | None, level: str | None) -> dict[str, dict]:
    """Отбор по состоянию и уровню, одна находка на ключ."""
    out: dict[str, dict] = {}
    for item in items:
        if status and state(item) != status:
            continue
        if level and item.get("severity") != level:
            continue
        # Одна и та же уязвимость в engagement'е несколько раз (два сканера,
        # повторный импорт) — берём одну: сравниваем уязвимости, а не строки
        out.setdefault(match_key(item), item)
    return out


def _group(items: list[dict], limit: int) -> dict:
    items = sorted(
        items, key=lambda i: (SEVERITY_ORDER.get(i.get("severity") or "", 9), i.get("id", 0))
    )
    counts = {s: 0 for s in dojo.SEVERITIES}
    for item in items:
        if item.get("severity") in counts:
            counts[item["severity"]] += 1
    return {
        "total": len(items),
        "summary": counts,
        "findings": [_card(i) for i in items[:limit]],
    }


def list_engagements(product: str) -> dict:
    with dojo._client() as client:
        prod = dojo.resolve_product(client, product)
        known = engagements(client, prod["id"])
    known.sort(key=lambda e: e.get("target_start") or "", reverse=True)
    return {
        "product": prod.get("name"),
        "engagements": [
            {
                "name": e.get("name"),
                "status": e.get("status"),
                "start": e.get("target_start"),
                "url": f"{dojo.URL}/engagement/{e.get('id')}",
            }
            for e in known
        ],
    }


def one(product: str, engagement: str, status: str | None, level: str | None, limit: int) -> dict:
    with dojo._client() as client:
        prod = dojo.resolve_product(client, product)
        eng = resolve_engagement(engagement, engagements(client, prod["id"]))
        items = engagement_findings(client, eng["id"])
    picked = _pick(items, status, level)
    return {
        "product": prod.get("name"),
        "engagement": eng.get("name"),
        "engagement_url": f"{dojo.URL}/engagement/{eng.get('id')}",
        **_group(list(picked.values()), limit),
    }


def compare(
    product: str,
    first: str,
    second: str,
    status: str | None,
    level: str | None,
    limit: int,
) -> dict:
    """Что появилось во втором, что ушло из первого, что осталось в обоих."""
    with dojo._client() as client:
        prod = dojo.resolve_product(client, product)
        known = engagements(client, prod["id"])
        a = resolve_engagement(first, known)
        b = resolve_engagement(second, known)
        if a.get("id") == b.get("id"):
            raise dojo.DojoError(
                f"Оба названия указывают на один engagement: {a.get('name')}."
            )
        in_a = _pick(engagement_findings(client, a["id"]), status, level)
        in_b = _pick(engagement_findings(client, b["id"]), status, level)

    name = prod.get("name", "")
    return {
        "product": name,
        "first": a.get("name"),
        "second": b.get("name"),
        "only_in_second": _group([v for k, v in in_b.items() if k not in in_a], limit),
        "only_in_first": _group([v for k, v in in_a.items() if k not in in_b], limit),
        # из второго: там актуальные место и ссылка
        "in_both": _group([v for k, v in in_b.items() if k in in_a], limit),
    }
