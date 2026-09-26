"""code_search и cb_search: каждый ищет только в своей коллекции.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_code_search

Qdrant и эмбеддер подменены: проверяется, в какую коллекцию уходит запрос,
а не сам поиск. Код релиза CB18.5 не должен попадать в code_search и наоборот.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from kb import code_retriever, server
from kb.code_index import CB_COLLECTION, CODE_COLLECTION


class RetrieverCollectionTest(unittest.TestCase):
    def setUp(self):
        self.qdrant = mock.MagicMock()
        self.qdrant.query_points.return_value.points = []
        patches = [
            mock.patch.object(code_retriever, "client", return_value=self.qdrant),
            mock.patch.object(code_retriever, "embed", return_value=[0.0]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def collection_used(self) -> str:
        return self.qdrant.query_points.call_args.kwargs["collection_name"]

    def test_default_is_common_code(self):
        code_retriever.search("проверка токена")
        self.assertEqual(self.collection_used(), CODE_COLLECTION)

    def test_release_collection(self):
        code_retriever.search("проверка токена", collection=CB_COLLECTION)
        self.assertEqual(self.collection_used(), CB_COLLECTION)

    def test_available_and_repos_follow_collection(self):
        code_retriever.available(CB_COLLECTION)
        self.qdrant.collection_exists.assert_called_with(CB_COLLECTION)
        code_retriever.repos(CB_COLLECTION)
        self.assertEqual(
            self.qdrant.facet.call_args.kwargs["collection_name"], CB_COLLECTION
        )


class ToolCollectionTest(unittest.TestCase):
    """Инструменты сервера передают в поиск свою коллекцию."""

    def setUp(self):
        self.search = mock.MagicMock(return_value=[])
        patches = [
            mock.patch.object(server.code_retriever, "available", return_value=True),
            mock.patch.object(server.code_retriever, "search", self.search),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_cb_search_goes_to_release(self):
        out = server.cb_search("проверка лицензии")
        self.assertEqual(out["found"], 0)
        self.assertEqual(self.search.call_args.kwargs["collection"], CB_COLLECTION)

    def test_code_search_stays_on_common_code(self):
        server.code_search("проверка лицензии")
        self.assertEqual(self.search.call_args.kwargs["collection"], CODE_COLLECTION)

    def test_missing_release_index_says_how_to_build(self):
        with mock.patch.object(server.code_retriever, "available", return_value=False):
            out = server.cb_search("что угодно")
        self.assertIn(CB_COLLECTION, out["error"])
        self.assertIn("update-cb.sh", out["error"])
        self.search.assert_not_called()


class RegistrationTest(unittest.TestCase):
    def test_cb_search_is_read_only_tool(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        self.assertIn("cb_search", tools)
        self.assertTrue(tools["cb_search"].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
