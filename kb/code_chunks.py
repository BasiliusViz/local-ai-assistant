"""Нарезка кода на функции и классы для языков кроме Python — через tree-sitter.

Python режется в code_index.py стандартным `ast`, он проверен и остаётся как
был. Здесь — всё остальное: Go, Java, Kotlin, Groovy, JS/TS, C#, C/C++, PHP,
Ruby, Rust, Scala, Swift, Bash, PowerShell, Lua, Julia, Zig.

Разборщики — те же пакеты и версии, что ставит Graphify в образ code-graph:
раз тот образ собрался в контуре, зеркало PyPI их отдаёт.

Принцип тот же, что для Python: функция или метод — один чанк целиком, класс
(структура, интерфейс, impl) — отдельный чанк плюс его методы по отдельности.
Что не покрыто функциями (скрипты Jenkins, bash, верхний уровень Groovy), не
теряется — уходит кусками текста. Если разборщика нет или он споткнулся,
возвращается None, и файл индексируется кусками текста, как раньше.
"""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Расширение -> (модуль tree_sitter_*, функция языка)
LANGUAGES: dict[str, tuple[str, str]] = {
    ".go": ("go", "language"),
    ".java": ("java", "language"),
    ".kt": ("kotlin", "language"),
    ".kts": ("kotlin", "language"),
    ".groovy": ("groovy", "language"),
    ".gvy": ("groovy", "language"),
    ".gradle": ("groovy", "language"),
    ".js": ("javascript", "language"),
    ".jsx": ("javascript", "language"),
    ".mjs": ("javascript", "language"),
    ".cjs": ("javascript", "language"),
    ".ts": ("typescript", "language_typescript"),
    ".mts": ("typescript", "language_typescript"),
    ".cts": ("typescript", "language_typescript"),
    ".tsx": ("typescript", "language_tsx"),
    ".cs": ("c_sharp", "language"),
    ".c": ("c", "language"),
    # .h бывает и C, и C++; разборщик C++ читает C почти без потерь, наоборот — нет
    ".h": ("cpp", "language"),
    ".cpp": ("cpp", "language"),
    ".cc": ("cpp", "language"),
    ".cxx": ("cpp", "language"),
    ".hpp": ("cpp", "language"),
    ".hh": ("cpp", "language"),
    ".php": ("php", "language_php"),
    ".rb": ("ruby", "language"),
    ".rake": ("ruby", "language"),
    ".rs": ("rust", "language"),
    ".scala": ("scala", "language"),
    ".sc": ("scala", "language"),
    ".swift": ("swift", "language"),
    ".sh": ("bash", "language"),
    ".bash": ("bash", "language"),
    ".ps1": ("powershell", "language"),
    ".psm1": ("powershell", "language"),
    ".lua": ("lua", "language"),
    ".jl": ("julia", "language"),
    ".zig": ("zig", "language"),
}

# Файлы без расширения, у которых язык понятен по имени
NAMED_FILES: dict[str, tuple[str, str]] = {
    "Jenkinsfile": ("groovy", "language"),
    "Rakefile": ("ruby", "language"),
    "Gemfile": ("ruby", "language"),
}

# Функции и методы: чанк целиком, внутрь не спускаемся (вложенные лямбды и
# локальные функции — часть родителя)
FUNCTIONS = {
    "function_declaration",  # Go, JS/TS, Kotlin, Swift, Lua, Scala (без тела)
    "method_declaration",  # Go, Java, C#, PHP, Groovy
    "constructor_declaration",  # Java, C#
    "function_definition",  # C/C++, PHP, Bash, Scala, Groovy, Julia
    "method_definition",  # JS/TS
    "function_item",  # Rust
    "method",  # Ruby
    "singleton_method",  # Ruby
    "function_statement",  # PowerShell
    "class_method_definition",  # PowerShell
    "function_signature_item",  # Rust, в трейтах
}

# Контейнеры: сам — отдельным чанком (шапка), плюс спускаемся за методами
CONTAINERS = {
    "class_declaration",  # Java, C#, Kotlin, JS/TS, PHP, Swift, Groovy
    "class_definition",  # Scala
    "class_specifier",  # C++
    "struct_specifier",  # C/C++
    "interface_declaration",  # Java, C#, TS
    "enum_declaration",
    "record_declaration",  # Java, C#
    "struct_declaration",  # C#
    "object_declaration",  # Kotlin
    "object_definition",  # Scala
    "trait_definition",  # Scala
    "trait_item",  # Rust
    "struct_item",  # Rust
    "enum_item",  # Rust
    "impl_item",  # Rust
    "protocol_declaration",  # Swift
    "class",  # Ruby
    "module",  # Ruby
    "type_spec",  # Go: type X struct / interface
    "class_statement",  # PowerShell
}

# Значения, которые делают переменную функцией: const handler = () => {}
FUNCTION_VALUES = {"arrow_function", "function_expression", "function"}

# Тело функции — признак того, что это реализация, а не объявление в
# интерфейсе. Объявления без тела мелкие и уже есть в чанке интерфейса
BODY_TYPES = {
    "block", "body", "function_body", "compound_statement", "closure",
    "constructor_body", "statement_block", "declaration_list", "script_block",
    "do_block", "body_statement",
}

# Шапка класса короче, чем чанк функции: методы и так идут отдельно, а
# дублировать весь класс значит вдвое раздуть индекс и ночной прогон
CONTAINER_CHARS = 1500
TEXT_STEP = 120


def language_for(path: Path) -> tuple[str, str] | None:
    return NAMED_FILES.get(path.name) or LANGUAGES.get(path.suffix.lower())


@lru_cache(maxsize=None)
def _parser(module: str, func: str):
    """Разборщик языка или None, если пакета нет — тогда нарезка текстом."""
    try:
        from tree_sitter import Language, Parser

        mod = importlib.import_module(f"tree_sitter_{module}")
        return Parser(Language(getattr(mod, func)()))
    except Exception as e:  # нет пакета, несовместимая версия
        log.warning("разборщик %s недоступен (%s) — такие файлы пойдут текстом", module, e)
        return None


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _has_body(node) -> bool:
    if node.child_by_field_name("body") is not None:
        return True
    return any(c.type in BODY_TYPES for c in node.named_children)


def _declarator_name(node) -> str:
    """C/C++: имя спрятано в цепочке declarator -> function_declarator -> ..."""
    cur = node
    for _ in range(10):
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            break
        cur = nxt
    return _text(cur)


def _name(node) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(name)
    if node.type == "impl_item":  # Rust: impl Shape for Point -> Point
        return _text(node.child_by_field_name("type"))
    if node.child_by_field_name("declarator") is not None:
        return _declarator_name(node)
    for child in node.named_children:  # PowerShell и подобные: имя — первый простой узел
        if child.type in ("function_name", "simple_name", "identifier", "type_identifier",
                          "simple_identifier", "constant"):
            return _text(child)
    return ""


def _go_receiver(node) -> str:
    """Go: func (s *Server) Start() -> Server."""
    recv = node.child_by_field_name("receiver")
    if recv is None:
        return ""
    found = ""

    def walk(n):
        nonlocal found
        if n.type == "type_identifier":
            found = _text(n)
        for c in n.named_children:
            walk(c)

    walk(recv)
    return found


def _signature(node) -> str:
    """Первая строка объявления без тела — то, что человек читает первым."""
    first = _text(node).splitlines()[0] if node.text else ""
    return first.split("{")[0].strip()[:200]


def _doc(node) -> str:
    """Комментарии прямо над объявлением: в Go и Java там описание."""
    lines = []
    prev = node.prev_named_sibling
    expect = node.start_point[0]
    while prev is not None and "comment" in prev.type and prev.end_point[0] >= expect - 1:
        lines.insert(0, _text(prev))
        expect = prev.start_point[0]
        prev = prev.prev_named_sibling
    cleaned = []
    for line in "\n".join(lines).splitlines():
        line = line.strip().lstrip("/*#-").rstrip("*/").strip()
        if line:
            cleaned.append(line)
    return " ".join(cleaned)[:300]


def _chunk(node, symbol: str, kind: str, limit: int) -> dict:
    return {
        "symbol": symbol,
        "kind": kind,
        "signature": _signature(node),
        "doc": _doc(node),
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "text": _text(node)[:limit],
    }


def jenkins_step(path: Path) -> str:
    """Имя шага общей библиотеки Jenkins: vars/abActions.groovy -> abActions.

    Jenkins превращает каждый файл vars/*.groovy в глобальный шаг с именем
    файла: в пайплайне пишут `abActions(...)`, а выполняется `call()` из
    этого файла. Пусто — файл не из vars/.
    """
    if path.suffix == ".groovy" and path.parent.name == "vars":
        return path.stem
    return ""


def _name_jenkins_step(out: list[dict], step: str) -> None:
    """call -> abActions, прочие функции файла -> abActions.helper.

    Без этого в индексе десятки одинаковых `call`, а спрашивают люди именем
    шага, которое видят в пайплайне. Прочие функции Jenkins и вызывает так:
    `abActions.helper(...)`.
    """
    for c in out:
        if c["kind"] == "text":
            c["symbol"] = step
        elif c["symbol"] == "call":
            c["symbol"] = step
        elif "." not in c["symbol"]:
            c["symbol"] = f"{step}.{c['symbol']}"


def text_pieces(path: Path, lines: list[str], ranges: list[tuple[int, int]], limit: int) -> list[dict]:
    """Куски по TEXT_STEP строк из указанных диапазонов (0-based, конец не включён)."""
    out = []
    for lo, hi in ranges:
        for start in range(lo, hi, TEXT_STEP):
            stop = min(start + TEXT_STEP, hi)
            piece = "\n".join(lines[start:stop])
            if not piece.strip():
                continue
            out.append({
                "symbol": path.name,
                "kind": "text",
                "signature": "",
                "doc": "",
                "line_start": start + 1,
                "line_end": stop,
                "text": piece[:limit],
            })
    return out


def chunks(path: Path, source: str, limit: int) -> list[dict] | None:
    """Функции и классы файла. None — языка нет или разбор не удался."""
    lang = language_for(path)
    if lang is None:
        return None
    parser = _parser(*lang)
    if parser is None:
        return None
    try:
        tree = parser.parse(source.encode("utf-8", errors="replace"))
    except Exception as e:
        log.warning("не разобрался %s: %s", path.name, e)
        return None

    out: list[dict] = []

    def walk(node, owner: str) -> None:
        for child in node.named_children:
            t = child.type
            if t in FUNCTIONS:
                if not _has_body(child):
                    continue
                name = _name(child)
                if not name:
                    continue
                recv = _go_receiver(child) if t == "method_declaration" else ""
                prefix = recv or owner
                symbol = f"{prefix}.{name}" if prefix else name
                out.append(_chunk(child, symbol, "method" if prefix else "function", limit))
            elif t in CONTAINERS:
                name = _name(child)
                symbol = f"{owner}.{name}" if owner and name else (name or owner)
                if name:
                    out.append(_chunk(child, symbol, "class", CONTAINER_CHARS))
                walk(child, symbol)
            elif t == "variable_declarator":
                value = child.child_by_field_name("value")
                name = child.child_by_field_name("name")
                if value is not None and name is not None and value.type in FUNCTION_VALUES:
                    symbol = f"{owner}.{_text(name)}" if owner else _text(name)
                    # диапазон — весь оператор (const ... = () => {}), а не
                    # только правая часть: так видно, как функция объявлена
                    out.append(_chunk(node, symbol, "function", limit))
                else:
                    walk(child, owner)
            else:
                walk(child, owner)

    walk(tree.root_node, "")
    out.extend(_uncovered(path, source, out, limit))

    step = jenkins_step(path)
    if step:
        _name_jenkins_step(out, step)
    return out


def _uncovered(path: Path, source: str, out: list[dict], limit: int) -> list[dict]:
    # Что функциями не покрыто, не теряем. Для классов на Java это пустяки
    # (импорты), а в Jenkinsfile, bash и groovy-скриптах там почти весь смысл.
    # Порог — половина непустых строк: меньше — отдаём непокрытое текстом
    lines = source.splitlines()
    covered = [False] * len(lines)
    for c in out:
        for i in range(c["line_start"] - 1, min(c["line_end"], len(lines))):
            covered[i] = True
    meaningful = [i for i, line in enumerate(lines) if line.strip()]
    if not meaningful:
        return []
    share = sum(covered[i] for i in meaningful) / len(meaningful)
    if share >= 0.5:
        return []
    ranges: list[tuple[int, int]] = []
    start = None
    for i, is_covered in enumerate(covered + [True]):
        if not is_covered and start is None:
            start = i
        elif is_covered and start is not None:
            ranges.append((start, i))
            start = None
    return text_pieces(path, lines, ranges, limit)
