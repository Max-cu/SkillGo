from __future__ import annotations

import pytest

from app.model_gateway import _parse_agent_tool_response, _parse_json_object


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"summary":"ok"}', {"summary": "ok"}),
        ('```json\n{"summary":"ok"}\n```', {"summary": "ok"}),
        ('Result:\n{"summary":"ok"}\nDone', {"summary": "ok"}),
        (
            'Analysis: choose one action.\n{"action":"list_files","path":"/workspace"}\n'
            '{"note":"this trailing example must be ignored"}',
            {"action": "list_files", "path": "/workspace"},
        ),
        ('<think>internal reasoning</think>\n{"action":"finish","artifacts":[]}', {"action": "finish", "artifacts": []}),
    ],
)
def test_parse_model_json_object(content, expected):
    assert _parse_json_object(content) == expected


def test_parse_model_json_rejects_array():
    with pytest.raises(ValueError):
        _parse_json_object('["not", "an", "object"]')


def test_parse_native_tool_call_preserves_thinking_context():
    payload = {
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "I should inspect the workspace first.",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path":"/workspace","reason":"Inspect inputs"}',
                            },
                        }
                    ],
                }
            }
        ],
    }

    tool_calls, assistant_message = _parse_agent_tool_response(payload)

    assert tool_calls[0].action == {
        "action": "list_files",
        "path": "/workspace",
        "reason": "Inspect inputs",
    }
    assert tool_calls[0].id == "call_123"
    assert assistant_message["reasoning_content"] == "I should inspect the workspace first."
    assert assistant_message["tool_calls"] == payload["choices"][0]["message"]["tool_calls"]


def test_parse_native_tool_call_accepts_multiple_calls_in_order():
    tool_call = {
        "id": "call_123",
        "type": "function",
        "function": {"name": "list_files", "arguments": '{"path":"/workspace","reason":"Inspect"}'},
    }
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call, {**tool_call, "id": "call_456"}],
                }
            }
        ]
    }

    tool_calls, assistant_message = _parse_agent_tool_response(payload)

    assert [call.id for call in tool_calls] == ["call_123", "call_456"]
    assert [call.action["action"] for call in tool_calls] == ["list_files", "list_files"]
    assert assistant_message["tool_calls"] == payload["choices"][0]["message"]["tool_calls"]


def test_parse_native_tool_call_requires_finish_to_be_alone():
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path":"/workspace","reason":"Inspect"}',
                            },
                        },
                        {
                            "id": "call_456",
                            "type": "function",
                            "function": {
                                "name": "finish",
                                "arguments": '{"summary":"done","artifacts":[]}',
                            },
                        },
                    ],
                }
            }
        ]
    }

    with pytest.raises(ValueError, match="finish must be the only tool call"):
        _parse_agent_tool_response(payload)
