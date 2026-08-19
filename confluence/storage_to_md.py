"""Storage-формат Confluence (XHTML) -> Markdown.

Задача не в полноте конвертации, а в пригодности для поиска: нужны заголовки
(по ним режет kb/doc_index.py), читаемый текст и код в блоках. Оформление,
которое не влияет на смысл, отбрасывается.

Обрабатываются вещи, на которых обычно ломаются наивные конвертеры:
  - макросы <ac:structured-macro>: code, info/note/warning, остальные
  - таблицы -> markdown-таблицы
  - вложенные списки с сохранением уровней
  - ссылки на другие страницы <ac:link><ri:page>
  - изображения и вложения (текстом, файл всё равно не достать)
  - HTML-сущности и неразрывные пробелы

Стандартная библиотека, без зависимостей: скрипт поедет в закрытый контур,
где ставить пакеты может быть нельзя.
"""

import html
import re
from xml.etree import ElementTree

# storage-формат использует пространства имён ac: и ri: без объявления,
# поэтому парсер спотыкается - объявляем сами
NS_WRAPPER = (
    '<?xml version="1.0"?><root xmlns:ac="http://atlassian.com/content" '
    'xmlns:ri="http://atlassian.com/resource/identifier">{}</root>'
)

AC = "{http://atlassian.com/content}"
RI = "{http://atlassian.com/resource/identifier}"

PANEL_MACROS = {"info", "note", "warning", "tip", "panel"}


def _text(el) -> str:
    """Весь текст внутри элемента, включая вложенные теги."""
    return "".join(el.itertext())


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _macro(el, out: list) -> None:
    name = el.get(f"{AC}name", "")

    if name == "code":
        lang = ""
        body = ""
        for child in el:
            if child.tag == f"{AC}parameter" and child.get(f"{AC}name") == "language":
                lang = (child.text or "").strip()
            if child.tag == f"{AC}plain-text-body":
                body = child.text or ""
        out.append(f"```{lang}\n{body.strip()}\n```")
        return

    if name in PANEL_MACROS:
        # Панель - это выделенный абзац. Оформление теряем, текст сохраняем:
        # в нём часто самое важное ("красный пайплайн не повод для аппрува")
        inner = _clean(_text(el))
        if inner:
            out.append(f"> {inner}")
        return

    # Незнакомый макрос: берём текст, если он есть. Оглавления и подобное
    # текста не содержат и отвалятся сами
    inner = _clean(_text(el))
    if inner:
        out.append(inner)


def _table(el, out: list) -> None:
    rows = []
    for tr in el.iter("tr"):
        cells = [_clean(_text(c)) for c in tr if c.tag in ("td", "th")]
        if cells:
            rows.append(cells)
    if not rows:
        return

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    # Таблица - ОДИН элемент вывода: строки склеиваются переводами строки.
    # Если добавлять их по отдельности, между ними встанут пустые строки
    # (элементы соединяются через \n\n), и markdown таблицу не соберёт
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    out.append("\n".join(lines))


def _list_lines(el, level: int = 0) -> list[str]:
    """Строки списка вместе с вложенными уровнями, без пустых разделителей.

    Список обязан остаться ОДНИМ блоком: элементы вывода склеиваются через
    пустую строку, а наш чанкер режет длинные разделы как раз по ним - список
    разъехался бы по разным чанкам, и «шаг 3» потерял бы «шаги 1-2».
    """
    lines: list[str] = []
    ordered = el.tag == "ol"
    indent = "  " * level
    for i, li in enumerate(el.findall("li"), 1):
        # текст самого пункта, без вложенных списков
        own = "".join(
            part for part in li.itertext()
            if part not in {t for sub in li.findall("ul") + li.findall("ol") for t in sub.itertext()}
        )
        marker = f"{i}." if ordered else "-"
        line = _clean(own)
        if line:
            lines.append(f"{indent}{marker} {line}")
        for sub in li:
            if sub.tag in ("ul", "ol"):
                lines.extend(_list_lines(sub, level + 1))
    return lines


def _list(el, out: list) -> None:
    lines = _list_lines(el)
    if lines:
        out.append("\n".join(lines))


def _walk(el, out: list) -> None:
    tag = el.tag

    if tag == f"{AC}structured-macro":
        _macro(el, out)
        return

    if tag == f"{AC}link":
        page = el.find(f"{RI}page")
        title = page.get(f"{RI}content-title") if page is not None else None
        out.append(_clean(_text(el)) or (title or ""))
        return

    if tag == f"{AC}image":
        att = el.find(f"{RI}attachment")
        name = att.get(f"{RI}filename") if att is not None else "изображение"
        out.append(f"[изображение: {name}]")
        return

    if re.fullmatch(r"h[1-6]", tag or ""):
        level = int(tag[1])
        out.append("#" * level + " " + _clean(_text(el)))
        return

    if tag == "table":
        _table(el, out)
        return

    if tag in ("ul", "ol"):
        _list(el, out)
        return

    if tag in ("p", "div", "blockquote"):
        line = _clean(_text(el))
        if line:
            out.append(line)
        return

    if tag == "pre":
        out.append("```\n" + _text(el).strip() + "\n```")
        return

    for child in el:
        _walk(child, out)


# XML знает только пять сущностей, а Confluence щедро сыплет HTML-ными
# (&mdash;, &nbsp;, &laquo;). На первой же такой парсер падает, и страница
# уходит в запасной путь - плоским текстом без заголовков и таблиц.
# Раскрываем их заранее, кроме тех пяти, что XML понимает сам.
XML_SAFE = {"&amp;", "&lt;", "&gt;", "&quot;", "&apos;"}
ENTITY = re.compile(r"&[a-zA-Z][a-zA-Z0-9]*;|&#\d+;|&#x[0-9a-fA-F]+;")


def _expand_entities(text: str) -> str:
    def repl(m: re.Match) -> str:
        token = m.group(0)
        if token in XML_SAFE:
            return token
        expanded = html.unescape(token)
        # раскрылось в голый амперсанд - вернём в безопасном виде,
        # иначе снова сломаем разбор
        return "&amp;" if expanded == "&" else expanded

    return ENTITY.sub(repl, text)


# Ссылки и картинки прячут смысл в АТРИБУТАХ, а не в тексте. Если такой тег
# вложен в абзац, обработчик абзаца берёт itertext() и получает пустоту:
# «Подробнее см. .» Поэтому подменяем их обычным текстом до разбора.
LINK_RE = re.compile(
    r'<ac:link[^>]*>.*?<ri:page[^>]*ri:content-title="([^"]*)"[^>]*/?>.*?</ac:link>',
    re.DOTALL,
)
IMAGE_RE = re.compile(
    r'<ac:image[^>]*>.*?ri:filename="([^"]*)".*?</ac:image>', re.DOTALL
)


def _inline_refs(text: str) -> str:
    text = LINK_RE.sub(lambda m: f"«{m.group(1)}»", text)
    return IMAGE_RE.sub(lambda m: f"[изображение: {m.group(1)}]", text)


def convert(storage: str, title: str = "") -> str:
    """XHTML storage-формат -> markdown. Заголовок страницы становится H1."""
    if not storage or not storage.strip():
        return f"# {title}\n" if title else ""

    wrapped = NS_WRAPPER.format(_inline_refs(_expand_entities(storage)))
    try:
        root = ElementTree.fromstring(wrapped)
    except ElementTree.ParseError:
        # Confluence изредка отдаёт незакрытые теги. Лучше отдать текст без
        # разметки, чем потерять страницу целиком
        text = re.sub(r"<[^>]+>", " ", storage)
        body = _clean(html.unescape(text))
        return (f"# {title}\n\n" if title else "") + body + "\n"

    out: list[str] = []
    for child in root:
        _walk(child, out)

    body = "\n\n".join(p for p in out if p)
    body = html.unescape(body)
    body = re.sub(r"\n{3,}", "\n\n", body)

    head = f"# {title}\n\n" if title else ""
    return head + body.strip() + "\n"
