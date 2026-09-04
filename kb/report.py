"""Отчёт по уязвимостям продукта: один HTML-файл, который не стыдно показать.

Берёт находки из индекса и рендерит документ по структуре, принятой в
отрасли: сводка для руководства, затем детали по убыванию серьёзности, в
конце — что именно вошло в выборку. Модель не участвует: документ собирается
из данных DefectDojo, поэтому воспроизводим и не выдумывает.

Резюме для руководства собирается шаблоном по счётчикам, а не моделью:
это то место, где выдумка обходится дороже всего.

Файл самодостаточный: стили внутри, шрифты системные, картинок нет. В
закрытом контуре это принципиально — ничего не подгружается снаружи.
Печать в PDF: открыть в браузере и Ctrl+P, вёрстка для печати предусмотрена.

Два способа запуска.

На сервере, в контейнере — файл ложится в смонтированный каталог:

    docker compose exec kb python -m kb.report --product abinf
    docker compose exec kb python -m kb.report --product abinf --severity критичные

С рабочей машины — находки берутся с MCP-сервера, файл пишется рядом:

    python -m kb.report --product abinf --server http://СЕРВЕР:8012

Второй способ ничего не требует, кроме доступа к порту: ни базы, ни
DefectDojo, ни докера. Рендер работает от списка находок, а откуда он
взялся — ему безразлично.
"""

import argparse
import html
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from kb import config, dojo, dojo_retriever

log = logging.getLogger(__name__)

# Отраслевая шкала: критичный тёмно-красный, высокий оранжевый, средний
# янтарный, низкий СИНИЙ (не зелёный — зелёный неотличим от красного при
# самой частой форме дальтонизма), информационный серый.
# Цвет никогда не единственный признак: рядом всегда текст уровня
SEVERITY_COLORS = {
    "Critical": "#A32D2D",
    "High": "#D85A30",
    "Medium": "#BA7517",
    "Low": "#378ADD",
    "Info": "#888780",
}

SEVERITY_RU = {
    "Critical": "Критичная",
    "High": "Высокая",
    "Medium": "Средняя",
    "Low": "Низкая",
    "Info": "Информационная",
}

STATUS_RU = {
    "open": "открыта",
    "accepted": "риск принят",
    "false_positive": "ложное срабатывание",
    "fixed": "исправлена",
    "inactive": "неактивна",
}

STYLE = """
:root {
  --ink: #1a1c1e;
  --muted: #5f6469;
  --line: #dfe3e6;
  --bg: #ffffff;
  --panel: #f6f8f9;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.6 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
.wrap { max-width: 900px; margin: 0 auto; padding: 48px 32px 80px; }
h1 { font-size: 28px; line-height: 1.2; margin: 0 0 8px; }
h2 { font-size: 19px; margin: 48px 0 16px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }
h3 { font-size: 16px; margin: 0; }
p { margin: 0 0 12px; }
.meta { color: var(--muted); font-size: 14px; margin-bottom: 32px; }
.meta b { color: var(--ink); font-weight: 600; }

/* сводка */
.totals { display: flex; gap: 1px; background: var(--line); border: 1px solid var(--line); margin-bottom: 24px; }
.total { flex: 1; background: var(--bg); padding: 14px 16px; }
.total .n { font-size: 26px; font-weight: 600; line-height: 1; font-variant-numeric: tabular-nums; }
.total .l { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-top: 6px; }
.bar { display: flex; height: 10px; margin-bottom: 28px; background: var(--panel); }
.bar span { display: block; }

/* находки */
.finding { border: 1px solid var(--line); border-left-width: 4px; margin-bottom: 16px; page-break-inside: avoid; }
.finding-head { display: flex; align-items: baseline; gap: 12px; padding: 14px 18px; background: var(--panel); flex-wrap: wrap; }
.badge { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: #fff; padding: 3px 8px; white-space: nowrap; }
.status { font-size: 13px; color: var(--muted); }
.finding-body { padding: 16px 18px; }
.facts { display: grid; grid-template-columns: 140px 1fr; gap: 6px 16px; font-size: 14px; margin-bottom: 16px; }
.facts dt { color: var(--muted); }
.facts dd { margin: 0; }
code, .mono { font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; font-size: 13px; }
.block { margin-bottom: 14px; }
.block h4 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 6px; }
.block div { white-space: pre-wrap; }
.fix { border-left: 3px solid #2f7a52; padding-left: 12px; }

/* Раскрывающиеся разделы: карточка остаётся обозримой, детали по клику */
details { border-top: 1px solid var(--line); margin-top: 12px; padding-top: 10px; }
details summary {
  cursor: pointer; font-size: 13px; text-transform: uppercase;
  letter-spacing: .06em; color: var(--muted); list-style: none;
}
details summary::-webkit-details-marker { display: none; }
details summary:before { content: "B8  "; }
details[open] summary:before { content: "BE  "; }
details .body { padding: 10px 0 4px; white-space: pre-wrap; }
.advice { background: #fbfaf5; border-left: 3px solid #b08a2e; padding: 12px 14px; margin-top: 10px; }
.advice .tag {
  display: inline-block; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
  color: #7a5f14; background: #f2e6c4; padding: 2px 7px; margin-bottom: 8px;
}
.advice .body { white-space: pre-wrap; }
a { color: #1f5fa8; }
footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }

@media print {
  /* В печать всё уходит раскрытым: свёрнутый раздел на бумаге — потерянный
     раздел, а документ печатают, чтобы приложить к задаче или отдать */
  details > *:not(summary) { display: block !important; }
  details summary:before { content: ""; }
  .wrap { padding: 0; max-width: none; }
  body { font-size: 11pt; }
  h2 { page-break-after: avoid; }
  a { color: var(--ink); text-decoration: none; }
  a[href^="http"]:after { content: " (" attr(href) ")"; font-size: 9pt; color: var(--muted); }
}
"""


def esc(text: str) -> str:
    return html.escape(text or "")


def block(title: str, text: str, extra: str = "") -> str:
    """Раздел находки. Пустые не рисуем: пустая графа хуже отсутствующей."""
    if not (text or "").strip():
        return ""
    return (
        f'<div class="block {extra}"><h4>{esc(title)}</h4>'
        f"<div>{esc(text.strip())}</div></div>"
    )


def foldable(title: str, text: str) -> str:
    """Длинный раздел под раскрытие.

    Описания от сканеров бывают на пол-экрана, и в списке из сорока находок
    они превращают документ в полотно. Свернём: обзор остаётся читаемым, а
    подробности — по клику. На печати всё раскрывается принудительно.
    """
    if not (text or "").strip():
        return ""
    return (
        f"<details><summary>{esc(title)}</summary>"
        f'<div class="body">{esc(text.strip())}</div></details>'
    )


def advice_block(text: str) -> str:
    """Предложение модели. Всегда с пометкой, что это не данные сканера.

    В документе по безопасности смешивать найденное с придуманным нельзя:
    читатель должен видеть, за что отвечает инструмент, а что нужно
    перепроверить перед тем, как выносить в задачу.
    """
    if not (text or "").strip():
        return ""
    return (
        '<details class="advice-wrap" open><summary>Предложение по исправлению</summary>'
        '<div class="advice"><span class="tag">Составлено ассистентом, требует проверки</span>'
        f'<div class="body">{esc(text.strip())}</div></div></details>'
    )


def render(
    product: str,
    summary: dict,
    hits: list,
    note: str,
    filters: dict,
    advice: dict | None = None,
) -> str:
    advice = advice or {}
    total = sum(summary.values())
    when = datetime.now().strftime("%d.%m.%Y")

    totals = "".join(
        f'<div class="total"><div class="n" style="color:{SEVERITY_COLORS[s]}">{summary.get(s, 0)}</div>'
        f'<div class="l">{esc(SEVERITY_RU[s])}</div></div>'
        for s in dojo.SEVERITIES
    )

    # Полоса распределения: доля каждого уровня. Нужна, чтобы соотношение
    # читалось до чтения цифр
    bar = ""
    if total:
        for s in dojo.SEVERITIES:
            share = summary.get(s, 0) / total * 100
            if share:
                bar += f'<span style="width:{share:.2f}%;background:{SEVERITY_COLORS[s]}"></span>'

    cards = []
    for hit in hits:
        colour = SEVERITY_COLORS.get(hit.severity, "#888780")
        facts = []
        if hit.cwe:
            facts.append(("Класс", esc(hit.cwe)))
        if hit.component:
            facts.append(("Компонент", esc(hit.component)))
        if hit.location:
            facts.append(("Где", f'<span class="mono">{esc(hit.location)}</span>'))
        if hit.scanner:
            facts.append(("Обнаружено", esc(hit.scanner)))
        if hit.found_at:
            facts.append(("Дата находки", esc(hit.found_at[:10])))

        facts_html = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)

        cards.append(
            f'<div class="finding" style="border-left-color:{colour}">'
            f'<div class="finding-head">'
            f'<span class="badge" style="background:{colour}">{esc(SEVERITY_RU.get(hit.severity, hit.severity))}</span>'
            f"<h3>{esc(hit.title)}</h3>"
            f'<span class="status">№{esc(hit.finding_id)} · {esc(STATUS_RU.get(hit.status, hit.status))}</span>'
            f"</div>"
            f'<div class="finding-body">'
            + (f'<dl class="facts">{facts_html}</dl>' if facts_html else "")
            + block("Чем грозит", hit.impact)
            + block("Как исправить (по данным сканера)", hit.mitigation, extra="fix")
            + advice_block(advice.get(hit.finding_id, ""))
            + foldable("Подробное описание находки", hit.description)
            + (f'<p><a href="{esc(hit.url)}">Открыть в DefectDojo</a></p>' if hit.url else "")
            + "</div></div>"
        )

    scope = ", ".join(f"{k}: {v}" for k, v in filters.items() if v)

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Уязвимости: {esc(product)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>Отчёт по уязвимостям: {esc(product)}</h1>
  <p class="meta">Составлен <b>{when}</b> по данным DefectDojo. Выборка: {esc(scope) or 'без ограничений'}.</p>

  <h2>Сводка</h2>
  <div class="totals">{totals}</div>
  <div class="bar">{bar}</div>
  <p>{esc(note)}</p>

  <h2>Находки</h2>
  {''.join(cards) if cards else '<p>Под условия отбора ничего не попало.</p>'}

  <footer>
    Всего в выборке {plural(total, 'находка', 'находки', 'находок')}, показано {len(hits)}.
    Данные из индекса, а не из живой системы: перед решениями сверьтесь с DefectDojo.
  </footer>
</div>
</body>
</html>
"""


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числа: 1 находка, 2 находки, 5 находок.

    Мелочь, но в документе для руководства «3 находок» бросается в глаза
    первым — и обесценивает всё остальное.
    """
    if n % 10 == 1 and n % 100 != 11:
        form = one
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        form = few
    else:
        form = many
    return f"{n} {form}"



ADVICE_PROMPT = """Ты инженер по безопасности приложений. По найденной уязвимости
напиши, что конкретно сделать разработчику.

Находка: {title}
Уровень: {severity}
Класс: {cwe}
Компонент: {component}
Место: {location}
Описание: {description}
Рекомендация сканера: {mitigation}

Требования к ответу:
- ПИШИ ТОЛЬКО ПО-РУССКИ, целиком, включая термины;
- три-пять предложений, по делу, без вступлений;
- начни с самого действия, а не с пересказа проблемы;
- если данных не хватает на конкретику, так и скажи и назови, что уточнить;
- ничего не выдумывай про наш код: ты видишь только то, что выше.

Ответ по-русски:"""


def chat_endpoint(base: str | None) -> str:
    """Куда слать запрос к модели.

    Без ключа берём адрес из настроек. С ключом — собираем от переданного:
    в .env сервера адрес модели часто записан именем, которое резолвится
    только внутри докера (host.docker.internal), и с рабочей машины по нему
    не достучаться.
    """
    if not base:
        return config.chat_url()
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return f"{base}/api/chat" if config.OLLAMA_API == "native" else f"{base}/v1/chat/completions"



def looks_foreign(text: str) -> bool:
    """Ответ съехал на иероглифы?

    qwen — модель китайского происхождения, и на коротких промптах она
    периодически отвечает по-китайски, даже когда весь разговор русский.
    В документ такое пускать нельзя, поэтому проверяем результат, а не
    надеемся на инструкцию.

    Условия два сразу, и это важно: одиночный термин в скобках внутри русской
    фразы — не повод выбрасывать разумный ответ целиком, а вот десятки знаков
    означают, что модель ушла на другой язык.
    """
    if not text:
        return False
    cjk = sum(
        1
        for ch in text
        if "一" <= ch <= "鿿"  # китайские иероглифы
        or "぀" <= ch <= "ヿ"  # японские каны, попадаются рядом
    )
    return cjk >= 8 and cjk > len(text) * 0.02


def advise(hits: list, limit: int, ollama: str | None = None) -> dict:
    """Предложения по исправлению для самых серьёзных находок.

    Зовём модель по одной находке: так ответ конкретнее, чем при пересказе
    списком, и обрыв на одной не портит остальные. Ограничиваем количество —
    каждый вызов это секунды, а на сорока находках отчёт собирался бы полчаса.

    Любая ошибка молча пропускается: документ без предложения полезнее, чем
    отсутствие документа.
    """
    import requests

    out: dict[str, str] = {}
    if limit <= 0:
        return out

    native = config.OLLAMA_API == "native"
    url = chat_endpoint(ollama)
    print(f"  модель: {config.EXPAND_MODEL} на {url}")
    headers = {"Content-Type": "application/json", **config.auth_headers()}

    for hit in hits[:limit]:
        prompt = ADVICE_PROMPT.format(
            title=hit.title or "—",
            severity=SEVERITY_RU.get(hit.severity, hit.severity),
            cwe=hit.cwe or "не указан",
            component=hit.component or "не указан",
            location=hit.location or "не указано",
            description=(hit.description or "нет описания")[:1500],
            mitigation=(hit.mitigation or "нет")[:800],
        )
        body = {"model": config.EXPAND_MODEL, "stream": False}
        if native:
            body["messages"] = [{"role": "user", "content": prompt}]
            body["options"] = {"num_predict": 400}
        else:
            body["messages"] = [{"role": "user", "content": prompt}]
            body["max_tokens"] = 400

        try:
            resp = requests.post(url, json=body, headers=headers, timeout=180)
            resp.raise_for_status()
            data = resp.json()
            text = (
                data.get("message", {}).get("content", "")
                if native
                else data["choices"][0]["message"]["content"]
            )
        except Exception as e:
            log.debug("предложение для %s не получено: %s", hit.finding_id, e)
            print(f"  [!] находка {hit.finding_id}: предложение не получено ({e})")
            print(
                f"      адрес модели: {url}. Если он не виден с этой машины, "
                "задайте свой ключом --ollama http://АДРЕС:11434"
            )
            continue

        text = text.strip()
        # Некоторые модели пишут ход рассуждений перед ответом — в документ
        # ему не место
        if "</think>" in text:
            text = text.split("</think>", 1)[1].strip()

        if looks_foreign(text):
            # Одна повторная попытка с явным напоминанием. Если и она съехала,
            # лучше оставить находку без предложения, чем положить в документ
            # для руководства абзац на китайском
            print(f"  [!] находка {hit.finding_id}: ответ не на русском, повторяю")
            try:
                body["messages"] = [
                    {"role": "user", "content": prompt},
                    {
                        "role": "system",
                        "content": "Отвечай исключительно на русском языке.",
                    },
                ]
                resp = requests.post(url, json=body, headers=headers, timeout=180)
                resp.raise_for_status()
                data = resp.json()
                text = (
                    data.get("message", {}).get("content", "")
                    if native
                    else data["choices"][0]["message"]["content"]
                ).strip()
                if "</think>" in text:
                    text = text.split("</think>", 1)[1].strip()
            except Exception as e:
                log.debug("повтор для %s не удался: %s", hit.finding_id, e)
                text = ""

            if looks_foreign(text):
                print(f"  [!] находка {hit.finding_id}: пропускаю предложение")
                continue

        if text:
            out[hit.finding_id] = text
            print(f"  предложение готово: находка {hit.finding_id}")

    return out


def narrative(product: str, summary: dict, shown: int) -> str:
    """Абзац-резюме без модели: цифры и вывод из них.

    Пишется шаблоном намеренно. Резюме для руководства — то место, где
    выдумка обходится дороже всего, а сказать тут надо ровно то, что видно
    из счётчиков.
    """
    total = sum(summary.values())
    if not total:
        return f"По продукту {product} в индексе нет находок под заданные условия."

    parts = [f"Под условия отбора попало {plural(total, 'находка', 'находки', 'находок')}."]

    # Перечисляем только непустые уровни: «0 высоких» в сводке для
    # руководства выглядит как недоделка, а не как информация
    urgent = []
    if summary.get("Critical"):
        urgent.append(plural(summary["Critical"], "критичная", "критичные", "критичных"))
    if summary.get("High"):
        urgent.append(plural(summary["High"], "высокая", "высокие", "высоких"))

    if urgent:
        parts.append("Первоочередного внимания требуют " + " и ".join(urgent) + ".")
    else:
        parts.append("Критичных и высоких среди них нет.")

    if shown < total:
        parts.append(f"В документ вошли первые {shown}, начиная с самых серьёзных.")
    return " ".join(parts)



def from_server(url: str, product: str, status: str, severity: str | None, limit: int):
    """Забрать находки с MCP-сервера вместо обращения к Qdrant.

    Нужно, чтобы отчёт можно было собрать с рабочей машины: рендер ничего не
    знает ни про базу, ни про DefectDojo, ему хватает списка находок. Тогда
    файл ложится там, где его запустили, а не на сервере — копировать ничего
    не надо.
    """
    import requests

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "dojo_findings",
            "arguments": {
                "product": product,
                "status": status,
                "limit": limit,
                "response_format": "report",
            },
        },
    }
    if severity:
        body["params"]["arguments"]["severity"] = severity

    resp = requests.post(
        f"{url.rstrip('/')}/mcp",
        json=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "сервер вернул ошибку"))

    payload = json.loads(data["result"]["content"][0]["text"])
    if "error" in payload:
        raise RuntimeError(payload["error"])

    hits = [
        dojo_retriever.Hit(
            finding_id=str(f.get("id", "")),
            title=f.get("title", ""),
            severity=f.get("severity", ""),
            status=f.get("status", ""),
            product=f.get("product", product),
            scanner=f.get("scanner", ""),
            cwe=f.get("cwe", ""),
            component=f.get("component", ""),
            location=f.get("location", ""),
            url=f.get("url", ""),
            found_at=f.get("found", ""),
            description=f.get("description", ""),
            mitigation=f.get("mitigation", ""),
            impact=f.get("impact", ""),
        )
        for f in payload.get("findings", [])
    ]
    return payload.get("product", product), payload.get("summary", {}), hits


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    ap = argparse.ArgumentParser(description="HTML-отчёт по уязвимостям продукта")
    ap.add_argument("--product", required=True, help="название продукта")
    ap.add_argument("--severity", help="уровень: критичные, высокие, средние...")
    ap.add_argument("--status", default="open", help="состояние, по умолчанию open")
    ap.add_argument("--limit", type=int, default=50, help="сколько находок включить")
    ap.add_argument("-o", "--out", help="куда сохранить (по умолчанию /docs/reports)")
    ap.add_argument(
        "--advice",
        type=int,
        default=0,
        metavar="N",
        help="попросить модель предложить исправление для N самых серьёзных "
        "находок (0 — не просить). Каждая занимает секунды",
    )
    ap.add_argument(
        "--ollama",
        metavar="URL",
        help="адрес модели, если из .env не подходит: в нём часто записано имя, "
        "которое резолвится только внутри докера",
    )
    ap.add_argument(
        "--server",
        metavar="URL",
        help="взять находки с MCP-сервера (http://СЕРВЕР:8012) и сохранить файл "
        "локально — так отчёт собирается с рабочей машины, без доступа к базе",
    )
    args = ap.parse_args()

    try:
        if args.server:
            name, summary, hits = from_server(
                args.server, args.product, args.status, args.severity, args.limit
            )
        else:
            result = dojo_retriever.search(
                product=args.product,
                status=args.status,
                severity=args.severity,
                limit=args.limit,
                report=True,
            )
            hits, summary, name = result["hits"], result["summary"], result["product"]
    except (dojo_retriever.DojoSearchError, dojo.DojoError) as e:
        print(e)
        return 1
    except Exception as e:
        print(f"Не удалось получить находки: {e}")
        return 1

    advice = {}
    if args.advice:
        print(f"Готовлю предложения по исправлению (до {args.advice} находок)...")
        advice = advise(hits, args.advice, args.ollama)

    default_dir = Path.cwd() if args.server else Path("/docs/reports")
    out = Path(args.out) if args.out else default_dir / (
        f"{name.replace('/', '-')}-{datetime.now():%Y-%m-%d}.html"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render(
            name,
            summary,
            hits,
            narrative(name, summary, len(hits)),
            {"состояние": args.status, "уровень": args.severity or "все"},
            advice,
        ),
        encoding="utf-8",
    )

    print(f"Находок в выборке: {sum(summary.values())}, в документ вошло: {len(hits)}")
    print(f"Файл: {out}")
    print("Открыть в браузере; печать в PDF — Ctrl+P, вёрстка для печати готова.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
