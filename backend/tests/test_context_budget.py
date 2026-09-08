import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.agent_context import estimate_tokens, project_context


def project(messages, checkpoint, budget, **kwargs):
    return project_context(messages, checkpoint=json.dumps(checkpoint),
        skill_contexts=[], loaded=set(), completed=set(), max_tokens=budget, **kwargs)


def exchange(index, size=1000):
    return [
        {'role': 'assistant', 'reasoning_content': 'native reasoning',
         'tool_calls': [{'id': str(index), 'function': {'name': 'read_file', 'arguments': '{"path":"input.txt"}'}}]},
        {'role': 'tool', 'tool_call_id': str(index), 'content': 'x' * size},
    ]


def test_large_observation_history_cannot_evict_latest_tool_result():
    messages = [{'role': 'system', 'content': 'rules ' * 1000}, {'role': 'user', 'content': 'original request'}]
    for i in range(30):
        messages.extend(exchange(i))
    checkpoint = {'requirements': ['Check every record'], 'plan': {'goal': 'report'},
        'recent_observations': [{'tool': 'read_file', 'ok': True, 'path': 'input.txt',
                                 'result_excerpt': '中文' * 10000} for _ in range(10)]}
    before = copy.deepcopy(messages)
    diagnostics = {}
    result = project(messages, checkpoint, 5000, diagnostics=diagnostics)
    assert result[-2:] == messages[-2:]
    assert result[:2] == messages[:2]
    assert messages == before
    assert estimate_tokens(result) <= 5000
    assert 'Check every record' in result[2]['content']
    assert '中文' not in result[2]['content']
    assert diagnostics['dropped_exchange_count'] > 0
    for index, message in enumerate(result):
        if message['role'] == 'tool':
            assert result[index-1]['tool_calls'][0]['id'] == message['tool_call_id']


def test_oversized_latest_exchange_is_rejected_not_silently_discarded():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'}, *exchange(1, 10000)]
    with pytest.raises(ValueError, match='最近完整交互'):
        project(messages, {}, 3000)


def test_initial_budget_reserves_space_for_tools_and_feedback():
    messages = [{'role': 'system', 'content': 'rules ' * 500}, {'role': 'user', 'content': 'request'}]
    diagnostics = {}
    with pytest.raises(ValueError, match='工具定义 1000'):
        project(messages, {}, 3000, tool_tokens=1000, initial_exchange_reserve=2048, diagnostics=diagnostics)
    assert diagnostics['minimum_required_tokens'] > 3000


def test_small_budget_preserves_multi_tool_exchange_and_legacy_json_results():
    native = {'role': 'assistant', 'reasoning_content': 'unaltered', 'tool_calls': [{'id': 'a'}, {'id': 'b'}]}
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'},
        *exchange('old', 10000), native,
        {'role': 'tool', 'tool_call_id': 'a', 'content': 'first'},
        {'role': 'tool', 'tool_call_id': 'b', 'content': 'second'}]
    assert project(messages, {}, 1500)[-3:] == messages[-3:]
    legacy = messages[:2] + [{'role': 'assistant', 'content': '{"action":"read_file"}'},
                             {'role': 'user', 'content': '{"tool_result":"read_file","payload":"important"}'}]
    assert project(legacy, {}, 1500)[-2:] == legacy[-2:]


def test_large_unicode_result_is_offloaded_with_bounded_valid_json():
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        files = {}
        async def write_text(self, path, content): self.files[path] = content
    sandbox = Sandbox()
    original = {'ok': True, 'content': '仪表记录' * 10000}
    messages = []
    result = asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'read_file', original,
        sandbox=sandbox, turn_number=1, operation_number=1, tool_call_id='a'))
    assert json.loads(sandbox.files[result['full_result_path']]) == original
    assert len(result['excerpt'].encode('utf-8')) <= 1600
    assert result['truncated']
    assert json.loads(messages[0]['content'])['payload']['full_result_path'] == result['full_result_path']


def test_context_failure_persists_budget_snapshot_and_fails_before_model_call(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobEvent
    from app.sandbox_agent_loop import _run_agent_loop
    from app.sandbox_runtime import SandboxRuntimeError
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from test_orchestration import MemorySandbox
    from sqlalchemy import select
    _, version = create_version(client, user_headers, slug='context-preflight', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'report'}).json()
    class Gateway:
        connection = SimpleNamespace(context_tokens=1000, agent_options={'context_tokens': 2000, 'max_output_tokens': 1000})
        async def agent_step(self, **kwargs):
            pytest.fail('Insufficient budget must fail before inference')
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        with pytest.raises(SandboxRuntimeError, match='上下文预算不足'):
            asyncio.run(_run_agent_loop(db, job, MemorySandbox(), skill_contexts=[{
                'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Guide', 'runtime_requirements': {}}],
                gateway=Gateway(), job_cancelled=lambda: False))
        db.rollback()
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        assert job.memory.data['model_budget']['input_budget_tokens'] == 1000
        event = db.scalar(select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.event_type == 'reasoning'))
        assert event.status == 'failed'
        assert event.data['context_budget']['minimum_required_tokens'] > 1000


def test_embedded_migration_preserves_application_logging():
    import logging
    from sqlalchemy import create_engine
    from app.database import initialize_schema
    logger = logging.getLogger('app.model_adapter')
    before = (logger.disabled, logger.level)
    engine = create_engine('sqlite:///:memory:')
    try:
        logger.disabled = False
        logger.setLevel(logging.INFO)
        initialize_schema(engine)
        assert logger.disabled is False
        assert logger.isEnabledFor(logging.INFO)
    finally:
        logger.disabled, logger.level = before
        engine.dispose()
