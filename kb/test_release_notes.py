"""Проверка release notes по устранённым уязвимостям.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_release_notes

API DefectDojo подменён — те же данные, что в kb.test_dojo_compare:
main -> feature-x: устранена 2 (Critical), новая 12 (Critical), в обеих 11 и 13.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock

from kb import dojo, release_notes
from kb.test_dojo_compare import PRODUCT, fake_get


class ReleaseNotesTest(unittest.TestCase):
    def setUp(self):
        for p in (
            mock.patch.object(dojo, "_get", fake_get),
            mock.patch.object(dojo, "_client", lambda: nullcontext(object())),
            mock.patch.object(dojo, "resolve_product", lambda c, name: PRODUCT),
            mock.patch.object(dojo, "configured", lambda: True),
            mock.patch.object(dojo, "URL", "https://dojo.local"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.dir = Path(tempfile.mkdtemp())

    def run_cli(self, *args):
        out = self.dir / "notes.md"
        argv = ["release_notes", "--product", "abinf", "--from", "main",
                "--to", "feature-x", "--out", str(out), *args]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            code = release_notes.main()
        return code, out.read_text(encoding="utf-8") if out.exists() else ""

    def test_fixed_listed(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("# Устранённые уязвимости: ABInf Backend, feature-x_abinf", text)
        self.assertIn("**Устранено: 1 уязвимость** (критичные — 1).", text)
        self.assertIn("([#2](https://dojo.local/finding/2))", text)
        # новые без флага не показываются
        self.assertNotIn("#12]", text)
        self.assertIn("2 (высокие — 1, средние — 1)", text)

    def test_with_new(self):
        _, text = self.run_cli("--with-new")
        self.assertIn("## Новые в feature-x_abinf", text)
        self.assertIn("#12]", text)

    def test_severity_filter(self):
        _, text = self.run_cli("--severity", "high")
        self.assertIn("**Устранено: 0 уязвимостей**.", text)
        self.assertIn("Учтены уровни: высокие.", text)

    def test_details_in_line(self):
        _, text = self.run_cli("--with-new", "--severity", "medium")
        self.assertNotIn("XSS", text.split("## Остаются")[0])  # XSS в обеих, не устранена

    def test_unknown_engagement(self):
        argv = ["release_notes", "--product", "abinf", "--from", "release", "--to", "main",
                "--out", str(self.dir / "x.md")]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(buf):
            code = release_notes.main()
        self.assertEqual(code, 1)
        self.assertIn("Есть: main_abinf", buf.getvalue())

    # --- в чате, через инструмент

    def test_tool_release_notes(self):
        from kb import dojo_server

        out = dojo_server.dojo_findings(
            product="abinf", engagement="main", compare_with="feature-x",
            response_format="release_notes",
        )
        self.assertNotIn("error", out)
        self.assertIn("**Устранено: 1 уязвимость**", out["release_notes"])
        self.assertIn("ДОСЛОВНО", out["citation_instruction"])

    def test_tool_release_notes_levels_in_words(self):
        from kb import dojo_server

        out = dojo_server.dojo_findings(
            product="abinf", engagement="main", compare_with="feature-x",
            response_format="release_notes", severity="критичные и высокие",
        )
        self.assertIn("Учтены уровни: критичные, высокие.", out["release_notes"])

    def test_tool_release_notes_needs_two(self):
        from kb import dojo_server

        out = dojo_server.dojo_findings(
            product="abinf", engagement="main", response_format="release_notes"
        )
        self.assertIn("два engagement", out["error"])

    def test_chat_list_capped(self):
        from kb import dojo_compare

        result = dojo_compare.compare("abinf", "main", "feature-x", "open", None, 1000)
        text = release_notes.render(result, [], True, max_items=0)
        self.assertIn("_Показано 0 из 1.", text)

    def test_plural(self):
        words = ("уязвимость", "уязвимости", "уязвимостей")
        got = [release_notes.plural(n, *words) for n in (1, 2, 5, 11, 21, 22, 25, 112)]
        self.assertEqual(got, ["уязвимость", "уязвимости", "уязвимостей", "уязвимостей",
                               "уязвимость", "уязвимости", "уязвимостей", "уязвимостей"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
