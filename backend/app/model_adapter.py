"""Provider-specific request policy without silently changing the chosen model."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import time
import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class ModelFirstResponseTimeout(httpx.ReadTimeout):
    """No meaningful generation arrived within the first-response budget."""

    def __init__(self, message: str, *, budget_seconds: float) -> None:
        super().__init__(message)
        self.budget_seconds = budget_seconds


class ModelStreamStall(httpx.ReadTimeout):
    """An already-started response stream stopped producing data."""

    def __init__(self, message: str, *, budget_seconds: float) -> None:
        super().__init__(message)
        self.budget_seconds = budget_seconds


def request_options(connection, *, attempt: int = 0) -> dict:
    options = dict(connection.agent_options or {})
    result = {}
    reasoning = options.get('reasoning_effort')
    if reasoning:
        result['reasoning_effort'] = reasoning
    else:
        result['temperature'] = connection.temperature if attempt == 0 else 0
    if options.get('max_output_tokens'):
        field = 'max_completion_tokens' if options.get('adapter') == 'openai_reasoning' else 'max_tokens'
        result[field] = options['max_output_tokens']
    return result


def compatibility_messages(messages: list[dict]) -> list[dict]:
    projected = []
    for message in messages:
        if message.get('tool_calls'):
            projected.append({'role': 'assistant', 'content': json.dumps({'executed_tool_calls': message['tool_calls']}, ensure_ascii=False)})
        elif message.get('role') == 'tool':
            projected.append({'role': 'user', 'content': message.get('content', '')})
        else:
            projected.append({key: value for key, value in message.items() if key != 'reasoning_content'})
    return projected


def _with_diagnostics(exc: BaseException, stats: dict[str, Any]) -> BaseException:
    exc.diagnostics = dict(stats)
    return exc


def _reset_attempt_stats(stats: dict[str, Any]) -> None:
    stats.update({"chunks": 0, "bytes": 0, "first_chunk_ms": None, "done": False,
                  "first_progress_ms": None, "progress_events": 0})


def _merge_stream_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge OpenAI-compatible SSE delta chunks into one non-streamed payload."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason = None
    model_name = None
    usage = None
    for event in events:
        if isinstance(event.get("model"), str):
            model_name = event["model"]
        candidate_usage = event.get("usage")
        if isinstance(candidate_usage, dict):
            usage = candidate_usage
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        if isinstance(choice.get("finish_reason"), str):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if isinstance(delta.get("content"), str):
            content_parts.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str):
            reasoning_parts.append(delta["reasoning_content"])
        for call in delta.get("tool_calls") or ():
            if not isinstance(call, dict):
                continue
            index = call.get("index")
            if not isinstance(index, int) or index < 0:
                index = len(tool_calls)
            slot = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if isinstance(call.get("id"), str) and call["id"]:
                slot["id"] = call["id"]
            function = call.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    slot["function"]["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    slot["function"]["arguments"] += function["arguments"]
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [
            {"id": slot["id"] or f"call_{index}", "type": slot["type"], "function": slot["function"]}
            for index, slot in sorted(tool_calls.items())
        ]
    return {
        "model": model_name or "",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": usage or {},
    }


class _ProgressBudget:
    """Absolute per-attempt deadlines, unaffected by heartbeats or empty deltas."""

    def __init__(self, connection, started, stats):
        self.first = connection.first_chunk_timeout_seconds or None
        self.stall = connection.stream_stall_timeout_seconds or None
        self.started = started
        self.last_progress = None
        self.stats = stats

    def advance(self):
        now = time.monotonic()
        if self.last_progress is None:
            self.stats["first_progress_ms"] = round((now - self.started) * 1000)
        self.last_progress = now
        self.stats["progress_events"] += 1

    async def wait(self, operation):
        budget = self.first if self.last_progress is None else self.stall
        origin = self.started if self.last_progress is None else self.last_progress
        remaining = None if budget is None else origin + budget - time.monotonic()
        try:
            # Explicitly yield even for buffered lines: an endless ready iterator
            # must not starve cancellation or defeat the absolute deadline.
            async with asyncio.timeout(remaining) as timer:
                await asyncio.sleep(0)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                return await operation()
        except TimeoutError as exc:
            if not timer.expired() and (remaining is None or remaining > 0):
                raise
            kind = ModelFirstResponseTimeout if self.last_progress is None else ModelStreamStall
            raise kind(f'No generation progress within {budget:g} seconds',
                       budget_seconds=budget) from exc


def _has_generation_progress(event):
    for choice in event.get("choices") or ():
        delta = choice.get("delta") or {}
        if any(isinstance(delta.get(key), str) and delta[key]
               for key in ("content", "reasoning_content")):
            return True
        for call in delta.get("tool_calls") or ():
            function = call.get("function") or {}
            if any(isinstance(function.get(key), str) and function[key]
                   for key in ("arguments", "name")):
                return True
    return False


async def _consume_sse(lines, *, progress, request_started, stats):
    events = []
    while True:
        try:
            line = await progress.wait(lines.__anext__)
        except StopAsyncIteration:
            break
        if stats["first_chunk_ms"] is None:
            stats["first_chunk_ms"] = round((time.monotonic() - request_started) * 1000)
        stats["bytes"] += len(line.encode("utf-8", "replace")) + 1
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            stats["done"] = True
            break
        try:
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError("SSE event must be an object")
            meaningful = _has_generation_progress(event)
        except (ValueError, TypeError, AttributeError) as exc:
            raise httpx.RemoteProtocolError('Invalid model SSE event') from exc
        stats["chunks"] += 1
        events.append(event)
        if meaningful:
            progress.advance()
    return events


async def _streamed_attempt(
    client,
    connection,
    url: str,
    *,
    headers: dict,
    body: dict,
    attempt_started: float,
    deadline: float,
    stats: dict[str, Any],
):
    """Perform one streamed request attempt and return the complete response.

    Returns None when the server answered with a retryable HTTP status.
    """
    request_headers = {**headers, "Accept": "text/event-stream"}
    progress = _ProgressBudget(connection, attempt_started, stats)
    # AsyncExitStack registers cleanup only after a successful, bounded enter.
    async with AsyncExitStack() as stack:
        response = await progress.wait(lambda: stack.enter_async_context(
            client.stream("POST", url, headers=request_headers, json={**body, "stream": True})))
        if response.status_code in RETRYABLE_STATUS:
            return None
        if response.status_code >= 400:
            # Read the error body so HTTPStatusError handling can inspect the text.
            await progress.wait(response.aread)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            # Some gateways ignore stream=true and answer with one buffered JSON
            # document; require the complete buffered response within the first
            # response budget, including the time already spent awaiting headers.
            raw = await progress.wait(response.aread)
            stats["bytes"] += len(raw)
            stats["first_chunk_ms"] = round((time.monotonic() - attempt_started) * 1000)
            return response
        events = await _consume_sse(
            response.aiter_lines(),
            progress=progress,
            request_started=attempt_started,
            stats=stats,
        )
        payload = _merge_stream_events(events)
        if not stats.get("done") and payload["choices"][0]["finish_reason"] is None:
            raise httpx.RemoteProtocolError('Model stream ended without finish_reason or [DONE]')
        final = httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            json=payload,
            request=response.request,
        )
        return final


async def post_json(connection, url: str, *, headers: dict, body: dict):
    """Stream one inference response with layered timeouts; retry transport only.

    Budgets: one overall per-call deadline (timeout_seconds, shared across
    retries; 0 disables it so only the first-response and stream stall
    budgets bound the call), an optional first-response budget and an
    optional stream stall budget from the connection. Retries only happen
    before meaningful generation is received. Partial generations are not
    replayed automatically; callers only receive fully assembled responses.
    """
    deadline = time.monotonic() + connection.timeout_seconds if connection.timeout_seconds > 0 else float("inf")
    stats: dict[str, Any] = {"attempts": 0, "chunks": 0, "bytes": 0, "first_chunk_ms": None, "done": False}
    timeout = httpx.Timeout(
        connect=connection.connect_timeout_seconds,
        read=connection.timeout_seconds or None,
        write=connection.timeout_seconds or None,
        pool=connection.connect_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=timeout, verify=connection.tls_verify) as client:
        for attempt in range(3):
            stats["attempts"] = attempt + 1
            _reset_attempt_stats(stats)
            started = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _with_diagnostics(httpx.ReadTimeout('Model request deadline exceeded'), stats)
            try:
                async with asyncio.timeout(remaining):
                    response = await _streamed_attempt(
                        client, connection, url,
                        headers=headers, body=body,
                        attempt_started=started, deadline=deadline, stats=stats,
                    )
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                _with_diagnostics(exc, stats)
                logger.warning('Model transport attempt=%d duration_ms=%d error=%s first_chunk_ms=%s chunks=%d bytes=%d',
                               attempt + 1, round((time.monotonic() - started) * 1000),
                               type(exc).__name__, stats["first_chunk_ms"], stats["chunks"], stats["bytes"])
                if attempt == 2 or stats["progress_events"]:
                    raise
            except TimeoutError as exc:
                deadline_error = httpx.ReadTimeout('Model request deadline exceeded')
                _with_diagnostics(deadline_error, stats)
                logger.warning('Model transport attempt=%d duration_ms=%d error=RequestDeadlineExceeded first_chunk_ms=%s chunks=%d bytes=%d',
                               attempt + 1, round((time.monotonic() - started) * 1000),
                               stats["first_chunk_ms"], stats["chunks"], stats["bytes"])
                raise deadline_error from exc
            else:
                if response is None:
                    logger.info('Model transport attempt=%d duration_ms=%d status=retryable',
                                attempt + 1, round((time.monotonic() - started) * 1000))
                    await asyncio.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
                    continue
                logger.info('Model transport attempt=%d duration_ms=%d status=%d first_chunk_ms=%s chunks=%d bytes=%d',
                            attempt + 1, round((time.monotonic() - started) * 1000), response.status_code,
                            stats["first_chunk_ms"], stats["chunks"], stats["bytes"])
                stats["duration_ms"] = round((time.monotonic() - started) * 1000)
                response.extensions["model_transport"] = dict(stats)
                return response
            await asyncio.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    raise _with_diagnostics(httpx.ReadTimeout('Model request deadline exceeded'), stats)
