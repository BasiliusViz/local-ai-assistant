"""Проверка веток DefectDojo (engagement'ов): выбор, одна ветка, сравнение.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_dojo_compare

Ничего живого не нужно: API DefectDojo подменён словарём в памяти.
"""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest import mock

from kb import dojo, dojo_compare

PRODUCT = {"id": 1, "name": "ABInf Backend"}

ENGAGEMENTS = [
    {"id": 10, "name": "main_abinf", "status": "In Progress", "target_start": "2026-09-01"},
    {"id": 11, "name": "feature-x_abinf", "status": "In Progress", "target_start": "2026-09-10"},
    {"id": 12, "name": "main-old_abinf", "status": "Completed", "target_start": "2025-01-01"},
    {"id": 13, "name": "fix-a_abinf", "status": "In Progress", "target_start": "2026-09-05"},
    {"id": 14, "name": "fix-b_abinf", "status": "In Progress", "target_start": "2026-09-06"},
]


def f(fid, eng, sev, hash_code=None, **extra):
    base = {"id": fid, "engagement": eng, "severity": sev, "title": f"Находка {fid}",
            "hash_code": hash_code, "active": True, "duplicate": False,
            "false_p": False, "risk_accepted": False, "is_mitigated": False}
    base.update(extra)
    return base


FINDINGS = [
    # main
    f(1, 10, "High", "h1"),
    f(2, 10, "Critical", "h2"),
    f(3, 10, "Medium", None, title="XSS", cwe=79, file_path="app/a.py", line=10),
    f(4, 10, "Low", "h4", active=False, false_p=True),
    # feature-x
    # та же уязвимость, что 1: DefectDojo пометил дубликатом и выключил
    f(11, 11, "High", "h1", active=False, duplicate=True),
    f(12, 11, "Critical", "h5"),  # новая
    # та же XSS без hash_code, код сдвинулся на 4 строки
    f(13, 11, "Medium", None, title="XSS", cwe=79, file_path="app/a.py", line=14),
    f(14, 11, "Critical", "h2", active=False, is_mitigated=True),  # 2 исправлена в ветке
    # второй сканер нашёл то же самое — не должно считаться дважды
    f(15, 11, "Critical", "h5"),
]


def fake_get(client, path, **params):
    offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100))
    if path == "/engagements/":
        items = [e for e in ENGAGEMENTS if params.get("product") == PRODUCT["id"]]
    elif path == "/findings/":
        items = [x for x in FINDINGS if x["engagement"] == params.get("test__engagement")]
    else:
        raise AssertionError(path)
    return {"count": len(items), "results": items[offset : offset + limit]}


class CompareTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(dojo, "_get", fake_get),
            mock.patch.object(dojo, "_client", lambda: nullcontext(object())),
            mock.patch.object(dojo, "resolve_product", lambda c, name: PRODUCT),
            mock.patch.object(dojo, "configured", lambda: True),
            # по две записи на страницу — постраничная выдача обязана сработать
            mock.patch.object(dojo, "PAGE_SIZE", 2),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def ids(group):
        return sorted(x["id"] for x in group["findings"])

    # --- выбор ветки

    def test_resolve_prefers_whole_branch(self):
        pick = dojo_compare.resolve_engagement
        self.assertEqual(pick("main", ENGAGEMENTS)["id"], 10)
        self.assertEqual(pick("main-old", ENGAGEMENTS)["id"], 12)
        self.assertEqual(pick("feature", ENGAGEMENTS)["id"], 11)
        self.assertEqual(pick("MAIN_ABINF", ENGAGEMENTS)["id"], 10)

    def test_resolve_ambiguous_and_missing(self):
        with self.assertRaisesRegex(dojo.DojoError, "fix-a_abinf, fix-b_abinf"):
            dojo_compare.resolve_engagement("fix", ENGAGEMENTS)
        with self.assertRaisesRegex(dojo.DojoError, "Есть: main_abinf"):
            dojo_compare.resolve_engagement("release", ENGAGEMENTS)

    # --- сравнение

    def test_compare_groups(self):
        r = dojo_compare.compare("abinf", "main", "feature-x", "open", None, 25)
        self.assertEqual((r["first"], r["second"]), ("main_abinf", "feature-x_abinf"))
        self.assertEqual(self.ids(r["only_in_second"]), [12])  # новая, одна на два сканера
        self.assertEqual(self.ids(r["only_in_first"]), [2])  # исправлена в ветке
        self.assertEqual(self.ids(r["in_both"]), [11, 13])  # дубликат и сдвинутая XSS
        self.assertEqual(r["only_in_second"]["summary"]["Critical"], 1)
        self.assertEqual(r["in_both"]["total"], 2)

    def test_duplicate_counts_as_open(self):
        [card] = [x for x in dojo_compare.compare(
            "abinf", "main", "feature-x", "open", None, 25)["in_both"]["findings"] if x["id"] == 11]
        self.assertEqual(card["status"], "open")

    def test_compare_severity_filter(self):
        r = dojo_compare.compare("abinf", "main", "feature-x", "open", "Critical", 25)
        self.assertEqual(self.ids(r["only_in_second"]), [12])
        self.assertEqual(self.ids(r["only_in_first"]), [2])
        self.assertEqual(r["in_both"]["total"], 0)

    def test_compare_all_statuses(self):
        r = dojo_compare.compare("abinf", "main", "feature-x", None, None, 25)
        # 2 в ветке исправлена, но присутствует — теперь общая
        self.assertIn(14, self.ids(r["in_both"]))
        self.assertEqual(self.ids(r["only_in_first"]), [4])

    def test_same_engagement_twice(self):
        with self.assertRaisesRegex(dojo.DojoError, "один engagement"):
            dojo_compare.compare("abinf", "main", "main_abinf", "open", None, 25)

    # --- одна ветка и список

    def test_one_branch(self):
        r = dojo_compare.one("abinf", "feature-x", "open", None, 25)
        self.assertEqual(r["engagement"], "feature-x_abinf")
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["findings"][0]["severity"], "Critical")

    def test_list_newest_first(self):
        r = dojo_compare.list_engagements("abinf")
        self.assertEqual(r["engagements"][0]["name"], "feature-x_abinf")
        self.assertEqual(len(r["engagements"]), 5)

    # --- через инструмент, как вызовет модель

    def test_tool_compare(self):
        from kb import dojo_server

        out = dojo_server.dojo_compare(product="abinf", base="main", target="feature-x")
        self.assertNotIn("error", out)
        self.assertEqual(self.ids(out["new_in_target"]), [12])
        self.assertEqual(self.ids(out["fixed_in_target"]), [2])
        self.assertEqual(self.ids(out["still_open"]), [11, 13])
        self.assertIn("fixed_in_target", out["citation_instruction"])

    def test_tool_compare_severity_in_words(self):
        from kb import dojo_server

        out = dojo_server.dojo_compare(
            product="abinf", base="main", target="feature-x", severity="высокие и средние"
        )
        self.assertEqual(out["new_in_target"]["total"], 0)  # новая — критичная
        self.assertEqual(self.ids(out["still_open"]), [11, 13])

    def test_tool_null_severity_is_no_filter(self):
        # qwen3 на замере передала severity="null" строкой
        from kb import dojo_server

        for blank in ("null", "None", "", "все"):
            out = dojo_server.dojo_compare(
                product="abinf", base="main", target="feature-x", severity=blank
            )
            self.assertNotIn("error", out, blank)
            self.assertEqual(self.ids(out["new_in_target"]), [12], blank)

    def test_tool_list(self):
        from kb import dojo_server

        out = dojo_server.dojo_engagements(product="abinf")
        self.assertEqual(len(out["engagements"]), 5)

    def test_tool_needs_product(self):
        from kb import dojo_server

        out = dojo_server.dojo_compare(product="", base="main", target="feature-x")
        self.assertIn("Нужен продукт", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
