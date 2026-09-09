from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.agent_context import project_context
from app.agent_policy import AgentExecutionState
from app.model_adapter import request_options
from app.model_gateway import ModelConnection, ModelResult
from app.runtime_profile import detect_runtime_profile
from app.sandbox_runtime import SandboxCommandResult, SandboxRuntimeError
from app.sandbox_tool_registry import normalize_agent_action, validate_agent_action
from app.workflow_tools import run_verifier


class MemorySandbox:
    def __init__(self, *, failed=False, mutate=False):
        self.files = {'/workspace/output/result.txt': b'42', '/workspace/input/data.txt': b'21'}
        self.failed = failed
        self.mutate = mutate
        self.calls = []

    async def list_files(self, path):
        return [{'path': name, 'type': 'file', 'size': len(data)} for name, data in self.files.items() if name.startswith(path.rstrip('/') + '/')]

    def read_workspace_file(self, path):
        if path not in self.files:
            raise SandboxRuntimeError('SANDBOX_ARTIFACT_MISSING', path)
        return self.files[path]

    download_file = read_workspace_file

    async def write_text(self, path, content):
        self.files[path] = content.encode()

    async def read_text(self, path, offset=0, limit=30000):
        return self.files[path].decode()[offset:offset+limit]

    async def command(self, argv, **kwargs):
        self.calls.append(argv)
        if self.mutate:
            self.files['/workspace/output/result.txt'] = b'43'
        return SandboxCommandResult(1 if self.failed else 0, json.dumps({'checks': [{'requirement_id': 'r1', 'passed': not self.failed, 'observed': 42}]}), 'wrong total' if self.failed else '')


def plan():
    return {'goal': 'calculate then report', 'steps': [
        {'id': 'compute', 'title': 'Calculate', 'status': 'completed', 'evidence': 'result', 'skill_index': 1, 'input_refs': ['/workspace/input/data.txt'], 'output_refs': ['/workspace/output/result.txt']},
        {'id': 'report', 'title': 'Report', 'status': 'completed', 'evidence': 'report', 'skill_index': 2, 'depends_on': ['compute'], 'output_refs': ['/workspace/output/report.txt']},
        {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'depends_on': ['report']}],
        'success_criteria': ['The answer is 42'], 'validation_step_id': 'verify'}


def test_failed_verifier_cannot_be_replaced_by_generation_or_pass_claim():
    state = AgentExecutionState(skill_count=1)
    state.record({'action': 'command', 'argv': ['generate']}, {'exit_code': 0})
    proof = asyncio.run(run_verifier(MemorySandbox(failed=True), {'argv': ['verify']}, requirements=['correct']))
    state.verification = proof
    result = state.record_validation({'status': 'passed', 'summary': 'claimed', 'evidence': 'claimed', 'checks': ['claimed'], 'verification_id': proof['verification_id']}, artifact_snapshot=proof['artifacts'])
    assert result['error_code'] == 'VALIDATION_VERIFIER_FAILED'
    assert state.validation is None


@pytest.mark.parametrize('failed,mutate', [(True, False), (False, True)])
def test_verifier_requires_successful_exit_and_unchanged_output(failed, mutate):
    proof = asyncio.run(run_verifier(MemorySandbox(failed=failed, mutate=mutate), {'argv': ['verify']}, requirements=['correct']))
    assert not proof['ok']


def test_verifier_requires_coverage_of_all_requirements():
    proof = asyncio.run(run_verifier(MemorySandbox(), {'argv': ['verify']}, requirements=['correct', 'complete']))
    assert not proof['ok']
    assert 'r2' in proof['message']


def test_verifier_report_can_contain_more_than_twenty_checks():
    action = {'action': 'record_validation', 'status': 'passed', 'summary': 'observed', 'evidence': 'report', 'checks': [str(i) for i in range(30)]}
    assert validate_agent_action(action) is None
    assert normalize_agent_action(action)['checks'] == action['checks']


def test_failed_write_invalidates_cached_read_and_verification():
    state = AgentExecutionState(skill_count=1)
    read = {'action': 'read_file', 'path': '/workspace/value.txt'}
    state.record(read, 'old')
    state.verification = {'ok': True}
    state.record({'action': 'run_python', 'code': 'write_then_raise()'}, {'exit_code': 1})
    assert state.cached_observation(read) is None
    assert state.verification is None


def test_parameter_rejection_does_not_invalidate_workspace():
    state = AgentExecutionState(skill_count=1)
    read = {'action': 'read_file', 'path': '/workspace/value.txt'}
    state.record(read, 'old')
    state.record({'action': 'run_python', 'code': ''}, {'ok': False, 'error_code': 'SANDBOX_ACTION_INVALID'})
    assert state.cached_observation(read) == 'old'


def test_progressful_repeated_reads_are_allowed():
    state = AgentExecutionState(skill_count=1)
    read = {'action': 'read_file', 'path': '/workspace/value.txt'}
    for i in range(8):
        state.record({'action': 'write_file', 'path': read['path'], 'content': str(i)}, {'ok': True})
        state.record(read, str(i))
        assert state.repeated_count(read) <= 1


def test_successful_noop_does_not_reset_stall_detector():
    state = AgentExecutionState(skill_count=1)
    action = {'action': 'command', 'argv': ['true']}
    for _ in range(7):
        state.record(action, {'exit_code': 0, 'stdout': '', 'stderr': ''})
    assert state.repeated_count(action) == 6
    assert state.progress_epoch == 1
    assert state.mutation_epoch == 7


def test_corrupt_output_is_a_recoverable_verification_failure():
    sandbox = MemorySandbox()
    sandbox.files['/workspace/output/broken.json'] = b'{'
    proof = asyncio.run(run_verifier(sandbox, {'argv': ['verify']}, requirements=['correct']))
    assert not proof['ok']
    assert 'Invalid JSON' in proof['message']
    assert proof['full_result_path'] in sandbox.files


@pytest.mark.parametrize('refs', [None, 12, 'path', [False]])
def test_plan_rejects_malformed_file_refs_before_dispatch(refs):
    action = {**plan(), 'action': 'update_plan'}
    action['steps'][0]['input_refs'] = refs
    assert 'string array' in validate_agent_action(action)


def test_optional_documentation_command_is_not_a_hard_dependency():
    profile = detect_runtime_profile(skill_md='---\nname: report\ndescription: report\n---\nOptional alternative:\n```bash\npandoc input.md -o output.pdf\n```', manifest={})
    assert 'pandoc' in profile['requirements']['binaries']
    assert 'pandoc' not in profile['requirements']['required_binaries']


def test_active_skill_survives_long_context_without_editing_provider_messages():
    messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'original'}]
    for i in range(50):
        messages.extend([{'role': 'assistant', 'reasoning_content': 'keep intact', 'tool_calls': [{'id': str(i)}]}, {'role': 'tool', 'tool_call_id': str(i), 'content': 'x' * 1000}])
    before = copy.deepcopy(messages)
    projected = project_context(messages, checkpoint='{}', skill_contexts=[{'root': '/workspace/skill', 'skill_md': 'CRITICAL: never copy example names'}], loaded={1}, completed=set(), max_tokens=4000)
    assert 'CRITICAL' in json.dumps(projected)
    assert len(projected) < len(messages)
    assert messages == before
    for i, message in enumerate(projected):
        if message['role'] == 'tool':
            assert projected[i-1]['tool_calls'][0]['id'] == message['tool_call_id']
            assert projected[i-1]['reasoning_content'] == 'keep intact'


def test_oversized_original_context_is_reported_not_silently_cut():
    with pytest.raises(ValueError, match='Original request'):
        project_context([{'role': 'system', 'content': 'x' * 10000}, {'role': 'user', 'content': 'original'}], checkpoint='{}', skill_contexts=[], loaded=set(), completed=set(), max_tokens=1000)


def test_plan_rejects_cycles_unready_dependencies_and_missing_files():
    state = AgentExecutionState(skill_count=2)
    action = plan()
    action['steps'][0]['depends_on'] = ['report']
    assert state.update_plan(action)['error_code'] == 'PLAN_DEPENDENCY_INVALID'
    action = plan()
    action['steps'][0]['status'] = 'pending'
    assert state.update_plan(action)['error_code'] == 'PLAN_DEPENDENCY_PENDING'
    assert state.update_plan(plan(), files={})['error_code'] == 'PLAN_FILE_MISSING'


def test_input_change_invalidates_step_and_descendants_only():
    state = AgentExecutionState(skill_count=2, completed_skill_indexes={1, 2})
    files = {'/workspace/input/data.txt': 'a', '/workspace/output/result.txt': 'b', '/workspace/output/report.txt': 'c'}
    assert state.update_plan(plan(), files=files)['ok']
    state.invalidate_changed_steps({**files, '/workspace/input/data.txt': 'changed'})
    assert all(step['status'] == 'pending' for step in state.plan['steps'])
    assert state.completed_skill_indexes == set()


def test_requirements_cannot_disappear_on_replan():
    state = AgentExecutionState(skill_count=2)
    assert state.update_plan(plan())['ok']
    revised = plan()
    revised['success_criteria'] = ['Just create a file']
    assert state.update_plan(revised)['error_code'] == 'PLAN_REQUIREMENT_REMOVED'


def test_model_adapter_reserves_output_budget_and_selects_parameter_names():
    connection = ModelConnection(None, None, 'chosen-model', agent_options={'adapter': 'openai_reasoning', 'reasoning_effort': 'high', 'context_tokens': 64000, 'max_output_tokens': 16000})
    assert connection.context_tokens == 48000
    assert request_options(connection) == {'reasoning_effort': 'high', 'max_completion_tokens': 16000}


def test_agent_loop_executes_platform_verifier_before_finish(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='orchestration-loop', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()
    class ScriptedGateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            steps = [{'id': 'make', 'title': 'Make', 'status': 'in_progress', 'evidence': '', 'output_refs': ['/workspace/output/result.txt']}, {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'evidence': '', 'depends_on': ['make']}]
            base = {'action': 'update_plan', 'goal': 'answer 42', 'steps': steps, 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            if self.turn == 1:
                action = base
            elif self.turn == 2:
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif self.turn == 3:
                proof = next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "run_verifier"'))
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif self.turn == 4:
                for step in steps:
                    step.update(status='completed', evidence='verified output')
                action = base
            elif self.turn == 5:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert self.turn == 6
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        result = asyncio.run(_run_agent_loop(db, job, MemorySandbox(), skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']
        assert result[2] == 6
        assert job.execution_plan['steps'][0]['status'] == 'completed'


def test_agent_loop_recovers_when_file_tools_leave_workspace(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='orchestration-path-denied', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()

    class PathGuardSandbox(MemorySandbox):
        async def read_text(self, path, offset=0, limit=30000):
            if not path.startswith('/workspace'):
                raise SandboxRuntimeError('SANDBOX_PATH_DENIED', 'Path must stay inside /workspace')
            return await super().read_text(path, offset=offset, limit=limit)
        async def write_text(self, path, content):
            if not path.startswith('/workspace'):
                raise SandboxRuntimeError('SANDBOX_PATH_DENIED', 'Path must stay inside /workspace')
            await super().write_text(path, content)
        async def list_files(self, path):
            if not path.startswith('/workspace'):
                raise SandboxRuntimeError('SANDBOX_PATH_DENIED', 'Path must stay inside /workspace')
            return await super().list_files(path)

    def last_payload(messages, tool):
        return next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "%s"' % tool))

    class ScriptedGateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            steps = [{'id': 'make', 'title': 'Make', 'status': 'in_progress', 'evidence': '', 'output_refs': ['/workspace/output/result.txt']}, {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'evidence': '', 'depends_on': ['make']}]
            base = {'action': 'update_plan', 'goal': 'answer 42', 'steps': steps, 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            if self.turn == 1:
                action = base
            elif self.turn == 2:
                action = {'action': 'list_files', 'path': '/usr/local/lib/python3.12'}
            elif self.turn == 3:
                assert last_payload(messages, 'list_files') == {'ok': False, 'error_code': 'SANDBOX_PATH_DENIED', 'message': 'Path must stay inside /workspace', 'requested_path': '/usr/local/lib/python3.12', 'hint': 'list_files is limited to paths inside /workspace; start from /workspace.'}
                action = {'action': 'read_file', 'path': '/usr/local/lib/python3.12/site-packages/reportlab/pdfbase/_cidfontdata.py'}
            elif self.turn == 4:
                payload = last_payload(messages, 'read_file')
                assert payload['ok'] is False and payload['error_code'] == 'SANDBOX_PATH_DENIED' and payload['hint']
                action = {'action': 'write_file', 'path': '/etc/skillgo-write', 'content': 'x'}
            elif self.turn == 5:
                payload = last_payload(messages, 'write_file')
                assert payload['ok'] is False and payload['error_code'] == 'SANDBOX_PATH_DENIED' and payload['hint']
                action = {'action': 'read_file', 'path': '/workspace/input/data.txt'}
            elif self.turn == 6:
                assert last_payload(messages, 'read_file') == '21'
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif self.turn == 7:
                proof = last_payload(messages, 'run_verifier')
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif self.turn == 8:
                for step in steps:
                    step.update(status='completed', evidence='verified output')
                action = base
            elif self.turn == 9:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert self.turn == 10
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        result = asyncio.run(_run_agent_loop(db, job, PathGuardSandbox(), skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']


def test_automatic_mode_and_answer_are_scoped_to_owner(client, user_headers, owner_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob, WorkflowJobMemory, JobStatus, RunStatus
    from app.execution_runtime import ensure_job_run
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='orchestration-auto', package=sandbox_skill_zip())
    # Explicit mode remains available regardless of automatic routing.
    response = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'})
    assert response.status_code == 201, response.text
    job_id = response.json()['id']
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        job.status = JobStatus.WAITING_USER
        job.memory = WorkflowJobMemory(data={'pending_question': {'id': 'q1', 'question': 'Which unit?'}, 'answers': []}) if job.memory is None else job.memory
        job.memory.data = {'pending_question': {'id': 'q1', 'question': 'Which unit?'}, 'answers': []}
        ensure_job_run(db, job).status = RunStatus.WAITING_USER
        db.commit()
    answer = {'question_id': 'q1', 'answer': 'millimetres'}
    assert client.post(f'/api/v1/jobs/{job_id}/answer', headers=owner_headers, json=answer).status_code == 404
    response = client.post(f'/api/v1/jobs/{job_id}/answer', headers=user_headers, json=answer)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'queued'
    assert response.json()['pending_question'] is None
    assert client.post(f'/api/v1/jobs/{job_id}/answer', headers=user_headers, json=answer).status_code == 409


def test_no_matching_route_does_not_choose_arbitrary_skill():
    from app.routers.jobs import _fallback_route
    candidate = SimpleNamespace(skill=SimpleNamespace(name='Word Builder', slug='word-builder', summary='DOCX reports', description='Make Word documents'))
    assert _fallback_route('unrelatedtask', None, [candidate]) == []


def test_automatic_selection_calls_router_even_for_one_skill_and_allows_no_match(client, user_headers, fake_model_gateway, monkeypatch):
    from dataclasses import replace
    from app import config
    from test_workflow_jobs import create_version, sandbox_skill_zip
    monkeypatch.setattr(config, 'settings', replace(config.settings, sandbox_worker_enabled=True))
    _, version = create_version(client, user_headers, slug='automatic-choice', package=sandbox_skill_zip())
    selected = client.post('/api/v1/jobs', headers=user_headers, data={'automatic': 'true', 'instruction': 'create a report'})
    assert selected.status_code == 201, selected.text
    assert selected.json()['selected_skills'][0]['skill_version_id'] == version['id']
    assert len(fake_model_gateway.routed_skills) == 1
    async def no_match(**kwargs):
        return ModelResult({'version_ids': []}, 'test', {})
    monkeypatch.setattr(fake_model_gateway, 'route_skills', no_match)
    rejected = client.post('/api/v1/jobs', headers=user_headers, data={'automatic': 'true', 'instruction': 'unrelatedtask'})
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()['detail']['code'] == 'NO_MATCHING_SKILL'


def test_retry_preserves_confirmed_answers_but_resets_attempt_accounting(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob, WorkflowJobMemory, JobStatus
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='clarified-retry', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'create report'}).json()
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        job.status = JobStatus.FAILED
        job.memory.data = {'answers': [{'question': 'unit?', 'answer': 'mm'}], 'context': ['confirmed context'], 'resumed_attempts': 3, 'verification': {'ok': True}}
        db.commit()
    retried = client.post(f"/api/v1/jobs/{created['id']}/retry", headers=user_headers)
    assert retried.status_code == 201, retried.text
    with SessionLocal() as db:
        memory = db.get(WorkflowJob, retried.json()['id']).memory.data
        assert memory['answers'][0]['answer'] == 'mm'
        assert memory['context'] == ['confirmed context']
        assert 'resumed_attempts' not in memory
        assert 'verification' not in memory


def test_mixed_loop_runs_immutable_fixed_entrypoint(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop, AgentNeedsInput
    from app.skill_execution_spec import FixedExecutionSpec
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='mixed-loop', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'calculate and report'}).json()
    sandbox = MemorySandbox()
    contexts = [
        {'name': 'Calculate', 'version': '1', 'root': '/workspace/skills/one', 'skill_md': 'Fixed calculation', 'fixed_execution': FixedExecutionSpec(('python3', 'scripts/calculate.py'), None, 120, 120)},
        {'name': 'Report', 'version': '1', 'root': '/workspace/skills/two', 'skill_md': 'Write report'}]
    for context in contexts:
        context['runtime_requirements'] = {}
    actions = [
        {'action': 'read_skill', 'skill_index': 'bad-index'},
        {'action': 'command', 'argv': 12},
        {'action': 'update_plan', 'steps': 12},
        {'action': 'read_skill', 'skill_index': 1},
        {'action': 'update_plan', 'goal': 'calculate then report', 'steps': [{'id': 'calculate', 'title': 'Calculate', 'status': 'in_progress'}, {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'depends_on': ['calculate']}], 'success_criteria': ['answer 42'], 'validation_step_id': 'verify'},
        {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'not actually executed'},
        {'action': 'run_fixed_skill', 'skill_index': 1},
        {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'fixed result.txt'},
        {'action': 'read_skill', 'skill_index': 2},
        {'action': 'ask_user', 'question': 'Which report title?'}]
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        async def agent_step(self, *, messages):
            action = actions.pop(0)
            if action['action'] == 'run_fixed_skill':
                assert 'FIXED_SKILL_NOT_EXECUTED' in json.dumps(messages)
            return ModelResult(action, 'scripted', {})
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        with pytest.raises(AgentNeedsInput, match='Which report title'):
            asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=Gateway(), job_cancelled=lambda: False))
    assert sandbox.calls == [['python3', 'scripts/calculate.py']]
    assert contexts[0]['fixed_executed']
    contract = json.loads(sandbox.files['/workspace/work/skillgo-job.json'])
    assert contract['skill_root'] == '/workspace/skills/one'


def test_model_transport_retry_is_bounded_and_does_not_retry_bad_arguments(monkeypatch):
    import httpx
    from app import model_adapter
    responses = [429, 503, 200]
    calls = []
    class StreamContext:
        def __init__(self, response): self.response = response
        async def __aenter__(self): return self.response
        async def __aexit__(self, *args): return False
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            calls.append(kwargs['json'])
            response = httpx.Response(responses.pop(0), request=httpx.Request('POST', url), json={'ok': True})
            return StreamContext(response)
    async def no_sleep(seconds): pass
    monkeypatch.setattr(model_adapter.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(model_adapter.asyncio, 'sleep', no_sleep)
    connection = SimpleNamespace(timeout_seconds=30, tls_verify=True, connect_timeout_seconds=15,
                                 first_chunk_timeout_seconds=0, stream_stall_timeout_seconds=0)
    asyncio.run(model_adapter.post_json(connection, 'https://model.example.test', headers={}, body={'model': 'chosen'}))
    assert calls == [{'model': 'chosen', 'stream': True}] * 3
    responses[:] = [400]
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(model_adapter.post_json(connection, 'https://model.example.test', headers={}, body={}))
    assert len(calls) == 4


def test_worker_releases_sandbox_and_resumes_answer_without_spending_crash_budget(client, user_headers, fake_model_gateway, monkeypatch):
    from dataclasses import replace
    from app import config, sandbox_worker
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobStatus, RunStatus
    from test_workflow_jobs import create_version, sandbox_skill_zip
    monkeypatch.setattr(config, 'settings', replace(config.settings, sandbox_worker_enabled=True))
    monkeypatch.setattr(sandbox_worker, 'settings', replace(sandbox_worker.settings, sandbox_worker_max_attempts=1))
    _, version = create_version(client, user_headers, slug='worker-question', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'generate report'}).json()
    class Sandbox(MemorySandbox):
        closed = False
        def __init__(self, *args, **kwargs): super().__init__()
        def __enter__(self): return self
        def __exit__(self, *args): type(self).closed = True
        def put_files(self, files): self.files.update({f'/workspace/{path}': data for path, data in files.items()})
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        def for_model(self, name): return self
        async def agent_step(self, **kwargs):
            return ModelResult({'action': 'ask_user', 'question': 'Which units?'}, 'scripted', {})
    monkeypatch.setattr(sandbox_worker, 'DockerSandbox', Sandbox)
    monkeypatch.setattr(sandbox_worker, 'get_model_gateway', Gateway)
    first = sandbox_worker._claim_job('test-worker')
    assert first.job_id == created['id']
    asyncio.run(sandbox_worker.execute_sandbox_job(first.job_id, object(), lease=first))
    assert Sandbox.closed
    with SessionLocal() as db:
        job = db.get(WorkflowJob, first.job_id)
        assert job.status == JobStatus.WAITING_USER
        assert job.agent_run.status == RunStatus.WAITING_USER
        assert job.agent_run.lease_token is None
        question_id = job.pending_question['id']
    assert sandbox_worker._claim_job('other-worker') is None
    answered = client.post(f'/api/v1/jobs/{first.job_id}/answer', headers=user_headers, json={'question_id': question_id, 'answer': 'mm'})
    assert answered.status_code == 200, answered.text
    second = sandbox_worker._claim_job('test-worker')
    assert second.attempt == 2
    assert second.execution_id != first.execution_id
