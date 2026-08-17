"""Заглушка Confluence Server REST API для проверки коннектора без Confluence.

Отдаёт /rest/api/content и /rest/api/space в том же формате, что настоящий
Confluence Server/Data Center: storage-формат (XHTML), пагинация через
start/limit, макросы, таблицы, вложенные списки.

Страницы намеренно содержат то, на чём конвертеры обычно ломаются:
таблицы, code-макросы, info-панели, ссылки на другие страницы, вложения.

Запуск:
    python confluence/mock_server.py        # http://localhost:8090
"""

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8090
TOKEN = "test-token"

SPACES = [
    {"key": "DEV", "name": "Разработка"},
    {"key": "OPS", "name": "Эксплуатация"},
]

# storage-формат Confluence: XHTML со своими тегами ac: и ri:
PAGES = [
    {
        "id": "1001",
        "space": "DEV",
        "title": "Регламент код-ревью",
        "updated": "2026-07-01T10:00:00.000+03:00",
        "body": """<h2>Требования к merge request</h2>
<p>Каждый merge request требует <strong>двух аппрувов</strong>, один из них
&mdash; от владельца компонента.</p>
<ac:structured-macro ac:name="info">
  <ac:rich-text-body><p>Красный пайплайн не повод для аппрува.</p></ac:rich-text-body>
</ac:structured-macro>
<h2>Именование веток</h2>
<table>
  <tbody>
    <tr><th>Тип</th><th>Формат</th></tr>
    <tr><td>Фича</td><td>feature/JIRA-123-short</td></tr>
    <tr><td>Багфикс</td><td>fix/JIRA-456-short</td></tr>
  </tbody>
</table>
<ac:structured-macro ac:name="code">
  <ac:parameter ac:name="language">bash</ac:parameter>
  <ac:plain-text-body><![CDATA[git checkout -b feature/JIRA-123-short]]></ac:plain-text-body>
</ac:structured-macro>
<p>Подробнее см. <ac:link><ri:page ri:content-title="Регламент выкатки"/></ac:link>.</p>""",
    },
    {
        "id": "1002",
        "space": "OPS",
        "title": "Регламент выкатки",
        "updated": "2026-07-15T12:30:00.000+03:00",
        "body": """<h2>Процедура</h2>
<p>Выкатка идёт в три этапа:</p>
<ol>
  <li>canary &mdash; один под из десяти, наблюдение 15 минут
    <ul><li>метрики: error rate, p99 latency</li>
        <li>при росте ошибок выше 0.5% откат автоматический</li></ul>
  </li>
  <li>50% трафика, наблюдение 30 минут</li>
  <li>полная выкатка</li>
</ol>
<h2>Откат</h2>
<ac:structured-macro ac:name="code">
  <ac:plain-text-body><![CDATA[kubectl rollout undo deployment/payment-gateway]]></ac:plain-text-body>
</ac:structured-macro>
<p>Если проблема в миграции базы &mdash; сначала откатывается схема, потом деплой.</p>
<ac:image><ri:attachment ri:filename="rollback-scheme.png"/></ac:image>""",
    },
    {
        "id": "1003",
        "space": "OPS",
        "title": "Дежурство",
        "updated": "2026-08-01T09:00:00.000+03:00",
        # Вложена в «Регламент выкатки»: проверяем выгрузку поддерева
        "parent": "1002",
        "body": """<p>Дежурная смена DevOps: канал <code>#devops-duty</code>.</p>
<p>Время реакции на инцидент P1 &mdash; 15 минут в рабочее время,
30 минут ночью.</p>""",
    },
    {
        "id": "1004",
        "space": "OPS",
        "title": "Эскалация инцидентов",
        "updated": "2026-08-02T11:00:00.000+03:00",
        # Второй уровень вложенности: под «Дежурством»
        "parent": "1003",
        "body": """<h2>Порядок эскалации</h2>
<ol>
  <li>дежурный DevOps &mdash; 15 минут</li>
  <li>владелец сервиса &mdash; ещё 15 минут</li>
  <li>руководитель направления</li>
</ol>""",
    },
]


def descendants_of(root_id: str) -> list[dict]:
    """Все потомки на любой глубине — как CQL ancestor в настоящем Confluence."""
    out, frontier = [], [root_id]
    while frontier:
        current = frontier.pop()
        for page in PAGES:
            if page.get("parent") == current:
                out.append(page)
                frontier.append(page["id"])
    return out


def page_json(page: dict, with_body: bool) -> dict:
    out = {
        "id": page["id"],
        "type": "page",
        "title": page["title"],
        "space": {"key": page["space"]},
        "version": {"number": 3, "when": page["updated"]},
        "_links": {"webui": f"/spaces/{page['space']}/pages/{page['id']}"},
    }
    if with_body:
        out["body"] = {"storage": {"value": page["body"], "representation": "storage"}}
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path}")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # Confluence Server принимает Personal Access Token как Bearer
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            self._send(401, {"message": "Unauthorized"})
            return

        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/rest/api/space":
            self._send(200, {"results": [{"key": s["key"], "name": s["name"]} for s in SPACES], "size": len(SPACES)})
            return

        # поиск по CQL: нас интересует только ancestor=<id>
        if url.path == "/rest/api/content/search":
            cql = query.get("cql", [""])[0]
            start = int(query.get("start", ["0"])[0])
            limit = int(query.get("limit", ["25"])[0])
            expand = query.get("expand", [""])[0]

            m = re.search(r"ancestor\s*=\s*(\d+)", cql)
            items = descendants_of(m.group(1)) if m else []
            window = items[start : start + limit]
            self._send(200, {
                "results": [page_json(p, "body" in expand) for p in window],
                "start": start,
                "limit": limit,
                "size": len(window),
            })
            return

        # одна страница по id
        m = re.match(r"^/rest/api/content/(\d+)$", url.path)
        if m:
            for p in PAGES:
                if p["id"] == m.group(1):
                    self._send(200, page_json(p, with_body=True))
                    return
            self._send(404, {"message": "Not found"})
            return

        if url.path == "/rest/api/content":
            space = query.get("spaceKey", [None])[0]
            start = int(query.get("start", ["0"])[0])
            limit = int(query.get("limit", ["25"])[0])
            expand = query.get("expand", [""])[0]

            items = [p for p in PAGES if not space or p["space"] == space]
            window = items[start : start + limit]
            payload = {
                "results": [page_json(p, "body" in expand) for p in window],
                "start": start,
                "limit": limit,
                "size": len(window),
            }
            # Confluence отдаёт ссылку на следующую страницу, пока есть данные
            if start + limit < len(items):
                nxt = f"/rest/api/content?start={start + limit}&limit={limit}"
                if space:
                    nxt += f"&spaceKey={space}"
                payload["_links"] = {"next": nxt}
            self._send(200, payload)
            return

        self._send(404, {"message": "Unknown endpoint"})


if __name__ == "__main__":
    print(f"Заглушка Confluence на http://localhost:{PORT}")
    print(f"Токен: {TOKEN}")
    print(f"Спейсы: {', '.join(s['key'] for s in SPACES)}, страниц: {len(PAGES)}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
