"""Прокси к Ollama: дописывает ключ доступа в каждый запрос.

Зачем. Инструменты, которые ходят в Ollama напрямую (OASIS и подобные),
умеют указать адрес, но не умеют передать ключ — а наш шлюз без заголовка
`x-api-key` отвечает 403. Прокси встаёт между ними: слушает локально,
подставляет ключ из `.env` и пересылает всё как есть.

Инструменту говорим `--ollama-url http://localhost:11435`, и он работает
так, будто никакого шлюза нет.

Ответы передаются ПОТОКОМ, а не целиком: Ollama отдаёт генерацию по мере
готовности, и если её буферизовать, клиент будет ждать молча до самого
конца, а на длинных ответах ещё и упрётся в таймаут.

Слушает только localhost и намеренно: внутри лежит рабочий ключ, и
выставлять такое в сеть нельзя — получится дыра в обход шлюза.

    python ollama_proxy.py                  порт 11435, адрес из .env
    python ollama_proxy.py --port 11500
    python ollama_proxy.py --upstream http://gate.company.local:11434
"""

import argparse
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from kb import config

log = logging.getLogger("proxy")

# Заголовки, которые нельзя пересылать: они описывают само соединение, а не
# запрос, и при передаче ломают ответ — особенно Content-Length, который
# после потоковой передачи перестаёт совпадать
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

UPSTREAM = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - подпись задана базовым классом
        """Свой формат: стандартный печатает адрес клиента, а он всегда один."""
        log.info("%s", fmt % args)

    def _relay(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
        }
        headers.update(config.auth_headers())

        url = f"{UPSTREAM}{self.path}"
        try:
            upstream = requests.request(
                method,
                url,
                data=body,
                headers=headers,
                stream=True,
                timeout=600,
            )
        except requests.RequestException as e:
            log.error("не дошло до %s: %s", url, e)
            self.send_error(502, "upstream unreachable")
            return

        self.send_response(upstream.status_code)
        for key, value in upstream.headers.items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        # Длину не знаем заранее, поэтому отдаём чанками — так же, как отдаёт
        # сама Ollama при потоковой генерации
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                self.wfile.write(f"{len(chunk):X}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # Клиент ушёл, не дождавшись, — обычное дело, если прервали прогон
            log.debug("клиент закрыл соединение на %s", self.path)

    def do_GET(self):  # noqa: N802 - имена задаёт базовый класс
        self._relay("GET")

    def do_POST(self):  # noqa: N802
        self._relay("POST")

    def do_DELETE(self):  # noqa: N802
        self._relay("DELETE")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    ap = argparse.ArgumentParser(description="Прокси к Ollama с подстановкой ключа")
    ap.add_argument("--port", type=int, default=11435)
    ap.add_argument(
        "--bind",
        default="127.0.0.1",
        help="намеренно только localhost: внутри рабочий ключ",
    )
    ap.add_argument(
        "--upstream",
        help="адрес шлюза; по умолчанию берётся из .env (OLLAMA_URL)",
    )
    args = ap.parse_args()

    global UPSTREAM
    base = args.upstream or config.OLLAMA_URL
    base = base.rstrip("/")
    # Пути вида /api/tags живут в корне, поэтому /v1 с конца убираем:
    # клиент сам добавит нужный путь целиком
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    UPSTREAM = base

    key = config.auth_headers()
    if not key:
        print("В .env нет ключа (OLLAMA_API_KEY) — прокси не нужен,")
        print("указывайте инструменту адрес шлюза напрямую.")
        return 2

    # Заголовки HTTP передаются в latin-1, и кириллица в ключе роняет запрос
    # где-то в недрах http.client — по сообщению не догадаешься, что дело в
    # значении из .env. Проверяем на старте, пока это ещё понятно
    for name, value in key.items():
        try:
            f"{name}: {value}".encode("latin-1")
        except UnicodeEncodeError:
            print(f"В значении {name} есть символы вне латиницы.")
            print("Заголовки HTTP такое не передают — проверьте OLLAMA_API_KEY")
            print("в .env: скорее всего туда попал лишний символ или кавычки.")
            return 2

    print(f"Слушаю   http://{args.bind}:{args.port}")
    print(f"Пересылаю в {UPSTREAM}, добавляя заголовок {list(key)[0]}")
    print("Инструменту укажите этот адрес вместо адреса Ollama. Остановить: Ctrl+C")

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
