"""Проверка индексатора DefectDojo: отбор в индекс, сбои эмбеддера, прогресс.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_dojo_index

Отбору ничего не нужно: API DefectDojo подменён. Тестам прогона нужен
Qdrant: временная коллекция, в конце удаляется. Эмбеддер подменён.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from kb import config, dojo, dojo_index
from kb.embedder import EmbedError
from kb.retriever import client


def item(fid, sev, **flags):
    base = {"id": fid, "title": f"Находка {fid}", "severity": sev, "active": True,
            "false_p": False, "risk_accepted": False, "is_mitigated": False}
    base.update(flags)
    return base


API = [
    item(1, "Critical"),
    item(2, "High"),
    item(3, "High", active=False, is_mitigated=True),
    item(4, "Medium"),
    item(5, "Info"),
]


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def all_findings(client, product_id, severity=None):
            self.asked.append(severity)
            return [x for x in API if severity in (None, x["severity"])]

        for p in (
            mock.patch.object(dojo, "configured", lambda: True),
            mock.patch.object(dojo, "_client", lambda: nullcontext(object())),
            mock.patch.object(dojo, "products", lambda c: [{"id": 1, "name": "ABInf"}]),
            mock.patch.object(dojo, "all_findings", all_findings),
            mock.patch("builtins.print"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, **env):
        with mock.patch.dict(os.environ, {"DOJO_INDEX_SEVERITIES": "", "DOJO_INDEX_STATUS": "", **env}):
            return sorted(r["id"] for r in dojo_index.fetch())

    def test_everything_by_default(self):
        self.assertEqual(self.fetch(), [1, 2, 3, 4, 5])
        self.assertEqual(self.asked, [None])

    def test_severities_asked_from_dojo(self):
        # уровень отбирает сам DefectDojo: лишнее не скачивается вовсе
        self.assertEqual(self.fetch(DOJO_INDEX_SEVERITIES="Critical,высокий"), [1, 2, 3])
        self.assertEqual(self.asked, ["Critical", "High"])

    def test_statuses_filtered_here(self):
        self.assertEqual(
            self.fetch(DOJO_INDEX_SEVERITIES="Critical,High", DOJO_INDEX_STATUS="open"), [1, 2]
        )

    def test_bad_status_explained(self):
        with self.assertRaisesRegex(dojo.DojoError, "DOJO_INDEX_STATUS"):
            self.fetch(DOJO_INDEX_STATUS="открытые")


def ok_embed(texts):
    return [[1.0] + [0.0] * (config.EMBED_DIM - 1) for _ in texts]


def record(i):
    return {"id": i, "product": "ABInf", "title": f"Находка {i}", "severity": "High",
            "status": "open", "scanner": "", "cwe": "", "component": "", "location": "",
            "description": "", "mitigation": "", "impact": "", "date": "", "updated": "",
            "url": f"https://dojo/finding/{i}"}


class IndexRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            client().get_collections()
        except Exception as e:
            raise unittest.SkipTest(f"Qdrant недоступен ({config.QDRANT_URL}): {e}")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dojoindex-test-"))
        self.state = self.dir / ".index_state.dojo.json"
        self.coll = f"dojoindex_selftest_{os.getpid()}"
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        if client().collection_exists(self.coll):
            client().delete_collection(self.coll)

    def run_index(self, records, embed=ok_embed):
        calls = []

        def spy(texts):
            calls.append(texts)
            return embed(texts)

        with mock.patch.object(dojo_index, "embed_batch", spy):
            done = dojo_index.index(records, self.coll, 16, self.state)
        return done, calls

    def saved(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["files"]

    def test_bad_finding_skipped_and_retried(self):
        def embed(texts):
            if any("Находка 3" in t for t in texts):
                raise EmbedError("400")
            return ok_embed(texts)

        done, _ = self.run_index([record(i) for i in range(1, 6)], embed)
        self.assertGreater(done, 0)
        self.assertEqual(sorted(self.saved()), ["1", "2", "4", "5"])
        _, calls = self.run_index([record(i) for i in range(1, 6)])
        self.assertEqual(len(calls), 1)
        self.assertIn("Находка 3", calls[0][0])

    def test_embedder_down_stops_and_keeps_progress(self):
        def embed(texts):
            # первая находка проходит, дальше эмбеддер лежит
            if not all(t.startswith("1 · ") for t in texts):
                raise EmbedError("connection refused")
            return ok_embed(texts)

        with mock.patch.object(dojo_index, "CHECKPOINT_EVERY", 1000):
            done, calls = self.run_index([record(i) for i in range(1, 40)], embed)
        self.assertEqual(done, -1)
        self.assertEqual(len(calls) - 1, dojo_index.MAX_FAILS_IN_ROW)
        self.assertIn("1", self.saved())

    def test_checkpoint_during_run(self):
        # прогон «оборвался» на 8-й находке: до неё всё должно быть сохранено
        seen = {"n": 0}

        def embed(texts):
            seen["n"] += 1
            if seen["n"] == 8:
                raise KeyboardInterrupt
            return ok_embed(texts)

        with mock.patch.object(dojo_index, "CHECKPOINT_EVERY", 5):
            with self.assertRaises(KeyboardInterrupt):
                self.run_index([record(i) for i in range(1, 20)], embed)
        self.assertEqual(sorted(self.saved(), key=int), ["1", "2", "3", "4", "5"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
