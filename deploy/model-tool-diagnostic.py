from __future__ import annotations

import asyncio
import sys

import httpx


sys.path.insert(0, "/app")

from app.config import settings
from app.model_gateway import OpenAICompatibleGateway


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in one workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }
]


def error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500].replace("\n", " ")
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:500].replace("\n", " ")
    return str(payload)[:500].replace("\n", " ")


async def main() -> None:
    gateway = OpenAICompatibleGateway()
    headers = {"Content-Type": "application/json"}
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"
    common = {
        "model": settings.model_name,
        "messages": [
            {"role": "system", "content": "Call list_files once."},
            {"role": "user", "content": "Inspect /workspace."},
        ],
        "tools": TOOLS,
    }
    variants = [
        ("required_default_thinking", {"tool_choice": "required"}),
        ("auto_default_thinking", {}),
        ("required_non_thinking", {"tool_choice": "required", "thinking": {"type": "disabled"}}),
    ]
    async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
        for name, extra in variants:
            response = await client.post(
                gateway._chat_completions_url(), headers=headers, json={**common, **extra}
            )
            print(name, response.status_code)
            if response.status_code >= 400:
                print("error", error_message(response))
                continue
            payload = response.json()
            message = payload.get("choices", [{}])[0].get("message", {})
            print("has_tool_calls", bool(message.get("tool_calls")))
            print("has_reasoning_content", "reasoning_content" in message)


asyncio.run(main())
