"""Проверка поиска по находкам DefectDojo на синтетике.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_dojo_retriever

Нужен только Qdrant: тест заводит временную коллекцию, кладёт в неё
выдуманные находки трёх продуктов и в конце её удаляет. Рабочую коллекцию
не трогает. Эмбеддер подменён — Ollama и сеть не нужны.
"""

from __future__ import annotations

import logging
import os
import unittest
import uuid
from unittest import mock

from qdrant_client import models

from kb import config, dojo_retriever
from kb.dojo_index import ensure_dojo_indexes
from kb.retriever import client

COLLECTION = f"dojo_selftest_{os.getpid()}"

# Смысл находки задаётся направлением вектора: так косинусы предсказуемы
TOPICS = {
    "log4j": [1.0, 0.0, 0.0, 0.0],
    "инъекц": [0.0, 1.0, 0.0, 0.0],
    "борщ": [0.0, 0.0, 1.0, 0.0],
}
OTHER = [0.0, 0.0, 0.0, 1.0]


def fake_embed(texts: list[str]) -> list[list[float]]:
    out = []
    for text in texts:
        low = text.casefold()
        out.append(next((v for k, v in TOPICS.items() if k in low), OTHER))
    return out


def finding(fid, product, severity, status="open", topic=None, found="2026-01-01", chunks=1):
    """Точки одной находки: карточка (chunk_idx=0) и, если надо, продолжения."""
    vector = TOPICS[topic] if topic else OTHER
    points = []
    for idx in range(chunks):
        points.append(
            models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"dojo|{fid}|{idx}")),
                vector={config.DENSE_VECTOR: vector},
                payload={
                    "source": "dojo",
                    "finding_id": str(fid),
                    "product": product,
                    "severity": severity,
                    "finding_status": status,
                    "title": f"{product} · Находка {fid}",
                    "url": f"https://dojo.local/finding/{fid}",
                    "found_at": found,
                    "chunk_idx": idx,
                    "text": f"находка {fid} {topic or ''}",
                    "description": f"описание {fid}" if idx == 0 else "",
                    "mitigation": f"как чинить {fid}" if idx == 0 else "",
                },
            )
        )
    return points


def corpus():
    pts = []
    # ABInf: больше всех критичных — в обзоре должен быть первым
    pts += finding(1, "ABInf Backend", "Critical", found="2026-03-01", chunks=3)
    pts += finding(2, "ABInf Backend", "Critical", found="2025-11-01", topic="инъекц")
    pts += finding(3, "ABInf Backend", "Critical", found="2026-02-01")
    pts += finding(4, "ABInf Backend", "High")
    pts += finding(5, "ABInf Backend", "High", chunks=2)
    pts += finding(6, "ABInf Backend", "Low", status="accepted")
    # Billing: одна критичная и полторы сотни средних. Критичная заведена
    # ПОСЛЕДНЕЙ — старая выборка «первая сотня и сортировка» её теряла
    for i in range(150):
        pts += finding(1000 + i, "Billing API", "Medium")
    pts += finding(9021, "Billing API", "Critical", topic="log4j")
    # Legacy: всё закрыто — открытых нет, но в списке продуктов он есть
    pts += finding(500, "Legacy Portal", "High", status="fixed")
    return pts


class DojoRetrieverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # каждый запрос к Qdrant иначе печатается строкой в лог
        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            client().get_collections()
        except Exception as e:
            raise unittest.SkipTest(f"Qdrant недоступен ({config.QDRANT_URL}): {e}")
        cls.saved = config.COLLECTION
        config.COLLECTION = COLLECTION
        client().create_collection(
            COLLECTION,
            vectors_config={
                config.DENSE_VECTOR: models.VectorParams(
                    size=4, distance=models.Distance.COSINE
                )
            },
        )
        client().create_payload_index(
            COLLECTION, "source", field_schema=models.PayloadSchemaType.KEYWORD
        )
        ensure_dojo_indexes(client(), COLLECTION)
        client().upsert(COLLECTION, points=corpus(), wait=True)
        cls.patch = mock.patch.object(dojo_retriever, "embed_batch", fake_embed)
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        config.COLLECTION = cls.saved
        client().delete_collection(COLLECTION)

    # --- что считается «по всем продуктам»

    def test_wants_all(self):
        for value in (None, "", "  ", "все", "Все продукты", "all", "*", "всё сразу"):
            self.assertTrue(dojo_retriever.wants_all(value), value)
        for value in ("abinf", "Billing", "весы"):
            self.assertFalse(dojo_retriever.wants_all(value), value)

    # --- обзор без продукта

    def test_overview_summary_counts_open_findings_not_chunks(self):
        r = dojo_retriever.search()
        self.assertEqual(r["product"], "все продукты")
        # 3 + 1 критичных; у находки 1 три чанка, считается она одна
        self.assertEqual(r["summary"]["Critical"], 4)
        self.assertEqual(r["summary"]["High"], 2)
        self.assertEqual(r["summary"]["Medium"], 150)
        # принятая Low и закрытая High в открытые не входят
        self.assertEqual(r["summary"]["Low"], 0)

    def test_overview_by_product_sorted_by_severity(self):
        rows = dojo_retriever.search()["by_product"]
        self.assertEqual(
            [row["product"] for row in rows],
            ["ABInf Backend", "Billing API", "Legacy Portal"],
        )
        abinf = rows[0]
        self.assertEqual((abinf["Critical"], abinf["High"], abinf["total"]), (3, 2, 5))
        # продукт без открытых находок всё равно в списке — «какие продукты есть»
        self.assertEqual(rows[2]["total"], 0)

    def test_overview_worst_first_across_products(self):
        hits = dojo_retriever.search(limit=5)["hits"]
        self.assertEqual([h.severity for h in hits[:4]], ["Critical"] * 4)
        self.assertIn("Billing API", {h.product for h in hits[:4]})
        self.assertEqual(hits[4].severity, "High")
        # внутри уровня — давние первыми
        abinf_critical = [h.finding_id for h in hits if h.product == "ABInf Backend"]
        self.assertEqual(abinf_critical[:3], ["2", "3", "1"])

    def test_overview_severity_filter(self):
        r = dojo_retriever.search(severity="высокие")
        self.assertEqual({h.severity for h in r["hits"]}, {"High"})
        self.assertEqual(len(r["hits"]), 2)

    def test_status_all_includes_accepted_and_fixed(self):
        r = dojo_retriever.search(status="all", limit=500)
        statuses = {h.status for h in r["hits"]}
        self.assertEqual(statuses, {"open", "accepted", "fixed"})
        legacy = next(row for row in r["by_product"] if row["product"] == "Legacy Portal")
        self.assertEqual(legacy["High"], 1)

    # --- один продукт

    def test_product_worst_first_beyond_first_hundred(self):
        # регрессия: критичная — 151-я по порядку, раньше терялась
        r = dojo_retriever.search(product="billing", limit=3)
        self.assertEqual(r["product"], "Billing API")
        self.assertEqual(r["hits"][0].finding_id, "9021")
        self.assertNotIn("by_product", r)

    def test_product_partial_name(self):
        self.assertEqual(dojo_retriever.search(product="abinf")["product"], "ABInf Backend")

    def test_unknown_product_lists_known(self):
        with self.assertRaises(dojo_retriever.DojoSearchError) as ctx:
            dojo_retriever.search(product="payments")
        self.assertIn("ABInf Backend", str(ctx.exception))

    # --- тема

    def test_topic_across_products(self):
        r = dojo_retriever.search(query="где у нас log4j")
        self.assertEqual([h.finding_id for h in r["hits"]], ["9021"])
        self.assertEqual(r["hits"][0].product, "Billing API")
        self.assertIn("threshold", r["applied_filters"])

    def test_unrelated_topic_across_products_is_empty(self):
        # по всем продуктам без порога вернулись бы «ближайшие» находки
        self.assertEqual(dojo_retriever.search(query="рецепт борща")["hits"], [])

    def test_topic_inside_product_has_no_threshold(self):
        r = dojo_retriever.search(product="abinf", query="рецепт борща", limit=3)
        self.assertTrue(r["hits"])
        self.assertNotIn("threshold", r["applied_filters"])

    def test_topic_with_severity(self):
        r = dojo_retriever.search(query="инъекции", severity="критичные")
        self.assertEqual([h.finding_id for h in r["hits"]], ["2"])

    # --- ответ инструмента

    def test_tool_output_all_products(self):
        from kb import dojo_server

        out = dojo_server.dojo_findings()
        self.assertNotIn("error", out)
        self.assertIn("by_product", out)
        self.assertTrue(all("product" in f for f in out["findings"]))
        self.assertIn("by_product", out["citation_instruction"])

    def test_tool_output_one_product(self):
        from kb import dojo_server

        out = dojo_server.dojo_findings(product="abinf")
        self.assertNotIn("by_product", out)
        self.assertNotIn("product", out["findings"][0])

    def test_report_enriches_cards_across_products(self):
        from kb import dojo_server

        out = dojo_server.dojo_findings(severity="критичные", response_format="report")
        self.assertTrue(all(f["mitigation"].startswith("как чинить") for f in out["findings"]))

    # --- в индексе только часть уровней (DOJO_INDEX_SEVERITIES)

    def test_summary_only_indexed_levels(self):
        from kb import dojo_server

        with mock.patch.dict(os.environ, {"DOJO_INDEX_SEVERITIES": "Critical,High"}):
            out = dojo_server.dojo_findings()
        # «средних: 0» было бы неправдой — их просто не индексировали
        self.assertEqual(list(out["summary"]), ["Critical", "High"])
        self.assertEqual(out["indexed_levels"], ["Critical", "High"])
        self.assertIn("только уровни", out["citation_instruction"])

    # --- продукта нет в индексе, но он есть в DefectDojo

    def live(self, name_or_error):
        from contextlib import nullcontext

        def resolve(client, name):
            if isinstance(name_or_error, Exception):
                raise name_or_error
            return {"id": 7, "name": name_or_error}

        return [
            mock.patch("kb.dojo.configured", lambda: True),
            mock.patch("kb.dojo._client", lambda: nullcontext(object())),
            mock.patch("kb.dojo.resolve_product", resolve),
        ]

    def test_product_only_in_dojo_is_not_missing(self):
        from kb import dojo_server

        patches = self.live("Payments Gateway")
        for p in patches:
            p.start()
        try:
            out = dojo_server.dojo_findings(product="payments")
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("error", out)
        self.assertEqual(out["product"], "Payments Gateway")
        self.assertEqual(out["found"], 0)
        self.assertIn("есть в DefectDojo", out["note"])
        self.assertIn("Не говори, что продукта нет", out["citation_instruction"])

    def test_product_missing_everywhere(self):
        from kb import dojo, dojo_server

        patches = self.live(dojo.DojoError("Продукта «payments» нет среди доступных."))
        for p in patches:
            p.start()
        try:
            out = dojo_server.dojo_findings(product="payments")
        finally:
            for p in patches:
                p.stop()
        self.assertIn("среди находок нет", out["error"])

    def test_not_indexed_level_explained(self):
        from kb import dojo_server

        with mock.patch.dict(os.environ, {"DOJO_INDEX_SEVERITIES": "Critical,High"}):
            out = dojo_server.dojo_findings(severity="средние")
        self.assertIn("не индексируется", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
