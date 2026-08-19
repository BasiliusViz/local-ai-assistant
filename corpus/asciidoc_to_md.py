"""AsciiDoc -> Markdown для тестового корпуса.

Задача не в полноте конвертации, а в пригодности для поиска: нужны заголовки
(на них режет kb/doc_index.py) и чистый текст без разметочного шума, который
попадёт в эмбеддинги и загрязнит их.

Запуск:
    python corpus/asciidoc_to_md.py <откуда> <куда>
"""

import re
import sys
from pathlib import Path

# (((индексные записи))) — служебные пометки для указателя книги,
# в тексте не видны, а в эмбеддинг попадут мусором
INDEX_ENTRY = re.compile(r"\(\(\([^)]*\)\)\)")
# [[anchor_id]] и [source,console] — якоря и атрибуты блоков.
# Скобки жадные до последней: [[...]] иначе не вычищается целиком
ATTR_LINE = re.compile(r"^\[.*\]\s*$")
# image::path[alt] — картинку не сохранить, подпись бесполезна без неё
IMAGE = re.compile(r"^image::.*$")
HEADING = re.compile(r"^(=+)\s+(.*)$")
# ---- и ==== вокруг блоков кода
BLOCK_DELIM = re.compile(r"^(-{4,}|\.{4,}|={4,}|\*{4,})\s*$")


def convert(text: str) -> str:
    out = []
    in_code = False

    for line in text.splitlines():
        if BLOCK_DELIM.match(line):
            # чередуем открытие и закрытие блока кода
            out.append("```")
            in_code = not in_code
            continue

        if in_code:
            out.append(line)
            continue

        if IMAGE.match(line):
            continue

        line = INDEX_ENTRY.sub("", line)

        if ATTR_LINE.match(line):
            continue

        m = HEADING.match(line)
        if m:
            level = len(m.group(1))
            out.append("#" * level + " " + m.group(2).strip())
            continue

        # AsciiDoc-типографика. Тире ТОЛЬКО окружённое пробелами: иначе
        # git commit --amend превращается в --amend с длинным тире,
        # то есть в корпус попадают неработающие команды
        line = line.replace(" -- ", " — ").replace("`+", "`").replace("+`", "`")
        out.append(line)

    result = "\n".join(out)
    # схлопываем пустоты, оставшиеся от вырезанных строк
    return re.sub(r"\n{3,}", "\n\n", result).strip() + "\n"


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)

    count = 0
    for path in sorted(src.rglob("*.asc")):
        text = path.read_text(encoding="utf-8", errors="replace")
        md = convert(text)

        # мелочь вроде оглавлений и лицензий только зашумит корпус
        if len(md) < 800:
            continue

        # глава становится папкой: индексатор кладёт её имя в поле space,
        # получаем готовое разделение для фильтров поиска
        rel = path.relative_to(src)
        chapter = rel.parts[0] if len(rel.parts) > 1 else "misc"
        out_dir = dst / chapter
        out_dir.mkdir(exist_ok=True)
        (out_dir / (path.stem + ".md")).write_text(md, encoding="utf-8")
        count += 1

    total = sum(f.stat().st_size for f in dst.rglob("*.md"))
    print(f"Сконвертировано файлов: {count}")
    print(f"Объём: {total / 1024:.0f} КБ")
    print(f"Разделов: {len(list(dst.iterdir()))}")


if __name__ == "__main__":
    main()
