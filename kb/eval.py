"""Оценка качества поиска на эталонных вопросах.

Без такого набора любое «улучшение» неотличимо от случайности — мы это уже
проходили на трёх тестовых файлах, где реранкер выиграл один вопрос и проиграл
другой.

Критерий простой: в топ-K должен попасть чанк, содержащий маркер — слово или
команду, без которых правильного ответа не бывает.

Запуск:
    python -m kb.eval              # текущие настройки
    KB_RERANK=1 python -m kb.eval  # с реранкером
"""

import logging
import time

from kb import config
from kb.retriever import search

logging.disable(logging.INFO)

# (вопрос, маркеры) — маркер ищется в тексте чанка без учёта регистра.
# Вопросы намеренно разговорные: именно на них ломался поиск раньше.
CASES = [
    ("как отменить последний коммит?", ["--amend", "reset"]),
    ("как изменить сообщение уже сделанного коммита", ["--amend"]),
    ("чем merge отличается от rebase", ["перебазирован", "rebase"]),
    ("как посмотреть кто менял конкретную строку кода", ["blame"]),
    ("как временно отложить незаконченную работу", ["stash", "припрятать"]),
    ("как найти коммит который сломал сборку", ["bisect"]),
    ("как игнорировать файлы чтобы они не попадали в репозиторий", [".gitignore"]),
    ("как склеить несколько коммитов в один", ["squash", "объедин"]),
    ("что делать если случайно удалил ветку", ["reflog"]),
    ("как посмотреть отличия перед коммитом", ["diff"]),
    ("как забрать изменения с сервера", ["fetch", "pull"]),
    ("как создать метку для релиза", ["tag", "метк"]),
    ("как настроить имя и почту для коммитов", ["user.name", "user.email"]),
    ("как отменить изменения в файле не сохраняя их", ["checkout", "restore"]),
    ("как перенести один коммит из другой ветки", ["cherry-pick"]),
]


def main() -> None:
    top_k = 3
    hits = 0
    misses = []
    total_time = 0.0

    mode = "с реранкером" if config.RERANK else "только векторы"
    print(f"Режим: {mode}, top-{top_k}, вопросов: {len(CASES)}\n")

    for question, markers in CASES:
        start = time.time()
        found = search(question, top_k=top_k, source="progit").hits
        total_time += time.time() - start

        texts = " ".join(h.text.lower() for h in found)
        ok = any(m.lower() in texts for m in markers)
        hits += ok
        if not ok:
            misses.append(question)

        mark = "+" if ok else "-"
        top = found[0].text.replace("\n", " ")[:46] if found else "(пусто)"
        print(f"  [{mark}] {question[:44]:46} {top}")

    print(f"\nПопаданий: {hits}/{len(CASES)} ({hits / len(CASES) * 100:.0f}%)")
    print(f"Среднее время: {total_time / len(CASES) * 1000:.0f} мс")
    if misses:
        print("\nНе нашлось:")
        for m in misses:
            print(f"  - {m}")


if __name__ == "__main__":
    main()
