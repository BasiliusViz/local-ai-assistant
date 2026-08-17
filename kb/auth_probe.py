"""Проверка, что ключ доступа к Ollama действительно уходит в запросе.

Поднимает подставной сервер, который отвечает только при верном заголовке, и
прогоняет через него настоящий код эмбеддера. Так проверяется не «переменная
прочиталась», а что заголовок доехал до сети.

    python -m kb.auth_probe
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8099
TOKEN = "secret-token-123"
seen_headers: dict[str, str] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        seen_headers.clear()
        seen_headers.update({k.lower(): v for k, v in self.headers.items()})

        auth = self.headers.get("Authorization") or self.headers.get("x-api-key")
        if auth not in (f"Bearer {TOKEN}", TOKEN):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        count = len(payload.get("input", []))
        body = json.dumps(
            {"data": [{"embedding": [0.1] * 1024} for _ in range(count)]}
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["KB_OLLAMA_URL"] = f"http://127.0.0.1:{PORT}/v1"

    def reload_modules():
        import importlib
        from kb import config, embedder
        importlib.reload(config)
        importlib.reload(embedder)
        return embedder

    print("1. Без ключа — сервер должен отказать")
    os.environ["KB_OLLAMA_API_KEY"] = ""
    embedder = reload_modules()
    try:
        embedder.embed("проверка")
        print("   ПЛОХО: запрос прошёл без ключа")
    except embedder.EmbedError as e:
        print(f"   OK: {str(e)[:70]}")

    print("\n2. С ключом в Authorization: Bearer")
    os.environ["KB_OLLAMA_API_KEY"] = TOKEN
    embedder = reload_modules()
    vector = embedder.embed("проверка")
    print(f"   OK: вектор длиной {len(vector)}")
    print(f"   заголовок: {seen_headers.get('authorization')}")

    print("\n3. С ключом в x-api-key (другой формат шлюза)")
    os.environ["KB_OLLAMA_AUTH_HEADER"] = "x-api-key"
    os.environ["KB_OLLAMA_AUTH_PREFIX"] = ""
    embedder = reload_modules()
    vector = embedder.embed("проверка")
    print(f"   OK: вектор длиной {len(vector)}")
    print(f"   заголовок: {seen_headers.get('x-api-key')}")

    server.shutdown()


if __name__ == "__main__":
    main()
