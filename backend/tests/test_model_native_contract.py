from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app import model_gateway as gateway_module
from app.model_gateway import (
    ModelConnection, ModelGatewayError, OpenAICompatibleGateway,
    _model_http_error, _parse_agent_tool_response, _parse_tool_arguments,
)


def native(arguments, name='run_commands', call_id='call_1'):
    return {'model': 'test-model', 'choices': [{'message': {
        'content': None, 'reasoning_content': 'Keep original thinking context.',
        'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': arguments}}]}}]}


BAD_PLAN = '{"goal":"report","steps":[]}\n}'
BAD_BATCH = ('{"commands":[{"argv":["python3","first.py"],"reason":"first"},'
             '{"argv":["python3","second.py"],"reason":"search "LNG""}]}')


@pytest.mark.parametrize('arguments', [
    BAD_PLAN, BAD_BATCH, '{"path":"/workspace"} trailing',
    '```json\n{"path":"/workspace"}\n```',
    '[{"path":"/workspace"}]', 'null', 'true', '42',
    '{"commands":[],"commands":[{"argv":["python3"]}]}',
    '{"commands":[{"reason":"a","reason":"b"}]}',
    '{"offset":NaN}', '{"offset":Infinity}', '{"offset":-Infinity}',
])
def test_native_arguments_reject_malformed_or_ambiguous_wire_json(arguments):
    before = native(arguments)
    with pytest.raises(ValueError):
        _parse_agent_tool_response(before)
    assert before == native(arguments)


def test_valid_batch_preserves_full_object_original_arguments_and_reasoning():
    arguments = ' {"commands":[{"argv":["python3","one.py"]},{"argv":["python3","two.py"]}],"reason":"say \\"LNG\\""} '
    payload = native(arguments)
    before = copy.deepcopy(payload)
    calls, assistant = _parse_agent_tool_response(payload)
    assert calls[0].action['commands'] == json.loads(arguments)['commands']
    assert assistant == {'role': 'assistant', **payload['choices'][0]['message']}
    assert payload == before


def test_one_bad_native_call_rejects_the_whole_model_turn():
    payload = native('{"path":"/workspace"}', 'list_files')
    payload['choices'][0]['message']['tool_calls'].extend(
        native(BAD_BATCH, call_id='bad')['choices'][0]['message']['tool_calls'])
    with pytest.raises(ValueError):
        _parse_agent_tool_response(payload)


@pytest.mark.parametrize(('bad', 'name'), [(BAD_PLAN, 'update_plan'), (BAD_BATCH, 'run_commands')])
def test_gateway_corrects_bad_json_before_returning_any_executable_tool(monkeypatch, bad, name):
    seen = []
    history = [{'role': 'system', 'content': 'Use the tools'}, {'role': 'user', 'content': 'report'}]
    good_args = '{"goal":"report","steps":[]}' if name == 'update_plan' else '{"commands":[{"argv":["python3","good.py"]}]}'

    async def fake_post(connection, url, *, headers, body):
        seen.append(copy.deepcopy(body))
        if len(seen) == 2:
            assert body['messages'][:-1] == history
            assert 'one complete JSON object' in body['messages'][-1]['content']
            assert not any(m.get('tool_calls') for m in body['messages'])
        response = native(bad if len(seen) == 1 else good_args, name)
        return httpx.Response(200, json=response, request=httpx.Request('POST', url))

    monkeypatch.setattr(gateway_module, 'post_json', fake_post)
    gateway = OpenAICompatibleGateway(ModelConnection(base_url='https://model.test', api_key=None, model_name='test-model'))
    result = asyncio.run(gateway._request_tool_action(messages=history))
    assert len(seen) == 2
    assert result.output == {**json.loads(good_args), 'action': name}
    assert result.assistant_message['tool_calls'][0]['function']['arguments'] == good_args
    assert history == seen[0]['messages']


def test_two_malformed_replies_stop_native_correction_at_two_attempts(monkeypatch):
    attempts = []

    async def fake_post(connection, url, *, headers, body):
        attempts.append(body)
        return httpx.Response(200, json=native(BAD_BATCH), request=httpx.Request('POST', url))

    monkeypatch.setattr(gateway_module, 'post_json', fake_post)
    gateway = OpenAICompatibleGateway(ModelConnection(base_url='https://model.test', api_key=None, model_name='test-model'))
    with pytest.raises(ModelGatewayError) as error:
        asyncio.run(gateway._request_tool_action(messages=[{'role': 'user', 'content': 'report'}]))
    assert error.value.code == 'MODEL_TOOL_CALL_INVALID'
    assert len(attempts) == 2
    assert not any(m.get('tool_calls') for a in attempts for m in a['messages'])


def http_exception(payload, *, headers=None):
    request = httpx.Request('POST', 'https://model.test', headers={'Authorization': 'Bearer configured-test-secret'})
    response = httpx.Response(400, json=payload, headers=headers, request=request)
    return httpx.HTTPStatusError('bad request', request=request, response=response)


def test_http_diagnostic_keeps_only_bounded_allowlisted_fields_and_redacts_secrets():
    connection = ModelConnection(base_url='https://model.test', api_key='configured-test-secret', model_name='test-model')
    message = ('Invalid arguments: configured-test-secret Bearer another-secret password=hidden-value '
               'sk-other-secret https://person:pass@host/?token=hidden-query ' + 'x'*1000)
    error = _model_http_error(http_exception({'error': {
        'code': 'invalid_tool_arguments', 'type': 'invalid_request_error',
        'param': 'messages[3].tool_calls[0].function.arguments', 'message': message,
        'request_headers': {'Authorization': 'must-never-persist'}, 'prompt': 'must-never-persist',
    }, 'request': 'must-never-persist'}, headers={'x-request-id': 'req-safe-123'}), connection)
    assert error.code == 'MODEL_HTTP_ERROR'
    assert str(error).startswith('Private model returned HTTP 400: Invalid arguments:')
    assert error.details['http_status'] == 400
    assert error.details['request_id'] == 'req-safe-123'
    assert error.details['provider_error']['code'] == 'invalid_tool_arguments'
    assert len(error.details['provider_error']['message']) <= 512
    rendered = json.dumps(error.details) + str(error)
    for value in ('configured-test-secret', 'another-secret', 'hidden-value', 'sk-other-secret',
                  'hidden-query', 'person:pass', 'must-never-persist'):
        assert value not in rendered


@pytest.mark.parametrize('payload', [
    ['invalid'], {'error': {'message': ['not a string'], 'code': {'nested': 'private'}}},
    {'unexpected_body': 'must-never-persist'}, {'error': {'message': 'x'*20000}},
])
def test_http_diagnostic_does_not_persist_unstructured_or_oversized_response(payload):
    result = _model_http_error(http_exception(payload), ModelConnection(None, None, None))
    assert result.details == {'http_status': 400}


def test_http_plaintext_body_is_not_recorded():
    request = httpx.Request('POST', 'https://model.test')
    response = httpx.Response(400, text='private <html>body</html>', request=request)
    error = _model_http_error(httpx.HTTPStatusError('bad', request=request, response=response),
                              ModelConnection(None, None, None))
    assert error.details == {'http_status': 400}


@pytest.mark.parametrize('native_tools', [True, False])
def test_native_and_legacy_gateway_http_errors_retain_safe_provider_diagnostics(monkeypatch, native_tools):
    async def fake_post(connection, url, *, headers, body):
        raise http_exception({'error': {'code': 'bad_arguments', 'message': 'Invalid function.arguments'}})

    monkeypatch.setattr(gateway_module, 'post_json', fake_post)
    gateway = OpenAICompatibleGateway(ModelConnection(base_url='https://model.test', api_key=None, model_name='test-model', native_tools=native_tools))
    with pytest.raises(ModelGatewayError) as error:
        asyncio.run(gateway.agent_step(messages=[{'role': 'user', 'content': 'report'}]))
    assert error.value.details['provider_error']['code'] == 'bad_arguments'


def test_http_error_diagnostics_are_persisted_separately_from_transport(client, user_headers, monkeypatch):
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobEvent
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from test_orchestration import MemorySandbox

    _, version = create_version(client, user_headers, slug='native-http-diagnostic', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers,
                          data={'version_id': version['id'], 'instruction': 'report'}).json()

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            raise ModelGatewayError('MODEL_HTTP_ERROR', 'Private model returned HTTP 400',
                                    {'http_status': 400, 'provider_error': {'code': 'bad_arguments'}})

    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        with pytest.raises(ModelGatewayError):
            asyncio.run(_run_agent_loop(db, job, MemorySandbox(), skill_contexts=[{
                'name': 'Test', 'version': '1', 'root': '/workspace/skills/test',
                'skill_md': '# Report', 'runtime_requirements': {}}],
                gateway=Gateway(), job_cancelled=lambda: False))
        event = db.scalar(select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.event_type == 'reasoning'))
        assert event.data['model_http']['provider_error']['code'] == 'bad_arguments'
        assert 'model_transport' not in event.data
