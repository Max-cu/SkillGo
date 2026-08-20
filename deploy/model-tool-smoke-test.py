from __future__ import annotations

import asyncio
import json
import sys


sys.path.insert(0, "/app")

from app.model_gateway import OpenAICompatibleGateway


async def main() -> None:
    gateway = OpenAICompatibleGateway()
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "This is a two-turn native tool protocol test. On the first turn call list_files "
                "with path /workspace. After receiving its tool result, call finish with summary "
                "'protocol ok' and artifacts ['/workspace/output/probe.txt']. "
                "You may batch compatible tools, but call finish by itself."
            ),
        },
        {"role": "user", "content": "Begin the protocol test."},
    ]

    first = await gateway.agent_step(messages=messages)
    assert first.tool_call_id, "first response did not use a native tool call"
    assert first.assistant_message, "first response did not retain the assistant message"
    assert first.output.get("action") == "list_files", first.output
    messages.append(first.assistant_message)
    messages.append(
        {
            "role": "tool",
            "tool_call_id": first.tool_call_id,
            "content": json.dumps(
                {
                    "tool_result": "list_files",
                    "payload": [
                        {"path": "/workspace/output/probe.txt", "type": "file", "size": 2}
                    ],
                }
            ),
        }
    )

    second = await gateway.agent_step(messages=messages)
    assert second.tool_call_id, "second response did not use a native tool call"
    assert second.output.get("action") == "finish", second.output
    assert second.output.get("artifacts") == ["/workspace/output/probe.txt"], second.output

    print("native_round_1", first.output["action"])
    print("reasoning_context_retained", "reasoning_content" in first.assistant_message)
    print("native_round_2", second.output["action"])
    print("protocol_status", "ok")


asyncio.run(main())
