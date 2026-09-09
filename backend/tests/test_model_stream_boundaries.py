"""Exercise the real HTTPX transport against a deliberately faulty local server."""
import asyncio
from contextlib import asynccontextmanager
import json
import time
import sys

import httpx
import pytest

from app import model_adapter
from app.model_gateway import ModelConnection, _parse_agent_tool_response
from app.sandbox_agent_loop import _await_model_with_cancellation, AgentJobCancelled


def run_async(operation):
    # Match production's selector loop. Python 3.12's Windows Proactor can
    # leak the server connection count after a peer resets a streaming socket.
    factory = asyncio.SelectorEventLoop if sys.platform == 'win32' else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(operation)


def connection():
    return ModelConnection(base_url='http://127.0.0.1', api_key='', model_name='test',
                           timeout_seconds=0, agent_options={
                               'first_chunk_timeout_seconds': 0.15,
                               'stream_stall_timeout_seconds': 0.15})


def event(delta):
    return ('data: ' + json.dumps({'choices': [{'delta': delta}]}) + '\n\n').encode()


@asynccontextmanager
async def server(respond):
    tasks = set()
    disconnected = asyncio.Event()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            size = next(int(line.split(b':', 1)[1]) for line in headers.lower().split(b'\r\n')
                        if line.startswith(b'content-length:'))
            await reader.readexactly(size)
            await respond(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (TimeoutError, ConnectionError):
                pass
            disconnected.set()
            tasks.discard(task)

    listener = await asyncio.start_server(handle, '127.0.0.1', 0)
    try:
        yield f'http://127.0.0.1:{listener.sockets[0].getsockname()[1]}', disconnected
    finally:
        listener.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(listener.wait_closed(), 2)


async def headers(writer, content_type='text/event-stream'):
    writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n'.encode())
    await writer.drain()


async def attempt(url):
    stats = {'attempts': 1}
    model_adapter._reset_attempt_stats(stats)
    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
        return await model_adapter._streamed_attempt(
            client, connection(), url, headers={}, body={},
            attempt_started=time.monotonic(), deadline=float('inf'), stats=stats), stats


@pytest.mark.parametrize('mode', ['headers', 'heartbeats', 'role_only', 'buffered_body'])
def test_first_progress_timeout_covers_headers_and_useless_traffic(mode):
    async def run():
        async def respond(reader, writer):
            if mode == 'headers':
                await reader.read()
                return
            await headers(writer, 'application/json' if mode == 'buffered_body' else 'text/event-stream')
            while not reader.at_eof():
                writer.write(event({'role': 'assistant'}) if mode == 'role_only' else
                             b' ' if mode == 'buffered_body' else b': heartbeat\n\n')
                await writer.drain()
                await asyncio.sleep(0.015)
        async with server(respond) as (url, disconnected):
            with pytest.raises(model_adapter.ModelFirstResponseTimeout):
                await asyncio.wait_for(attempt(url), 2)
            await asyncio.wait_for(disconnected.wait(), 1)
    run_async(run())


def test_heartbeats_after_content_do_not_reset_stall_budget():
    async def run():
        async def respond(reader, writer):
            await headers(writer)
            writer.write(event({'content': 'started'}))
            while not reader.at_eof():
                writer.write(b': alive\n\n')
                await writer.drain()
                await asyncio.sleep(0.015)
        async with server(respond) as (url, _):
            with pytest.raises(model_adapter.ModelStreamStall):
                await asyncio.wait_for(attempt(url), 2)
    run_async(run())


@pytest.mark.parametrize('delta', [{'content': 'x'}, {'reasoning_content': 'x'},
                                 {'tool_calls': [{'index': 0, 'function': {'arguments': 'x'}}]}])
def test_valid_generation_can_continue_beyond_each_idle_budget(delta, monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient',
                        lambda **kwargs: real_client(trust_env=False, **kwargs))
    async def run():
        async def respond(reader, writer):
            await headers(writer)
            for _ in range(12):
                writer.write(event(delta))
                await writer.drain()
                await asyncio.sleep(0.025)
            writer.write(b'data: [DONE]\n\n')
            await writer.drain()
        async with server(respond) as (url, _):
            started = time.monotonic()
            response = await model_adapter.post_json(connection(), url, headers={}, body={})
            stats = response.extensions['model_transport']
            assert time.monotonic() - started > 0.3
            assert response.status_code == 200
            assert stats['progress_events'] == 12
            assert stats['first_progress_ms'] is not None
    run_async(run())


def test_done_marker_does_not_make_incomplete_tool_arguments_executable():
    async def run():
        async def respond(reader, writer):
            await headers(writer)
            writer.write(event({'tool_calls': [{'index': 0, 'id': 'call_1', 'function': {
                'name': 'run_python', 'arguments': '{"code": "unfinished'}}]}))
            writer.write(b'data: [DONE]\n\n')
            await writer.drain()
        async with server(respond) as (url, _):
            response, _ = await attempt(url)
            with pytest.raises(ValueError):
                _parse_agent_tool_response(response.json())
    run_async(run())


@pytest.mark.parametrize('cancel_via_job', [True, False])
def test_cancellation_closes_live_http_request_before_returning(cancel_via_job):
    async def run():
        waiting = asyncio.Event()
        cancelled = False
        async def respond(reader, writer):
            await headers(writer)
            writer.write(event({'content': 'started'}))
            await writer.drain()
            waiting.set()
            await reader.read()
        async with server(respond) as (url, disconnected):
            task = asyncio.create_task(_await_model_with_cancellation(
                attempt(url), lambda: cancelled, poll_seconds=0.005))
            await asyncio.wait_for(waiting.wait(), 1)
            if cancel_via_job:
                cancelled = True
            else:
                task.cancel()
            with pytest.raises(AgentJobCancelled if cancel_via_job else asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 1)
    run_async(run())
