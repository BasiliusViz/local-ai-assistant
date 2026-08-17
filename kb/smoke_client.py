"""Проверка MCP-сервера настоящим клиентом: список инструментов и вызовы."""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010/mcp"


def show(title: str, payload) -> None:
    print(f"\n--- {title} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1200])


async def main() -> None:
    async with streamable_http_client(URL) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            info = await s.initialize()
            print(f"Сервер: {info.server_info.name}")

            tools = await s.list_tools()
            print(f"Инструменты: {[t.name for t in tools.tools]}")
            t = tools.tools[0]
            print(f"Параметры {t.name}: {list(t.input_schema['properties'])}")
            print(f"Описание, первая строка: {t.description.splitlines()[0]}")

            r = await s.call_tool("kb_search", {"query": "как откатить деплой платёжного шлюза"})
            show("concise", json.loads(r.content[0].text))

            r = await s.call_tool(
                "kb_search",
                {"query": "требования к секретам", "top_k": 1, "response_format": "detailed"},
            )
            show("detailed", json.loads(r.content[0].text))

            r = await s.call_tool("kb_search", {"query": "тест", "source_filter": "jira"})
            show("ошибка фильтра", json.loads(r.content[0].text))

            r = await s.call_tool("kb_search", {"query": "рецепт борща с пампушками"})
            show("ничего не найдено", json.loads(r.content[0].text))


if __name__ == "__main__":
    asyncio.run(main())
