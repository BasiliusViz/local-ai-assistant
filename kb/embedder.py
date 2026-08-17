"""Эмбеддинги через Ollama по OpenAI-совместимому эндпоинту.

Критично: модель обязана совпадать с той, которой индексировали
(index-files.ps1, bge-m3). Иначе векторы окажутся из разных пространств
и поиск будет возвращать шум, не падая с ошибкой.
"""

import httpx

from kb import config


class EmbedError(RuntimeError):
    pass


def embed(text: str) -> list[float]:
    """Вектор для одной строки."""
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    try:
        resp = httpx.post(
            f"{config.OLLAMA_URL}/embeddings",
            json={"model": config.EMBED_MODEL, "input": texts},
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise EmbedError(
            f"Не удалось получить эмбеддинги с {config.OLLAMA_URL}: {e}. "
            f"Проверь, что контейнер ollama запущен и модель "
            f"{config.EMBED_MODEL} загружена."
        ) from e

    data = resp.json().get("data", [])
    vectors = [item["embedding"] for item in data]

    if len(vectors) != len(texts):
        raise EmbedError(
            f"Ollama вернула {len(vectors)} векторов на {len(texts)} строк"
        )

    got = len(vectors[0])
    if got != config.EMBED_DIM:
        raise EmbedError(
            f"Размерность вектора {got}, а коллекция ждёт {config.EMBED_DIM}. "
            f"Похоже, модель эмбеддингов не та, которой индексировали."
        )

    return vectors


if __name__ == "__main__":
    v = embed("проверка")
    print(f"OK, размерность: {len(v)}")
