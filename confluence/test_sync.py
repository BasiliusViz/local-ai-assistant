"""Проверка выгрузки Confluence на заглушке — без настоящего Confluence.

    python confluence/test_sync.py
    docker compose exec kb python confluence/test_sync.py

Заглушка (mock_server.py) поднимается прямо в тесте на свободном порту:
два общих пространства (DEV, OPS), личное (~ivanov), вложенные страницы и
раздел «Архив» с поддеревом. Каждый тест пишет во временный каталог.
"""

from __future__ import annotations

import copy
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import mock_server  # noqa: E402
import sync  # noqa: E402

ORIGINAL_PAGES = copy.deepcopy(mock_server.PAGES)


class SyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mock_server.Handler.log_message = lambda *a, **k: None
        cls.server = HTTPServer(("127.0.0.1", 0), mock_server.Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        mock_server.PAGES[:] = copy.deepcopy(ORIGINAL_PAGES)
        mock_server.SPACES_FORBIDDEN = False
        mock_server.SEARCH_FORBIDDEN = False
        mock_server.NO_TOTAL_SIZE = False
        self.out = Path(tempfile.mkdtemp(prefix="confluence-test-"))

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def run_sync(self, *args, spaces="", pages="", exclude="", page_size="50"):
        env = {
            "CONFLUENCE_URL": self.url,
            "CONFLUENCE_TOKEN": mock_server.TOKEN,
            "CONFLUENCE_OUT": str(self.out),
            "CONFLUENCE_SPACES": spaces,
            "CONFLUENCE_PAGES": pages,
            "CONFLUENCE_EXCLUDE": exclude,
            "CONFLUENCE_PAGE_SIZE": page_size,
        }
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(
            sys, "argv", ["sync.py", *args]
        ), redirect_stdout(buf):
            code = sync.main()
        return code, buf.getvalue()

    def files(self) -> set[str]:
        return {
            str(p.relative_to(self.out)).replace("\\", "/")
            for p in self.out.rglob("*.md")
        }

    ALL_GLOBAL = {
        "DEV/Регламент код-ревью.md",
        "OPS/Регламент выкатки.md",
        "OPS/Дежурство.md",
        "OPS/Эскалация инцидентов.md",
        "OPS/Архив.md",
        "OPS/Старый регламент выкатки.md",
    }

    # --- что выгружать

    def test_spaces_by_key(self):
        code, out = self.run_sync(spaces="DEV,OPS")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), self.ALL_GLOBAL)
        self.assertIn("«Эксплуатация»", out)

    def test_all_spaces_skips_personal(self):
        code, out = self.run_sync(spaces="*")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), self.ALL_GLOBAL)
        self.assertNotIn("~ivanov", out)

    def test_pagination(self):
        # по одной странице за запрос — ничего не должно потеряться
        code, out = self.run_sync(spaces="*", page_size="1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), self.ALL_GLOBAL)

    def test_spaces_and_pages_together(self):
        code, out = self.run_sync(spaces="DEV", pages="1002")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.files(),
            {
                "DEV/Регламент код-ревью.md",
                "OPS/Регламент выкатки.md",
                "OPS/Дежурство.md",
                "OPS/Эскалация инцидентов.md",
            },
        )

    def test_overlap_counted_once(self):
        # 1002 лежит внутри OPS: страница не должна выгружаться дважды
        code, out = self.run_sync(spaces="OPS", pages="1002")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"новых\s+5\n")

    # --- исключения

    def test_exclude_page_with_subtree(self):
        code, out = self.run_sync(spaces="*", exclude="1005")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.files(),
            self.ALL_GLOBAL - {"OPS/Архив.md", "OPS/Старый регламент выкатки.md"},
        )

    def test_exclude_space(self):
        code, out = self.run_sync(spaces="*", exclude="OPS")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), {"DEV/Регламент код-ревью.md"})

    def test_exclude_added_later_removes_files(self):
        self.run_sync(spaces="*")
        code, out = self.run_sync(spaces="*", exclude="1005")
        self.assertEqual(code, 0, out)
        self.assertNotIn("OPS/Архив.md", self.files())
        self.assertRegex(out, r"удалено\s+2\n")

    # --- ошибки

    def test_all_spaces_when_listing_forbidden(self):
        mock_server.SPACES_FORBIDDEN = True
        code, out = self.run_sync(spaces="*")
        self.assertEqual(code, 1)
        self.assertIn("CONFLUENCE_SPACES=DEV,OPS", out)

    def test_listed_keys_work_when_listing_forbidden(self):
        # главное: закрытый список пространств не мешает выгрузке по ключам
        mock_server.SPACES_FORBIDDEN = True
        code, out = self.run_sync(spaces="DEV")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), {"DEV/Регламент код-ревью.md"})

    def test_unknown_space(self):
        code, out = self.run_sync(spaces="DEV,NOPE")
        self.assertEqual(code, 1)
        self.assertIn("Пространства «NOPE» нет", out)
        self.assertEqual(self.files(), set())

    def test_nothing_configured(self):
        code, out = self.run_sync()
        self.assertEqual(code, 2)
        self.assertIn("CONFLUENCE_SPACES", out)

    def test_check_counts_without_writing(self):
        code, out = self.run_sync("--check", spaces="DEV,OPS", exclude="1005")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"OPS\s+3")
        self.assertEqual(self.files(), set())

    # --- какие пространства есть (--list-spaces)

    def test_list_spaces(self):
        code, out = self.run_sync("--list-spaces")
        self.assertEqual(code, 0, out)
        self.assertIn("способ: список пространств", out)
        self.assertIn("CONFLUENCE_SPACES=DEV,OPS\n", out)
        self.assertIn("Эксплуатация", out)
        self.assertEqual(self.files(), set())

    def test_list_spaces_personal(self):
        code, out = self.run_sync("--list-spaces", "--personal")
        self.assertEqual(code, 0, out)
        self.assertIn("CONFLUENCE_SPACES=DEV,OPS,~ivanov", out)

    def test_list_spaces_count(self):
        code, out = self.run_sync("--list-spaces", "--count")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"OPS\s+5\s+Эксплуатация")
        self.assertRegex(out, r"6\s+всего")

    def test_count_one_request_per_space(self):
        # totalSize есть — перебирать страницы незачем: по запросу на пространство
        client = sync.Client(self.url, mock_server.TOKEN, page_size=1)
        with mock.patch.object(client, "_paged", side_effect=AssertionError("перебор")):
            self.assertEqual(client.count_pages("OPS"), 5)

    def test_count_without_total_size(self):
        # старый Confluence без totalSize — перебор, но число то же
        mock_server.NO_TOTAL_SIZE = True
        code, out = self.run_sync("--list-spaces", "--count", page_size="2")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"OPS\s+5\s+Эксплуатация")

    def test_list_spaces_when_listing_forbidden_uses_search(self):
        mock_server.SPACES_FORBIDDEN = True
        code, out = self.run_sync("--list-spaces")
        self.assertEqual(code, 0, out)
        self.assertIn("способ: поиск CQL", out)
        self.assertIn("CONFLUENCE_SPACES=DEV,OPS\n", out)

    def test_list_spaces_when_all_forbidden_scans_pages(self):
        mock_server.SPACES_FORBIDDEN = True
        mock_server.SEARCH_FORBIDDEN = True
        code, out = self.run_sync("--list-spaces")
        self.assertEqual(code, 0, out)
        self.assertIn("способ: обход страниц", out)
        self.assertIn("CONFLUENCE_SPACES=DEV,OPS\n", out)

    def test_list_spaces_needs_no_spaces_setting(self):
        # смотрят как раз затем, чтобы узнать, что вписать
        code, out = self.run_sync("--list-spaces", spaces="", pages="")
        self.assertEqual(code, 0, out)

    # --- инкрементальность

    def test_second_run_changes_nothing(self):
        self.run_sync(spaces="*")
        code, out = self.run_sync(spaces="*")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"без изменений\s+6\n")

    def test_renamed_page_leaves_no_old_file(self):
        self.run_sync(spaces="*")
        page = next(p for p in mock_server.PAGES if p["id"] == "1003")
        page["title"] = "Дежурство DevOps"  # версия та же — так в Confluence
        code, out = self.run_sync(spaces="*")
        self.assertEqual(code, 0, out)
        self.assertIn("OPS/Дежурство DevOps.md", self.files())
        self.assertNotIn("OPS/Дежурство.md", self.files())

    def test_moved_page_moves_file(self):
        self.run_sync(spaces="*")
        page = next(p for p in mock_server.PAGES if p["id"] == "1004")
        page["space"] = "DEV"
        page.pop("parent")
        code, out = self.run_sync(spaces="*")
        self.assertEqual(code, 0, out)
        self.assertIn("DEV/Эскалация инцидентов.md", self.files())
        self.assertNotIn("OPS/Эскалация инцидентов.md", self.files())

    def test_file_deleted_by_hand_comes_back(self):
        self.run_sync(spaces="DEV")
        (self.out / "DEV" / "Регламент код-ревью.md").unlink()
        self.run_sync(spaces="DEV")
        self.assertIn("DEV/Регламент код-ревью.md", self.files())


if __name__ == "__main__":
    unittest.main(verbosity=2)
