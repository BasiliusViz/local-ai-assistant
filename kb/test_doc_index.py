"""Проверка индексатора документов: нарезка и устойчивость к сбоям эмбеддера.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_doc_index

Нарезке ничего не нужно. Тестам прогона нужен Qdrant: они заводят временную
коллекцию и в конце её удаляют, эмбеддер подменён — Ollama и сеть не нужны.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qdrant_client import models

from kb import config, doc_index
from kb.embedder import EmbedError
from kb.retriever import client

LIMIT = doc_index.EMBED_MAX_CHARS


class SplitTest(unittest.TestCase):
    def test_normal_document_untouched(self):
        text = "# Регламент\n\nШаг 1. Сделать.\n\nШаг 2. Проверить.\n"
        self.assertEqual(
            doc_index.split_text(text),
            [{"heading": "Регламент", "text": "Шаг 1. Сделать.\n\nШаг 2. Проверить."}],
        )

    def test_huge_table_split_with_header(self):
        rows = "\n".join(f"| srv-{i:04} | nginx 1.25 | prod | владелец команда-{i} |" for i in range(400))
        text = "# Серверы\n\n| Хост | ПО | Среда | Владелец |\n|---|---|---|---|\n" + rows
        chunks = doc_index.split_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["text"]), LIMIT)
            self.assertTrue(c["text"].startswith("| Хост | ПО | Среда | Владелец |\n|---|"))
        # ни одна строка не потерялась
        body = "\n".join(c["text"] for c in chunks)
        self.assertIn("srv-0000", body)
        self.assertIn("srv-0399", body)

    def test_huge_code_block_split(self):
        log = "\n".join(f"2026-09-23 12:00:{i % 60:02} INFO request {i} ok" for i in range(600))
        chunks = doc_index.split_text(f"# Лог\n\n```\n{log}\n```\n")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c["text"]) <= LIMIT for c in chunks))

    def test_single_giant_line_cut(self):
        chunks = doc_index.split_text("# Blob\n\n" + "A" * (LIMIT * 3 + 10))
        self.assertEqual(len(chunks), 4)
        self.assertTrue(all(len(c["text"]) <= LIMIT for c in chunks))


def ok_embed(texts):
    return [[1.0] + [0.0] * (config.EMBED_DIM - 1) for _ in texts]


class RunTest(unittest.TestCase):
    """Прогон целиком на временной коллекции."""

    @classmethod
    def setUpClass(cls):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            client().get_collections()
        except Exception as e:
            raise unittest.SkipTest(f"Qdrant недоступен ({config.QDRANT_URL}): {e}")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="docindex-test-"))
        self.coll = f"docindex_selftest_{os.getpid()}"
        for i in range(1, 6):
            self.write(i, f"текст номер {i}")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        if client().collection_exists(self.coll):
            client().delete_collection(self.coll)

    def write(self, i, body):
        (self.root / f"doc{i}.md").write_text(f"# Док {i}\n\n{body}\n", encoding="utf-8")

    def run_index(self, embed=ok_embed):
        calls = []

        def spy(texts):
            calls.append(texts)
            return embed(texts)

        argv = ["doc_index", str(self.root), "--source", "t", "--collection", self.coll]
        with mock.patch.object(doc_index, "embed_batch", spy), mock.patch.object(
            sys, "argv", argv
        ), mock.patch("builtins.print"):
            code = doc_index.main()
        return code, calls

    def state(self):
        path = self.root / ".index_state.t.json"
        return json.loads(path.read_text(encoding="utf-8"))["files"]

    def points(self, rel):
        got, _ = client().scroll(
            self.coll,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="source_id", match=models.MatchValue(value=rel))]
            ),
            with_payload=True,
        )
        return [p.payload["text"] for p in got]

    def test_one_bad_file_does_not_stop_run(self):
        def embed(texts):
            if any("номер 3" in t for t in texts):
                raise EmbedError("400: input length exceeds the context length")
            return ok_embed(texts)

        code, _ = self.run_index(embed)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.state()), ["doc1.md", "doc2.md", "doc4.md", "doc5.md"])
        # следующий прогон берёт только недосчитанный
        code, calls = self.run_index()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("номер 3", calls[0][0])

    def test_failed_update_keeps_old_version_searchable(self):
        self.run_index()
        self.write(2, "новая версия")

        def embed(texts):
            if any("новая версия" in t for t in texts):
                raise EmbedError("400")
            return ok_embed(texts)

        self.run_index(embed)
        # старая версия в поиске, в состоянии — старый хеш: будет повтор
        self.assertEqual(self.points("doc2.md"), ["текст номер 2"])
        _, calls = self.run_index()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.points("doc2.md"), ["новая версия"])

    def test_embedder_down_stops_run_and_keeps_progress(self):
        for i in range(6, 30):
            self.write(i, f"текст номер {i}")

        # doc1 отрабатывает, дальше (doc10, doc11, ... по алфавиту) эмбеддер лежит
        fails = []

        def embed(texts):
            if not any(t.endswith("текст номер 1") for t in texts):
                fails.append(texts)
                raise EmbedError("Не удалось получить эмбеддинги: connection refused")
            return ok_embed(texts)

        with mock.patch.object(doc_index, "CHECKPOINT_EVERY", 1000):
            code, _ = self.run_index(embed)
        self.assertEqual(code, 1)
        # остановился после MAX_FAILS_IN_ROW неудач, а не молотил все 28
        self.assertEqual(len(fails), doc_index.MAX_FAILS_IN_ROW)
        # и сохранил сделанное, хотя до контрольной точки не дошёл
        self.assertIn("doc1.md", self.state())

    def test_emptied_file_removed_from_search(self):
        self.run_index()
        (self.root / "doc4.md").write_text("", encoding="utf-8")
        self.run_index()
        self.assertEqual(self.points("doc4.md"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
