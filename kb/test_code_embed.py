"""Индексация кода не падает от длинных кусков и сбоев эмбеддера, умеет продолжать.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_code_embed

Эмбеддер и Qdrant подменены. Поводом стал прогон релиза CB18.5: через час
работы кусок в 4069 символов получил 400 «input length exceeds the context
length», и всё пришлось бы начинать заново.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from kb import code_index
from kb.embedder import EmbedError

TOO_LONG = EmbedError("400 от http://gw/api/embed: input length exceeds the context length")


def fake_embed(limit: int, calls: list):
    """Эмбеддер с окном limit символов: длиннее — 400, как у шлюза."""
    def embed(texts):
        calls.append([len(t) for t in texts])
        if any(len(t) > limit for t in texts):
            raise TOO_LONG
        return [[float(len(t))] for t in texts]
    return embed


class EmbedSafe(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.sleep = self.slept.append

    def test_vector_text_is_capped(self):
        calls = []
        out = code_index.embed_safe(["x" * 5000, "y" * 10], fake_embed(10**6, calls), self.sleep)
        self.assertEqual(calls, [[code_index.EMBED_MAX_CHARS, 10]])
        self.assertEqual(len(out), 2)

    def test_too_long_is_halved_not_fatal(self):
        calls = []
        out = code_index.embed_safe(["a" * 3000, "b" * 100], fake_embed(1000, calls), self.sleep)
        # пачка целиком -> 400, дальше по одному; длинный укорачивается до влезающего
        self.assertEqual(out, [[750.0], [100.0]])
        self.assertEqual(calls[0], [3000, 100])
        self.assertEqual(self.slept, [], "ошибку длины не повторяют с паузой")

    def test_hopeless_piece_still_fails(self):
        with self.assertRaises(EmbedError):
            code_index.embed_safe(["z" * 3000], fake_embed(10, []), self.sleep)

    def test_transient_error_is_retried(self):
        attempts = []

        def flaky(texts):
            attempts.append(1)
            if len(attempts) < 3:
                raise EmbedError("Не удалось получить эмбеддинги: connection reset")
            return [[1.0] for _ in texts]

        self.assertEqual(code_index.embed_safe(["t"], flaky, self.sleep), [[1.0]])
        self.assertEqual(self.slept, [5, 10])

    def test_persistent_error_gives_up(self):
        def down(texts):
            raise EmbedError("Не удалось получить эмбеддинги: connection refused")

        with self.assertRaises(EmbedError):
            code_index.embed_safe(["t"], down, self.sleep)
        self.assertEqual(len(self.slept), code_index.EMBED_RETRIES - 1)


class Resume(unittest.TestCase):
    def test_existing_ids_pages_through_collection(self):
        pages = {
            None: ([SimpleNamespace(id="a"), SimpleNamespace(id="b")], "p2"),
            "p2": ([SimpleNamespace(id="c")], None),
        }

        class Client:
            def scroll(self, collection_name, limit, offset, with_payload, with_vectors):
                assert not with_payload and not with_vectors, "грузить только id"
                return pages[offset]

        self.assertEqual(code_index.existing_ids(Client(), "code_cb"), {"a", "b", "c"})

    def test_point_id_is_stable(self):
        chunk = {"repo": "core", "path": "a.go", "symbol": "Run", "line_start": 10}
        self.assertEqual(code_index.point_id(chunk), code_index.point_id(dict(chunk)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
