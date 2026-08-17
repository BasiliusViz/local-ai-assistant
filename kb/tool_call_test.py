"""Проверка: решает ли qwen3:8b сам вызвать kb_search.

Главный риск всей затеи. Инструмент может работать идеально, но если модель
его не зовёт (или зовёт на любой вопрос) — RAG не работает.
"""

import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

OLLAMA = "http://localhost:11434/v1/chat/completions"
MCP_URL = "http://localhost:8010/mcp"
MODEL = "qwen3:8b"

# Вопросы по базе — инструмент звать НАДО. Посторонние — НЕ надо.
CASES = [
    ("как откатить деплой payment-gateway?", True),
    ("сколько аппрувов нужно для merge request у нас?", True),
    ("какие требования к хранению секретов в наших сервисах?", True),
    ("что делать новому разработчику в первый день?", True),
    ("напиши функцию на python которая переворачивает строку", False),
    ("какая столица Франции?", False),
    ("чем отличается list от tuple в python?", False),
]


async def tool_schema() -> dict:
    """Берём схему прямо с MCP-сервера — как это делает Continue."""
    async with streamable_http_client(MCP_URL) as st:
        async with ClientSession(st[0], st[1]) as s:
            await s.initialize()
            t = (await s.list_tools()).tools[0]
            return {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }


def ask(question: str, tools: list[dict]) -> tuple[bool, str]:
    r = httpx.post(
        OLLAMA,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": question}],
            "tools": tools,
            "tool_choice": "auto",
        },
        timeout=300,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if calls:
        args = json.loads(calls[0]["function"]["arguments"])
        return True, f"{calls[0]['function']['name']}({args.get('query', '')!r})"
    text = (msg.get("content") or "").strip().replace("\n", " ")
    return False, text[:70]


async def main() -> None:
    tools = [await tool_schema()]
    print(f"Схема получена с MCP: {tools[0]['function']['name']}\n")

    ok = 0
    for question, should_call in CASES:
        called, detail = ask(question, tools)
        verdict = "OK " if called == should_call else "МИМО"
        ok += called == should_call
        want = "звать" if should_call else "не звать"
        print(f"[{verdict}] ждём: {want:9} | вызвал: {str(called):5} | {question}")
        print(f"         {detail}")

    print(f"\nИтог: {ok} из {len(CASES)}")


if __name__ == "__main__":
    asyncio.run(main())
