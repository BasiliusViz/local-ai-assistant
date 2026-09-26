"""Проверка mcp_prefix.py: имена с префиксом наружу, исходные — в Graphify.

    python3 code/test_mcp_prefix.py
    docker compose exec cb-graph python /app/test_mcp_prefix.py

Только стандартная библиотека. Graphify подменён маленьким HTTP-сервером,
который записывает, что к нему пришло.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mcp_prefix as mp  # noqa: E402

TOOLS = [
    {"name": "get_neighbors", "description": "Соседи узла", "inputSchema": {"type": "object"}},
    {"name": "query_graph", "description": "Обход графа", "inputSchema": {"type": "object"}},
]


class FakeGraphify(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list = []
    sse = False

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGraphify.received.append(msg)
        method = msg.get("method")
        if method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "вызван " + msg["params"]["name"]}]}
        elif method == "initialize":
            result = {"serverInfo": {"name": "graphify", "version": "1"}, "capabilities": {}}
        else:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        if FakeGraphify.sse:
            body, ctype = f"event: message\ndata: {reply}\n\n".encode(), "text/event-stream"
        else:
            body, ctype = reply.encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def serve(handler) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = serve(FakeGraphify)
        up = f"http://127.0.0.1:{cls.upstream.server_port}/mcp"
        handler = mp.make_handler(up, mp.Renamer("cb_", "РЕЛИЗ CB18.5", "cb-graph"))
        handler.log_message = lambda *a: None
        cls.proxy = serve(handler)
        cls.url = f"http://127.0.0.1:{cls.proxy.server_port}/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.upstream.shutdown()

    def setUp(self):
        FakeGraphify.received = []
        FakeGraphify.sse = False

    def call(self, method, params=None, url=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        req = urllib.request.Request(url or self.url, data=body.encode(), method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
        if raw.startswith("event:"):
            raw = next(l[5:] for l in raw.splitlines() if l.startswith("data:"))
        return json.loads(raw)

    def test_list_adds_prefix_and_label(self):
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["cb_get_neighbors", "cb_query_graph"])
        self.assertIn("РЕЛИЗ CB18.5", tools[0]["description"])
        self.assertIn("Соседи узла", tools[0]["description"])
        self.assertEqual(tools[0]["inputSchema"], {"type": "object"})

    def test_call_strips_prefix(self):
        out = self.call("tools/call", {"name": "cb_get_neighbors", "arguments": {"label": "X"}})
        self.assertEqual(FakeGraphify.received[-1]["params"]["name"], "get_neighbors")
        self.assertEqual(FakeGraphify.received[-1]["params"]["arguments"], {"label": "X"})
        self.assertEqual(out["result"]["content"][0]["text"], "вызван get_neighbors")

    def test_server_name(self):
        info = self.call("initialize")["result"]["serverInfo"]
        self.assertEqual(info["name"], "cb-graph")

    def test_sse_answer_is_rewritten_too(self):
        FakeGraphify.sse = True
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual(tools[0]["name"], "cb_get_neighbors")

    def test_notification_passes_through(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 202)

    def test_upstream_down_gives_clear_error(self):
        handler = mp.make_handler("http://127.0.0.1:9/mcp", mp.Renamer("cb_", "РЕЛИЗ CB18.5", ""))
        handler.log_message = lambda *a: None
        dead = serve(handler)
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.call("tools/list", url=f"http://127.0.0.1:{dead.server_port}/mcp")
            self.assertEqual(ctx.exception.code, 502)
            self.assertIn("update-cb.sh", ctx.exception.read().decode())
        finally:
            dead.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
