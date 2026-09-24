from __future__ import annotations

import asyncio
import pytest
import httpx

from app.model_gateway import (
    ModelConnection,
    OpenAICompatibleGateway,
    _parse_agent_tool_response,
    _parse_json_object,
)


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


@pytest.mark.parametrize(
    ("media_type", "expected_filename"),
    [("image/png", b'filename="attachment.png"'), ("application/pdf", b'filename="attachment.pdf"')],
)
def test_mineru_ocr_uses_file_parse_and_normalizes_markdown(
    monkeypatch, media_type, expected_filename
):
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["content_type"] = request.headers["content-type"]
        observed["body"] = await request.aread()
        return httpx.Response(
            200,
            json={
                "backend": "hybrid-auto-engine",
                "version": "2.7.6",
                "results": {
                    "attachment": {
                        "md_content": "![](images/temp.jpg)\n\n发票号码：12345"
                    }
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return async_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("app.model_gateway.httpx.AsyncClient", client_factory)
    gateway = OpenAICompatibleGateway(
        ModelConnection(
            base_url="http://mineru.example.com",
            api_key=None,
            model_name="MinerU OCR",
            api_format="mineru",
            capabilities=("ocr",),
        )
    )

    result = asyncio.run(
        gateway.analyze_image(
            data=b"attachment-bytes",
            media_type=media_type,
            prompt="extract text",
            purpose="ocr",
        )
    )

    assert observed["url"] == "http://mineru.example.com/file_parse"
    assert str(observed["content_type"]).startswith("multipart/form-data; boundary=")
    assert expected_filename in observed["body"]
    assert b'name="return_images"' in observed["body"]
    assert result.output == {
        "message": "发票号码：12345",
        "provider": "mineru",
        "backend": "hybrid-auto-engine",
        "version": "2.7.6",
    }
    assert result.model_name == "MinerU OCR"


def test_mineru_connection_test_checks_openapi_without_running_ocr(monkeypatch):
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["url"] = str(request.url)
        return httpx.Response(200, json={"paths": {"/file_parse": {"post": {}}}})

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return async_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("app.model_gateway.httpx.AsyncClient", client_factory)
    gateway = OpenAICompatibleGateway(
        ModelConnection(
            base_url="http://mineru.example.com/file_parse",
            api_key=None,
            model_name="MinerU OCR",
            api_format="mineru",
            capabilities=("ocr",),
        )
    )

    result = asyncio.run(gateway.test_connection())

    assert observed == {
        "method": "GET",
        "url": "http://mineru.example.com/openapi.json",
    }
    assert result["model_name"] == "MinerU OCR"


def _mineru_gateway(monkeypatch, response_payload, *, observed=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if observed is not None:
            observed["url"] = str(request.url)
            observed["body"] = await request.aread()
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return async_client(transport=transport, timeout=kwargs.get("timeout"))

    monkeypatch.setattr("app.model_gateway.httpx.AsyncClient", client_factory)
    return OpenAICompatibleGateway(
        ModelConnection(
            base_url="http://mineru.example.com",
            api_key=None,
            model_name="MinerU OCR",
            api_format="mineru",
            capabilities=("ocr",),
        )
    )


def test_parse_document_mineru_returns_blocks_with_bbox_and_page_stats(monkeypatch):
    observed: dict[str, object] = {}
    gateway = _mineru_gateway(
        monkeypatch,
        {
            "backend": "hybrid-auto-engine",
            "version": "2.7.6",
            "results": {
                "doc": {
                    "md_content": "# 标题\n\n正文内容",
                    "content_list": [
                        {"type": "title", "text": "标题", "bbox": [1, 2, 100, 40], "page_idx": 0},
                        {"type": "text", "text": "正文内容", "bbox": [5, 60, 200, 90], "page_idx": 1},
                        "ignored-without-type",
                    ],
                }
            },
        },
        observed=observed,
    )

    result = asyncio.run(
        gateway.parse_document_mineru(data=b"scan-bytes", media_type="application/pdf")
    )

    assert observed["url"] == "http://mineru.example.com/file_parse"
    body = observed["body"]
    assert b'name="return_content_list"\r\n\r\ntrue\r\n' in body
    assert b'name="return_images"\r\n\r\nfalse\r\n' in body
    assert b'name="start_page_id"' not in body
    output = result.output
    assert output["provider"] == "mineru"
    assert output["backend"] == "hybrid-auto-engine"
    assert output["markdown"] == "# 标题\n\n正文内容"
    assert output["block_count"] == 2
    assert output["page_count"] == 2
    assert output["block_types"] == {"title": 1, "text": 1}
    assert output["blocks"][0]["bbox"] == [1, 2, 100, 40]
    assert output["blocks"][0]["page_idx"] == 0
    assert "2 block(s) across 2 page(s)" in output["message"]
    assert result.model_name == "MinerU OCR"


def test_parse_document_mineru_sends_page_range_and_accepts_json_string(monkeypatch):
    observed: dict[str, object] = {}
    gateway = _mineru_gateway(
        monkeypatch,
        {
            "results": {
                "doc": {
                    "md_content": "第3页",
                    "content_list": (
                        '[{"type": "text", "text": "第3页", "bbox": [0, 0, 10, 10],'
                        ' "page_idx": 2}]'
                    ),
                }
            }
        },
        observed=observed,
    )

    result = asyncio.run(
        gateway.parse_document_mineru(
            data=b"scan-bytes", media_type="application/pdf", pages=(2, 5)
        )
    )

    body = observed["body"]
    assert b'name="start_page_id"\r\n\r\n2\r\n' in body
    assert b'name="end_page_id"\r\n\r\n5\r\n' in body
    assert result.output["blocks"][0]["text"] == "第3页"
    assert result.output["page_count"] == 1


def test_parse_document_mineru_requires_mineru_connection():
    gateway = OpenAICompatibleGateway(
        ModelConnection(
            base_url="http://chat.example.com",
            api_key=None,
            model_name="qwen",
            api_format="openai",
            capabilities=("chat", "ocr"),
        )
    )
    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            gateway.parse_document_mineru(data=b"x", media_type="application/pdf")
        )
    assert exc_info.value.code == "DOCUMENT_STRUCTURE_BACKEND_UNAVAILABLE"


def test_parse_document_mineru_rejects_missing_content_list(monkeypatch):
    gateway = _mineru_gateway(
        monkeypatch,
        {"results": {"doc": {"md_content": "正文但没有结构"}}},
    )
    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            gateway.parse_document_mineru(data=b"x", media_type="application/pdf")
        )
    assert exc_info.value.code == "ATTACHMENT_MODEL_RESPONSE_INVALID"
