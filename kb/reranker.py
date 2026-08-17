"""Вторая ступень поиска: переупорядочивание кандидатов через bge-reranker-v2-m3.

Зачем. Векторный поиск сравнивает два независимо сжатых вектора и на близких
темах перестаёт различать: замерено на testdocs, весь топ-5 укладывался в
0.451-0.472, где первым стоял нерелевантный кусок. Реранкер читает пару
"вопрос + текст" вместе и отвечает на прямой вопрос: это ответ или нет.

Замер на той же паре вопросов (сырые оценки):
    "как откатить деплой payment-gateway"  верный +5.18, неверный -11.03
    "сколько аппрувов нужно для MR"        верный +7.06, неверный -10.06
Разрыв 16-17 пунктов вместо сотых у векторов - отсюда порог по нулю.

Почему не qwen3, которая уже загружена. Проверено: она судит правильно, но
только с включёнными рассуждениями - 17 секунд на фрагмент. Без них раздаёт
всем подряд одну и ту же оценку, то есть не оценивает вовсе. Генеративная
модель вынуждена рассуждать словами; cross-encoder сразу выдаёт число.

Сервис на CPU намеренно: 568M модели процессору по силам, кандидатов всего 20,
а видеопамять остаётся генеративной модели. При необходимости переносится на
отдельную машину - меняется только KB_RERANKER_URL.
"""

import logging

import httpx

from kb import config

log = logging.getLogger(__name__)


def rerank(query: str, texts: list[str]) -> list[float]:
    """Оценки релевантности для каждого текста, в том же порядке.

    Пустой список означает, что реранк не сработал: вызывающий код должен
    остаться на порядке векторного поиска, а не падать.
    """
    if not texts:
        return []

    try:
        resp = httpx.post(
            f"{config.RERANKER_URL}/rerank",
            json={"query": query, "texts": texts, "raw_scores": True},
            timeout=config.RERANK_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        # Реранк - улучшение, а не необходимость. Сервис лежит - отдаём
        # векторный порядок, он хуже, но работает
        log.warning("реранкер недоступен (%s), остаётся порядок Qdrant", e)
        return []

    out = [config.RERANK_MIN - 1.0] * len(texts)
    for item in items:
        try:
            idx = int(item["index"])
            if 0 <= idx < len(texts):
                out[idx] = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def available() -> bool:
    try:
        return httpx.get(f"{config.RERANKER_URL}/health", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("сервис доступен:", available())
    q = "как откатить деплой payment-gateway"
    docs = [
        "Откат выполняется командой kubectl rollout undo deployment/payment-gateway.",
        "Получить доступы: корпоративная почта, VPN, GitLab, Confluence.",
    ]
    for text, score in zip(docs, rerank(q, docs)):
        print(f"  {score:+7.2f}  {text[:60]}")
