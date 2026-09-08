from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent_context import project_context
from app.model_gateway import _transport_error
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


@pytest.mark.parametrize('exception,code', [
    (httpx.ReadTimeout, 'MODEL_RESPONSE_TIMEOUT'),
    (httpx.ConnectTimeout, 'MODEL_CONNECTION_FAILED'),
    (httpx.ConnectError, 'MODEL_CONNECTION_FAILED'),
    (httpx.RemoteProtocolError, 'MODEL_TRANSPORT_ERROR'),
])
def test_transport_errors_are_specific_and_do_not_expose_credentials(exception, code):
    error = _transport_error(exception('https://secret:password@private/'), 600)
    assert error.code == code
    assert 'secret' not in str(error)
    assert 'password' not in str(error)


@pytest.mark.parametrize('exception', [httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_transient_inference_transport_failure_retries_same_request(monkeypatch, exception):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            if len(calls) == 1:
                raise exception('transient')
            return httpx.Response(200, request=httpx.Request('POST', url), json={'ok': True})
    async def no_sleep(_): pass
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', no_sleep)
    response = asyncio.run(model_adapter.post_json(SimpleNamespace(timeout_seconds=10, tls_verify=True),
                          'https://model.test', headers={}, body={'messages': [{'role': 'user', 'content': 'same'}]}))
    assert response.status_code == 200
    assert len(calls) == 2 and calls[0] == calls[1]


def test_request_deadline_does_not_grant_another_full_retry_window(monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            calls.append(1)
            await asyncio.Event().wait()
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', Client)
    with pytest.raises(httpx.ReadTimeout, match='deadline'):
        asyncio.run(model_adapter.post_json(SimpleNamespace(timeout_seconds=0.01, tls_verify=True),
                    'https://model.test', headers={}, body={}))
    assert len(calls) == 1


def test_cancellation_is_not_retried(monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            calls.append(1)
            raise asyncio.CancelledError()
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', Client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(model_adapter.post_json(SimpleNamespace(timeout_seconds=10, tls_verify=True),
                    'https://model.test', headers={}, body={}))
    assert len(calls) == 1


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
