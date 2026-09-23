"""Доступ к DefectDojo только на чтение — проверка запрета в самом клиенте.

На сервере, после пересборки kb:
    docker compose exec kb python -m kb.test_dojo_readonly

DefectDojo не нужен: под клиент подставлен транспорт, который записывает,
что до него дошло. Изменяющий запрос не должен дойти вовсе.
"""

from __future__ import annotations

import unittest
import unittest.mock

import httpx

from kb import dojo


class ReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.sent = []

        def handler(request):
            self.sent.append(request.method)
            return httpx.Response(200, json={"count": 0, "results": []})

        with unittest.mock.patch.object(dojo, "URL", "https://dojo.test"):
            self.client = dojo._client()
        # тот же клиент, что в работе, со всеми его хуками — подменён только
        # последний шаг, отправка по сети
        self.client._transport = httpx.MockTransport(handler)

    def tearDown(self):
        self.client.close()

    def test_changes_blocked_before_sending(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaisesRegex(dojo.DojoError, "только на чтение"):
                self.client.request(method, "/findings/1/", json={"active": False})
        self.assertEqual(self.sent, [])

    def test_reading_allowed(self):
        self.assertEqual(dojo._get(self.client, "/findings/", limit=1)["count"], 0)
        self.assertEqual(self.sent, ["GET"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
