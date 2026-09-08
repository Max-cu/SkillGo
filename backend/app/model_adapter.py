"""Provider-specific request policy without silently changing the chosen model."""
from __future__ import annotations

import asyncio
import json
import time
import logging
import httpx


logger = logging.getLogger(__name__)


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


async def post_json(connection, url: str, *, headers: dict, body: dict):
    """Only retry inference transport; never repeat an executed tool operation."""
    deadline = time.monotonic() + connection.timeout_seconds
    async with httpx.AsyncClient(timeout=connection.timeout_seconds, verify=connection.tls_verify) as client:
        for attempt in range(3):
            started = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ReadTimeout('Model request deadline exceeded')
            try:
                async with asyncio.timeout(remaining):
                    response = await client.post(url, headers=headers, json=body)
                logger.info('Model transport attempt=%d duration_ms=%d status=%d',
                            attempt + 1, round((time.monotonic() - started) * 1000), response.status_code)
                if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                    response.raise_for_status()
                    return response
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                logger.warning('Model transport attempt=%d duration_ms=%d error=%s',
                               attempt + 1, round((time.monotonic() - started) * 1000), type(exc).__name__)
                if attempt == 2:
                    raise
            except TimeoutError as exc:
                logger.warning('Model transport attempt=%d duration_ms=%d error=RequestDeadlineExceeded',
                               attempt + 1, round((time.monotonic() - started) * 1000))
                raise httpx.ReadTimeout('Model request deadline exceeded') from exc
            await asyncio.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    raise httpx.ReadTimeout('Model request deadline exceeded')
