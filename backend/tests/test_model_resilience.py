from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent_context import project_context
from app.model_gateway import _transport_error, ModelConnection, ModelResult, AgentToolCall
from app.model_adapter import ModelFirstResponseTimeout, ModelStreamStall
from app import model_adapter


def test_pinned_skill_is_not_duplicated_and_other_active_skill_survives():
    guide = 'Unique full guide: ' + 'requirement ' * 500
    messages = [{'role': 'system', 'content': 'Approved SKILL.md:\n' + guide},
                {'role': 'user', 'content': 'original request'}]
    before = copy.deepcopy(messages)
    result = project_context(messages, checkpoint='{}',
                             skill_contexts=[{'root': '/workspace/one', 'skill_md': guide},
                                             {'root': '/workspace/two', 'skill_md': 'Second guide'}],
                             loaded={1, 2}, completed=set(), max_tokens=5000)
    assert json.dumps(result).count('Unique full guide:') == 1
    assert 'Second guide' in json.dumps(result)
    assert result[:2] == before == messages


def _connection(**overrides) -> ModelConnection:
    options = dict(
        base_url='https://model.test',
        api_key='key',
        model_name='test-model',
        timeout_seconds=5,
        tls_verify=True,
    )
    options.update(overrides)
    return ModelConnection(**options)


def _sse_lines(*chunks) -> list[str]:
    return ['data: ' + json.dumps(chunk, ensure_ascii=False) for chunk in chunks] + ['data: [DONE]']


def _content_chunks(text: str = 'ok') -> list[dict]:
    return [
        {'model': 'test-model', 'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]},
        {'choices': [{'index': 0, 'delta': {'content': text}}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}},
    ]


class FakeStreamResponse:
    def __init__(self, lines=None, *, status_code=200, content_type='text/event-stream',
                 raw: bytes = b'{}', delay_before_body: float = 0.0, hang_after_lines: bool = False):
        self.status_code = status_code
        self.headers = {'content-type': content_type}
        self.extensions = {}
        self.request = httpx.Request('POST', 'https://model.test')
        self._lines = list(lines or [])
        self._raw = raw
        self._delay = delay_before_body
        self._hang = hang_after_lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._hang:
            await asyncio.Event().wait()
            yield ''

    async def aread(self) -> bytes:
        await asyncio.sleep(self._delay)
        return self._raw

    def json(self):
        return json.loads(self._raw)

    def raise_for_status(self):
        raise AssertionError('raise_for_status was not expected in this scenario')


class StreamContext:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    async def __aexit__(self, *args):
        return False


class StreamClient:
    """httpx.AsyncClient double whose stream() yields scripted responses or exceptions."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def stream(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        return StreamContext(self._script.pop(0))


async def _no_sleep(_seconds):
    pass


@pytest.mark.parametrize('exception,code', [
    (httpx.ReadTimeout, 'MODEL_RESPONSE_TIMEOUT'),
    (httpx.ConnectTimeout, 'MODEL_CONNECTION_FAILED'),
    (httpx.ConnectError, 'MODEL_CONNECTION_FAILED'),
    (httpx.RemoteProtocolError, 'MODEL_TRANSPORT_ERROR'),
    (ModelFirstResponseTimeout, 'MODEL_FIRST_RESPONSE_TIMEOUT'),
    (ModelStreamStall, 'MODEL_STREAM_STALLED'),
])
def test_transport_errors_are_specific_and_do_not_expose_credentials(exception, code):
    if exception in (ModelFirstResponseTimeout, ModelStreamStall):
        raised = exception('https://secret:password@private/', budget_seconds=5)
    else:
        raised = exception('https://secret:password@private/')
    error = _transport_error(raised, 600)
    assert error.code == code
    assert 'secret' not in str(error)
    assert 'password' not in str(error)


def test_sse_tool_call_deltas_merge_into_native_payload(monkeypatch):
    from app.model_gateway import _parse_agent_tool_response

    chunks = [
        {'model': 'deepseek-test', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': 'think'}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'run_python', 'arguments': '{"co'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 0, 'function': {'arguments': 'de": 1}'}}]}}]},
        {'choices': [{'index': 0, 'delta': {'tool_calls': [
            {'index': 1, 'id': 'call_2', 'type': 'function',
             'function': {'name': 'read_file', 'arguments': '{"path": "/workspace/a.txt"}'}}]}}]},
        {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
         'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}},
    ]
    client = StreamClient([FakeStreamResponse(_sse_lines(*chunks))])
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)

    response = asyncio.run(model_adapter.post_json(
        _connection(), 'https://model.test', headers={}, body={'messages': []}))

    payload = response.json()
    message = payload['choices'][0]['message']
    assert [call['id'] for call in message['tool_calls']] == ['call_1', 'call_2']
    assert message['tool_calls'][0]['function']['arguments'] == '{"code": 1}'
    assert message['tool_calls'][1]['function']['name'] == 'read_file'
    assert message['reasoning_content'] == 'think'
    assert payload['choices'][0]['finish_reason'] == 'tool_calls'
    assert payload['usage']['total_tokens'] == 15

    stats = response.extensions['model_transport']
    assert stats['attempts'] == 1 and stats['chunks'] == 5 and stats['done'] is True
    assert stats['first_chunk_ms'] is not None and stats['bytes'] > 0

    parsed_calls, assistant_message = _parse_agent_tool_response(payload)
    assert parsed_calls[0].action == {'code': 1, 'action': 'run_python'}
    assert parsed_calls[1].action == {'path': '/workspace/a.txt', 'action': 'read_file'}
    assert assistant_message['tool_calls'] == message['tool_calls']


def test_first_response_timeout_is_classified_with_diagnostics(monkeypatch):
    client = StreamClient([FakeStreamResponse(hang_after_lines=True)] * 3)
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)
    connection = _connection(timeout_seconds=0.5, agent_options={'first_chunk_timeout_seconds': 0.05})

    with pytest.raises(ModelFirstResponseTimeout) as excinfo:
        asyncio.run(model_adapter.post_json(connection, 'https://model.test', headers={}, body={}))

    assert excinfo.value.diagnostics['attempts'] == 3
    error = _transport_error(excinfo.value, 0.5)
    assert error.code == 'MODEL_FIRST_RESPONSE_TIMEOUT'
    assert error.details['attempts'] == 3
    assert error.details['first_chunk_ms'] is None


def test_stream_stall_timeout_is_classified_with_diagnostics(monkeypatch):
    started = ['data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'par'}}]})]
    client = StreamClient([FakeStreamResponse(started, hang_after_lines=True)] * 3)
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)
    connection = _connection(timeout_seconds=0.5, agent_options={
        'first_chunk_timeout_seconds': 0.05, 'stream_stall_timeout_seconds': 0.05})

    with pytest.raises(ModelStreamStall) as excinfo:
        asyncio.run(model_adapter.post_json(connection, 'https://model.test', headers={}, body={}))

    error = _transport_error(excinfo.value, 0.5)
    assert error.code == 'MODEL_STREAM_STALLED'
    assert error.details['first_chunk_ms'] is not None
    assert error.details['attempts'] == 3


def test_buffered_json_response_only_uses_total_deadline(monkeypatch):
    payload = {'model': 'test-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
               'message': {'role': 'assistant', 'content': 'ok'}}], 'usage': {}}
    response = FakeStreamResponse(content_type='application/json',
                                  raw=json.dumps(payload).encode(), delay_before_body=0.3)
    client = StreamClient([response])
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    connection = _connection(timeout_seconds=5, agent_options={
        'first_chunk_timeout_seconds': 0.05, 'stream_stall_timeout_seconds': 0.05})

    result = asyncio.run(model_adapter.post_json(connection, 'https://model.test', headers={}, body={}))

    assert result.json() == payload
    assert result.extensions['model_transport']['first_chunk_ms'] >= 200


def test_truncated_stream_without_finish_is_transport_error(monkeypatch):
    lines = ['data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'partial'}}]})]
    client = StreamClient([FakeStreamResponse(lines)] * 3)
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)

    with pytest.raises(httpx.RemoteProtocolError) as excinfo:
        asyncio.run(model_adapter.post_json(_connection(), 'https://model.test', headers={}, body={}))

    error = _transport_error(excinfo.value, 5)
    assert error.code == 'MODEL_TRANSPORT_ERROR'
    assert error.details['attempts'] == 3


@pytest.mark.parametrize('exception', [httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_transient_inference_transport_failure_retries_same_request(monkeypatch, exception):
    client = StreamClient([exception('transient'), FakeStreamResponse(_sse_lines(*_content_chunks()))])
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)

    response = asyncio.run(model_adapter.post_json(
        _connection(), 'https://model.test', headers={}, body={'messages': [{'role': 'user', 'content': 'same'}]}))

    assert response.status_code == 200
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1]


def test_retryable_http_status_is_retried(monkeypatch):
    client = StreamClient([
        FakeStreamResponse([], status_code=503),
        FakeStreamResponse(_sse_lines(*_content_chunks())),
    ])
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)

    response = asyncio.run(model_adapter.post_json(_connection(), 'https://model.test', headers={}, body={}))

    assert response.status_code == 200
    assert response.extensions['model_transport']['attempts'] == 2
    assert len(client.calls) == 2


def test_request_deadline_does_not_grant_another_full_retry_window(monkeypatch):
    client = StreamClient([FakeStreamResponse(hang_after_lines=True)])
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)

    with pytest.raises(httpx.ReadTimeout, match='deadline'):
        asyncio.run(model_adapter.post_json(
            _connection(timeout_seconds=0.01), 'https://model.test', headers={}, body={}))

    assert len(client.calls) == 1


def test_cancellation_is_not_retried(monkeypatch):
    client = StreamClient([asyncio.CancelledError()])
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(model_adapter.post_json(
            _connection(timeout_seconds=10), 'https://model.test', headers={}, body={}))

    assert len(client.calls) == 1


def test_model_connection_http_timeout_zero_means_none():
    assert _connection().http_timeout == 5
    assert _connection(timeout_seconds=0).http_timeout is None


def test_zero_total_budget_disables_transport_deadline(monkeypatch):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return StreamClient([FakeStreamResponse(_sse_lines(*_content_chunks()))])

    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', factory)

    response = asyncio.run(model_adapter.post_json(
        _connection(timeout_seconds=0), 'https://model.test', headers={}, body={'messages': []}))

    assert response.status_code == 200
    assert captured['timeout'].read is None
    assert captured['timeout'].write is None
    assert captured['timeout'].connect is not None


def test_zero_total_budget_still_enforces_first_response_timeout(monkeypatch):
    client = StreamClient([FakeStreamResponse(hang_after_lines=True)] * 3)
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)
    connection = _connection(timeout_seconds=0, agent_options={'first_chunk_timeout_seconds': 0.05})

    with pytest.raises(ModelFirstResponseTimeout) as excinfo:
        asyncio.run(model_adapter.post_json(connection, 'https://model.test', headers={}, body={}))

    error = _transport_error(excinfo.value, 0)
    assert error.code == 'MODEL_FIRST_RESPONSE_TIMEOUT'
    assert excinfo.value.diagnostics['attempts'] == 3


def test_zero_total_budget_still_enforces_stream_stall(monkeypatch):
    started = ['data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'par'}}]})]
    client = StreamClient([FakeStreamResponse(started, hang_after_lines=True)] * 3)
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', lambda **kwargs: client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', _no_sleep)
    connection = _connection(timeout_seconds=0, agent_options={
        'first_chunk_timeout_seconds': 0.05, 'stream_stall_timeout_seconds': 0.05})

    with pytest.raises(ModelStreamStall) as excinfo:
        asyncio.run(model_adapter.post_json(connection, 'https://model.test', headers={}, body={}))

    error = _transport_error(excinfo.value, 0)
    assert error.code == 'MODEL_STREAM_STALLED'
    assert excinfo.value.diagnostics['first_chunk_ms'] is not None
    assert excinfo.value.diagnostics['attempts'] == 3


def test_transport_error_without_total_budget_mentions_layered_detection():
    error = _transport_error(httpx.ReadTimeout('Model request deadline exceeded'), 0)

    assert error.code == 'MODEL_RESPONSE_TIMEOUT'
    assert '总预算 0' not in str(error)
    assert '未设置单轮总预算' in str(error)


def test_failed_model_round_persists_timing_before_worker_rollback(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobEvent
    from app.model_gateway import ModelGatewayError
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from test_orchestration import MemorySandbox
    from sqlalchemy import select

    _, version = create_version(client, user_headers, slug='model-timeout-evidence', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers,
                          data={'version_id': version['id'], 'instruction': 'report'}).json()
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        async def agent_step(self, **kwargs):
            raise ModelGatewayError('MODEL_RESPONSE_TIMEOUT', 'Model deadline exceeded')
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill',
                     'skill_md': '# Answer', 'runtime_requirements': {}}]
        with pytest.raises(ModelGatewayError):
            asyncio.run(_run_agent_loop(db, job, MemorySandbox(), skill_contexts=contexts,
                        gateway=Gateway(), job_cancelled=lambda: False))
        db.rollback()
    with SessionLocal() as db:
        event = db.scalar(select(JobEvent).where(JobEvent.job_id == created['id'], JobEvent.event_type == 'reasoning'))
        assert event.status == 'failed'
        assert event.data['error_code'] == 'MODEL_RESPONSE_TIMEOUT'
        assert event.data['duration_ms'] >= 0
        assert event.data['input_estimated_tokens'] > 0


def test_reasoning_event_records_model_transport_stats(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobEvent
    from app.sandbox_runtime import SandboxRuntimeError
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from test_orchestration import MemorySandbox
    from sqlalchemy import select

    _, version = create_version(client, user_headers, slug='model-transport-stats', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers,
                          data={'version_id': version['id'], 'instruction': 'report'}).json()
    transport_stats = {'attempts': 1, 'chunks': 5, 'bytes': 300, 'first_chunk_ms': 12,
                       'done': True, 'duration_ms': 40}
    block_call = AgentToolCall(
        id='call_1',
        action={'action': 'block', 'summary': '测试沙箱不可用，无法完成目标',
                'evidence': 'MemorySandbox 无法执行真实操作', 'reason': '测试终止'},
    )
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        async def agent_step(self, **kwargs):
            return ModelResult(output=dict(block_call.action), model_name='test-model',
                               token_usage={}, tool_calls=(block_call,),
                               transport_stats=transport_stats)
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill',
                     'skill_md': '# Answer', 'runtime_requirements': {}}]
        with pytest.raises(SandboxRuntimeError):
            asyncio.run(_run_agent_loop(db, job, MemorySandbox(), skill_contexts=contexts,
                        gateway=Gateway(), job_cancelled=lambda: False))
        db.rollback()
    with SessionLocal() as db:
        event = db.scalar(select(JobEvent).where(JobEvent.job_id == created['id'], JobEvent.event_type == 'reasoning'))
        assert event.status == 'succeeded'
        assert event.data['model_transport'] == transport_stats
