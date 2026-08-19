"""Расширение запроса: несколько формулировок вместо одной.

Проблема, ради которой это делается — разрыв в словаре. Пользователь пишет
«как временно отложить незаконченную работу», в документации это `git stash`
и «припрятать». Для эмбеддера это разные области смысла, и нужный фрагмент
не попадает даже в кандидаты — исправлять порядок уже нечем.

Решение: сгенерировать 2 переформулировки в терминах документации, искать по
всем трём и слить результаты. Замер на нашем наборе показал, что именно такие
вопросы и промахивались.

Два предохранителя, оба намеренные:

1. **Исходный вопрос ВСЕГДА остаётся в наборе.** Исследование 2026 года («Out
   of Style: RAG's Fragility to Linguistic Variation») показало, что
   переписывание запроса стабильно ухудшает результат, когда искажает смысл.
   Оригинал в наборе не даёт переформулировкам увести поиск в сторону.
2. **`reasoning_effort: "none"`** — без него qwen3 уходит в рассуждения и
   вместо секунды тратит минуту. Задача простая, рассуждать не о чем.
"""

import json
import logging

import httpx

from kb import config

log = logging.getLogger(__name__)

PROMPT = """Ты помогаешь искать по внутренней технической документации: \
разработка ПО, системы контроля версий, DevOps, эксплуатация сервисов, \
информационная безопасность.

ВОПРОС ПОЛЬЗОВАТЕЛЯ: {query}

Переформулируй его {n} разными способами так, чтобы найти нужный раздел \
документации.

Главное правило: замени бытовые слова на профессиональные термины и названия \
команд, которыми это называется в документации. Примеры замен: «отложить \
работу» -> git stash, «откатить» -> rollback, «слить ветки» -> merge.

Требования:
- ПИШИ ПЕРЕФОРМУЛИРОВКИ НА ТОМ ЖЕ ЯЗЫКЕ, ЧТО И ВОПРОС. Документация написана \
на языке вопроса; перевод на английский уводит поиск к чужим документам
- названия команд и технические термины (git stash, merge request, rollback) \
оставляй английскими внутри русской фразы
- НЕ расшифровывай сокращения по-своему: «прод» это production (боевая среда), \
«мр» это merge request, «БД» это база данных
- сохраняй исходный смысл: не добавляй условий, которых в вопросе не было
- каждая переформулировка - законченный вопрос, а не список слов

Верни только JSON: {{"queries": ["...", "..."]}}"""


def expand(query: str) -> list[str]:
    """Исходный запрос плюс переформулировки. При сбое — только исходный."""
    if not config.EXPAND:
        return [query]

    messages = [
        {"role": "user", "content": PROMPT.format(query=query, n=config.EXPAND_VARIANTS)}
    ]
    # Нулевая температура: при 0.3 переформулировки менялись от запуска к
    # запуску, и один и тот же вопрос то находился, то нет. Воспроизводимость
    # здесь важнее разнообразия — его обеспечивают сами варианты запроса.
    # reasoning_effort=none обязателен: иначе модель уходит в рассуждения и
    # тратит минуту вместо секунды.
    if config.OLLAMA_API == "native":
        body = {
            "model": config.EXPAND_MODEL,
            "messages": messages,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0},
        }
    else:
        body = {
            "model": config.EXPAND_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "none",
            "temperature": 0,
        }

    try:
        resp = httpx.post(
            config.chat_url(),
            json=body,
            headers=config.auth_headers(),
            timeout=config.EXPAND_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        # Формат ответа: родное API кладёт текст в message.content,
        # OpenAI-совместимое — в choices[0].message.content
        if config.OLLAMA_API == "native":
            raw = payload["message"]["content"]
        else:
            raw = payload["choices"][0]["message"]["content"]
        variants = json.loads(raw).get("queries", [])
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        # Расширение — улучшение, а не необходимость
        log.warning("расширение запроса не удалось (%s), ищем как есть", e)
        return [query]

    out = [query]
    for v in variants[: config.EXPAND_VARIANTS]:
        if isinstance(v, str) and v.strip() and v.strip().lower() != query.lower():
            out.append(v.strip())

    log.info("запрос расширен: %s", out[1:])
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for q in [
        "как временно отложить незаконченную работу",
        "что делать если случайно удалил ветку",
        "куда писать если упал прод",
    ]:
        print(f"\n{q}")
        for v in expand(q):
            print(f"   -> {v}")
