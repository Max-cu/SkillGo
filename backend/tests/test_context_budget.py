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


def test_recent_command_output_below_52k_stays_fully_inline():
    """Two-tier pruning: recent command results (4 KiB < size <= 52 KiB) are
    delivered whole, so build logs under ~50 KB no longer collapse to a
    1.6 KB head."""
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        def __init__(self): self.writes = []
        async def write_text(self, path, content): self.writes.append((path, content))
    sandbox = Sandbox()
    original = {'exit_code': 0, 'stdout': 'line\n' * 3000, 'stderr': ''}  # ~15 KiB
    messages = []
    result = asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'command', original,
        sandbox=sandbox, turn_number=2, operation_number=3, tool_call_id='c'))
    assert sandbox.writes == []
    assert result == original
    assert json.loads(messages[0]['content'])['payload']['stdout'] == original['stdout']


def test_large_command_stdout_is_offloaded_with_head_and_tail():
    """Output above the 52 KiB recent tier is persisted wholesale; the inline
    payload keeps BOTH head and tail (tracebacks live at the end)."""
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        files = {}
        async def write_text(self, path, content): self.files[path] = content
    sandbox = Sandbox()
    stdout = 'START-' + ('middle\n' * 20000) + '-FINAL_TRACEBACK_LINE'
    assert len(stdout.encode()) > 52 * 1024
    original = {'exit_code': 1, 'stdout': stdout, 'stderr': ''}
    messages = []
    result = asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'command', original,
        sandbox=sandbox, turn_number=1, operation_number=1, tool_call_id='a'))
    assert json.loads(sandbox.files[result['full_result_path']]) == original
    assert result['truncated']
    clipped = result['stdout']
    assert 'START-' in clipped and 'FINAL_TRACEBACK_LINE' in clipped
    assert 'bytes omitted' in clipped and result['full_result_path'] in clipped
    assert len(clipped) < 12000
    # Persisted file retains the untruncated stream.
    assert json.loads(sandbox.files[result['full_result_path']])['stdout'] == stdout


def test_non_command_tools_keep_legacy_4kb_tier():
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        files = {}
        async def write_text(self, path, content): self.files[path] = content
    sandbox = Sandbox()
    original = {'ok': True, 'content': 'x' * 8000}
    messages = []
    result = asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'read_skill', original,
        sandbox=sandbox, turn_number=1, operation_number=1, tool_call_id='s'))
    assert result['truncated'] and result['full_result_path'] in sandbox.files


def test_read_file_window_is_never_offloaded_even_when_large():
    """The model's explicit bounded window must arrive whole; this contract
    break caused the copy-to-work-file/smaller-chunk loop."""
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        def __init__(self): self.writes = []
        async def write_text(self, path, content): self.writes.append((path, content))
    sandbox = Sandbox()
    # 26 KiB mutable extracted text read as a raw-string payload.
    text = '条目' * 6500
    assert len(text.encode('utf-8')) > 4000
    messages = []
    result = asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'read_file', text,
        sandbox=sandbox, turn_number=9, operation_number=12, tool_call_id='r'))
    assert sandbox.writes == []
    assert isinstance(result, str) and result == text
    inline = json.loads(messages[0]['content'])['payload']
    assert inline == text
    # Dict-shaped read payloads (e.g. small error) are unaffected too.
    err = {'ok': False, 'error_code': 'SANDBOX_READ_FAILED', 'message': 'x' * 6000}
    messages2 = []
    result2 = asyncio.run(_append_tool_result_with_offload(messages2, SimpleNamespace(), 'read_file', err,
        sandbox=sandbox, turn_number=1, operation_number=2, tool_call_id='e'))
    assert result2 == err and sandbox.writes == []


def _shelf_entry(path, text):
    import hashlib
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return {'path': path, 'sha256': digest, 'bytes': len(text.encode('utf-8')),
            'chars': len(text), 'content': text}


def test_reference_shelf_is_pinned_after_old_exchanges_drop():
    messages = [{'role': 'system', 'content': 'rules ' * 400}, {'role': 'user', 'content': 'request'}]
    for i in range(20):
        messages.extend(exchange(i))
    spec = 'PHASE_ONE_SPEC_' + '必须遵守的规范 ' * 300
    checkpoint = {'requirements': ['Follow the spec'], 'plan': {'goal': 'report'},
                  'reference_shelf': [_shelf_entry('/workspace/skills/01-demo/demo/references/spec.md', spec)]}
    result = project(messages, checkpoint, 6000)
    memory = result[2]['content']
    # Pinned even though the original read exchange was projected out.
    assert 'PHASE_ONE_SPEC_' in memory
    assert spec in memory
    assert 'reference_shelf' in memory


def test_reference_shelf_drops_oldest_when_budget_is_tight_keeps_newest():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'}]
    old_spec = 'OLD_SPEC_MARKER ' + '甲' * 200
    new_spec = 'NEW_SPEC_MARKER ' + '乙' * 200
    checkpoint = {'requirements': ['r'], 'plan': {'goal': 'g'},
                  'reference_shelf': [
                      _shelf_entry('/workspace/skills/01-demo/demo/references/old.md', old_spec),
                      _shelf_entry('/workspace/skills/01-demo/demo/references/new.md', new_spec)]}
    result = project(messages, checkpoint, 1800)
    memory = result[2]['content']
    assert 'NEW_SPEC_MARKER' in memory
    assert 'OLD_SPEC_MARKER' not in memory


def test_retained_reference_payload_stays_full_inline_and_is_never_offloaded():
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        def __init__(self):
            self.writes = []
        async def write_text(self, path, content):
            self.writes.append((path, content))
    sandbox = Sandbox()
    payload = {'ok': True, 'path': '/workspace/skills/01-demo/demo/spec.md', 'reference': True,
               'retained': True, 'content': '规范' * 3000, 'sha256': 'abc', 'bytes': 18000}
    messages = []
    result = asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'read_file', payload,
        sandbox=sandbox, turn_number=3, operation_number=2, tool_call_id='q'))
    assert sandbox.writes == []  # no offload file written
    assert result == payload
    inline = json.loads(messages[0]['content'])['payload']
    assert inline['retained'] and inline['content'] == payload['content']
    assert 'full_result_path' not in inline and 'truncated' not in inline


def test_cached_reference_slice_payload_is_also_never_offloaded():
    from app.sandbox_agent_loop import _append_tool_result_with_offload
    class Sandbox:
        def __init__(self):
            self.writes = []
        async def write_text(self, path, content):
            self.writes.append((path, content))
    sandbox = Sandbox()
    payload = {'ok': True, 'path': '/workspace/skills/01-demo/demo/spec.md', 'reference': True,
               'cached': True, 'unchanged': True, 'content': '规范' * 3000, 'offset': 2000,
               'limit': 30000, 'chars': 18000}
    messages = []
    asyncio.run(_append_tool_result_with_offload(messages, SimpleNamespace(), 'read_file', payload,
        sandbox=sandbox, turn_number=8, operation_number=9, tool_call_id='z'))
    assert sandbox.writes == []
    inline = json.loads(messages[0]['content'])['payload']
    assert inline['cached'] and 'truncated' not in inline


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
