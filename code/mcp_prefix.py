"""Прослойка MCP: те же инструменты Graphify, но под другими именами.

Зачем. Граф релиза CB18.5 — второй экземпляр Graphify, и его инструменты
называются так же, как у общего графа: get_neighbors, query_graph... Клиенты
MCP одинаковые имена от двух серверов путают — одни падают, другие молча
прячут один набор. Прослойка отдаёт их как cb_get_neighbors, cb_query_graph
и дописывает в описание, что это граф релиза, а вызовы пересылает обратно
под исходными именами.

Работает на уровне JSON-RPC поверх HTTP, только стандартная библиотека: от
версии Graphify и MCP SDK не зависит. Graphify должен быть запущен с
--stateless --json-response (так он запущен у нас); поток SSE тоже разбирается.

    python mcp_prefix.py --port 8013 --upstream http://127.0.0.1:8011/mcp \\
        --prefix cb_ --label "РЕЛИЗ CB18.5" --name cb-graph
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Заголовки, которые протокол передаёт между клиентом и сервером. Остальные
# (Host, Content-Length, Connection) у каждого соединения свои
PASS_REQUEST = ("Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-ID")
PASS_RESPONSE = ("Content-Type", "Mcp-Session-Id")
TIMEOUT = 300


class Renamer:
    def __init__(self, prefix: str, label: str, name: str):
        self.prefix = prefix
        self.label = label
        self.name = name

    def request(self, msg):
        """Клиент -> Graphify: cb_get_neighbors -> get_neighbors."""
        if isinstance(msg, list):
            return [self.request(m) for m in msg]
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name", "")
            if name.startswith(self.prefix):
                msg = {**msg, "params": {**params, "name": name[len(self.prefix):]}}
        return msg

    def response(self, msg):
        """Graphify -> клиент: имена с префиксом, в описании — чей это граф."""
        if isinstance(msg, list):
            return [self.response(m) for m in msg]
        if not isinstance(msg, dict) or not isinstance(msg.get("result"), dict):
            return msg
        result = dict(msg["result"])
        if isinstance(result.get("tools"), list):
            result["tools"] = [self.tool(t) for t in result["tools"]]
        info = result.get("serverInfo")
        if isinstance(info, dict) and self.name:
            result["serverInfo"] = {**info, "name": self.name}
        return {**msg, "result": result}

    def tool(self, tool):
        if not isinstance(tool, dict):
            return tool
        out = dict(tool)
        out["name"] = self.prefix + str(tool.get("name", ""))
        if self.label:
            out["description"] = (
                f"[{self.label}] Граф кода {self.label}, не общего кода. "
                + str(tool.get("description", ""))
            )
        return out

    def body(self, raw: bytes, content_type: str) -> bytes:
        """Переписать тело ответа: JSON целиком или каждое событие SSE."""
        if not raw:
            return raw
        if "application/json" in content_type:
            try:
                return json.dumps(self.response(json.loads(raw)), ensure_ascii=False).encode()
            except ValueError:
                return raw
        if "text/event-stream" in content_type:
            lines = []
            for line in raw.decode("utf-8", "replace").split("\n"):
                if line.startswith("data:"):
                    try:
                        data = json.loads(line[5:])
                        line = "data: " + json.dumps(self.response(data), ensure_ascii=False)
                    except ValueError:
                        pass
                lines.append(line)
            return "\n".join(lines).encode()
        return raw


def make_handler(upstream: str, renamer: Renamer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _forward(self, method: str):
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else None
            if data:
                try:
                    data = json.dumps(renamer.request(json.loads(data)), ensure_ascii=False).encode()
                except ValueError:
                    pass
            req = urllib.request.Request(upstream, data=data, method=method)
            for h in PASS_REQUEST:
                if self.headers.get(h):
                    req.add_header(h, self.headers[h])
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    status, headers, raw = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                status, headers, raw = e.code, e.headers, e.read()
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                self._reply(502, {"Content-Type": "application/json"}, json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32000, "message": (
                        f"Граф {renamer.label or ''} недоступен ({e}). Скорее всего, "
                        "он ещё не построен — ./update-cb.sh на сервере"
                    )},
                }, ensure_ascii=False).encode())
                return
            content_type = headers.get("Content-Type", "")
            out = {h: headers[h] for h in PASS_RESPONSE if headers.get(h)}
            self._reply(status, out, renamer.body(raw, content_type))

        def _reply(self, status: int, headers: dict, body: bytes):
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self._forward("POST")

        def do_GET(self):
            self._forward("GET")

        def do_DELETE(self):
            self._forward("DELETE")

        def log_message(self, format, *args):
            sys.stderr.write("mcp_prefix: " + (format % args) + "\n")

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP-прослойка: префикс к именам инструментов")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--upstream", default="http://127.0.0.1:8011/mcp")
    ap.add_argument("--prefix", default="cb_")
    ap.add_argument("--label", default="РЕЛИЗ CB18.5")
    ap.add_argument("--name", default="cb-graph")
    args = ap.parse_args()

    renamer = Renamer(args.prefix, args.label, args.name)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.upstream, renamer))
    print(f"mcp_prefix: :{args.port} -> {args.upstream}, префикс {args.prefix}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
