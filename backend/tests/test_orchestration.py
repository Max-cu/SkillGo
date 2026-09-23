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


def test_plan_rejects_cycles_and_unready_dependencies_but_warns_on_missing_files():
    state = AgentExecutionState(skill_count=2)
    action = plan()
    action['steps'][0]['depends_on'] = ['report']
    assert state.update_plan(action)['error_code'] == 'PLAN_DEPENDENCY_INVALID'
    action = plan()
    action['steps'][0]['status'] = 'pending'
    assert state.update_plan(action)['error_code'] == 'PLAN_DEPENDENCY_PENDING'
    # Mid-run trust: absent referenced files no longer block the plan update;
    # they come back as a non-fatal warning and are verified at finish.
    result = state.update_plan(plan(), files={})
    assert result['ok'] is True
    assert result['warnings'][0]['code'] == 'PLAN_FILE_PENDING'


def test_input_change_invalidates_step_and_descendants_only():
    state = AgentExecutionState(skill_count=2, completed_skill_indexes={1, 2})
    files = {'/workspace/input/data.txt': 'a', '/workspace/output/result.txt': 'b', '/workspace/output/report.txt': 'c'}
    assert state.update_plan(plan(), files=files)['ok']
    state.invalidate_changed_steps({**files, '/workspace/input/data.txt': 'changed'})
    assert all(step['status'] == 'pending' for step in state.plan['steps'])
    assert state.completed_skill_indexes == set()


def test_status_only_replan_preserves_dependency_and_file_contracts():
    state = AgentExecutionState(skill_count=2)
    files = {'/workspace/input/data.txt': 'a', '/workspace/output/result.txt': 'b', '/workspace/output/report.txt': 'c'}
    assert state.update_plan(plan(), files=files)['ok']
    revised = plan()
    for step in revised['steps']:
        for key in ('depends_on', 'input_refs', 'output_refs', 'skill_index'):
            step.pop(key, None)
    assert state.update_plan(revised, files=files)['ok']
    assert state.plan['steps'][1]['depends_on'] == ['compute']
    assert state.plan['steps'][0]['skill_index'] == 1
    # Files absent on a status-only replan warn instead of blocking.
    assert state.update_plan(revised, files={})['ok'] is True
    state.invalidate_changed_steps({**files, '/workspace/input/data.txt': 'changed'})
    assert all(step['status'] == 'pending' for step in state.plan['steps'])


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


@pytest.mark.parametrize('tool_limit', [0, 480])
def test_agent_loop_executes_platform_verifier_before_finish(client, user_headers, fake_model_gateway, monkeypatch, tool_limit):
    from dataclasses import replace
    from app import sandbox_agent_loop
    monkeypatch.setattr(sandbox_agent_loop, 'settings', replace(sandbox_agent_loop.settings, sandbox_max_agent_tool_calls=tool_limit))
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


@pytest.mark.parametrize("legacy_registration", [False, True])
@pytest.mark.parametrize("tamper_before_finish", [False, True])
def test_agent_loop_syncs_verification_plan_and_finishes_without_replan(client, user_headers, fake_model_gateway, legacy_registration, tamper_before_finish):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='orchestration-auto-sync', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()
    class FinishRejected(Exception):
        pass

    class ScriptedGateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            if self.turn == 3:
                proof = next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "run_verifier"'))
                assert proof['validation_recorded'] is True
                assert proof['validation_step_completed'] is True
            step = self.turn if legacy_registration or self.turn < 3 else self.turn + 1
            if step == 6 and tamper_before_finish:
                rejected = next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "finish"'))
                assert rejected['ok'] is False
                assert rejected['error_code'] == 'AGENT_PLAN_INCOMPLETE'
                raise FinishRejected()
            steps = [{'id': 'make', 'title': 'Make', 'status': 'completed', 'evidence': 'result file exists', 'output_refs': ['/workspace/output/result.txt']}, {'id': 'verify', 'title': 'Verify', 'status': 'in_progress', 'evidence': '', 'depends_on': ['make']}]
            base = {'action': 'update_plan', 'goal': 'answer 42', 'steps': steps, 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            if step == 1:
                action = base
            elif step == 2:
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif step == 3:
                proof = next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "run_verifier"'))
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif step == 4:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert step == 5
                if tamper_before_finish:
                    sandbox.files['/workspace/output/result.txt'] = b'43'
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        class CountingSandbox(MemorySandbox):
            downloads = 0
            def download_file(self, path):
                self.downloads += 1
                return self.read_workspace_file(path)
        sandbox = CountingSandbox()
        if tamper_before_finish:
            with pytest.raises(FinishRejected):
                asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
            return
        result = asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']
        assert result[2] == (5 if legacy_registration else 4)
        assert sandbox.downloads == (4 if legacy_registration else 3)
        assert job.execution_plan['steps'][0]['status'] == 'completed'

        assert job.execution_plan['steps'][-1]['status'] == 'completed'
        assert json.loads(sandbox.files['/workspace/work/skillgo-plan.json'])['steps'][-1]['status'] == 'completed'


def test_agent_loop_auto_activates_unique_ready_step_without_rejecting_work(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='orchestration-auto-activate', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()

    class ScriptedGateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            base = {'action': 'update_plan', 'goal': 'answer 42', 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            if self.turn == 1:
                # No step marked in_progress — the model starts work right away.
                steps = [
                    {'id': 'make', 'title': 'Make', 'status': 'pending', 'evidence': '', 'output_refs': ['/workspace/output/result.txt']},
                    {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'evidence': '', 'depends_on': ['make']},
                ]
                action = {**base, 'steps': steps}
            elif self.turn == 2:
                action = {'action': 'command', 'argv': ['python3', 'build.py']}
            elif self.turn == 3:
                # The mutating call must have run (no SANDBOX_ACTION_INVALID)
                # and make must now show in_progress in the state memory.
                payloads = [json.loads(item['content'])['payload'] for item in messages
                            if isinstance(item.get('content'), str) and '"tool_result"' in item['content']]
                assert not any(p.get('error_code') == 'SANDBOX_ACTION_INVALID' for p in payloads)
                assert any(p.get('exit_code') == 0 for p in payloads)
                steps = [
                    {'id': 'make', 'title': 'Make', 'status': 'completed', 'evidence': 'result file exists', 'output_refs': ['/workspace/output/result.txt']},
                    {'id': 'verify', 'title': 'Verify', 'status': 'in_progress', 'evidence': '', 'depends_on': ['make']},
                ]
                action = {**base, 'steps': steps}
            elif self.turn == 4:
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif self.turn == 5:
                proof = next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "run_verifier"'))
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif self.turn == 6:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert self.turn == 7
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})

    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        sandbox = MemorySandbox()
        result = asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']
        assert result[2] == 7
        # The gated command actually executed instead of being rejected.
        assert ['python3', 'build.py'] in sandbox.calls
        # A neutral status event (not a red failed tool event) recorded it.
        assert any(event.title == '自动激活计划步骤' for event in job.events)
        assert not any(event.event_type == 'tool' and event.status == 'failed' for event in job.events)
        assert job.execution_plan['steps'][0]['status'] == 'completed'


def test_agent_loop_allows_pre_plan_binary_inspection_but_blocks_writes(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='orchestration-preplan-inspect', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()

    class ScriptedGateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            base = {'action': 'update_plan', 'goal': 'answer 42', 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            if self.turn == 1:
                # No plan yet: binary-input inspection via run_python must run.
                action = {'action': 'run_python', 'code': "print('pdf structure: 19 pages')"}
            elif self.turn == 2:
                payloads = [json.loads(item['content'])['payload'] for item in messages
                            if isinstance(item.get('content'), str) and '"tool_result"' in item['content']]
                inspection = next(p for p in payloads if p.get('script_path'))
                assert inspection.get('exit_code') == 0
                assert 'update_plan' in inspection.get('hint', '')
                # Producing files before a plan is still blocked.
                action = {'action': 'write_file', 'path': '/workspace/work/premature.txt', 'content': 'x'}
            elif self.turn == 3:
                payloads = [json.loads(item['content'])['payload'] for item in messages
                            if isinstance(item.get('content'), str) and '"tool_result"' in item['content']]
                blocked = next(p for p in payloads if p.get('error_code') == 'SANDBOX_ACTION_INVALID')
                assert 'plan' in blocked['message']
                steps = [{'id': 'make', 'title': 'Make', 'status': 'in_progress', 'evidence': '', 'output_refs': ['/workspace/output/result.txt']}, {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'evidence': '', 'depends_on': ['make']}]
                action = {**base, 'steps': steps}
            elif self.turn == 4:
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif self.turn == 5:
                proof = next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "run_verifier"'))
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif self.turn == 6:
                steps = [
                    {'id': 'make', 'title': 'Make', 'status': 'completed', 'evidence': 'result file exists', 'output_refs': ['/workspace/output/result.txt']},
                    {'id': 'verify', 'title': 'Verify', 'status': 'completed', 'evidence': 'verified', 'depends_on': ['make']},
                ]
                action = {**base, 'steps': steps}
            elif self.turn == 7:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert self.turn == 8
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})

    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        sandbox = MemorySandbox()
        result = asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']
        assert result[2] == 8
        # The pre-plan inspection really executed (python3 ran in sandbox).
        assert any(argv and argv[0] == 'python3' for argv in sandbox.calls)
        # The premature write was refused and never reached the sandbox.
        assert '/workspace/work/premature.txt' not in sandbox.files


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
                payload = last_payload(messages, 'read_file')
                # Immutable input reads are pinned via the reference shelf.
                assert payload['ok'] and payload['retained'] and payload['content'] == '21'
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


def test_pinned_reference_pagination_is_served_without_a_second_sandbox_read(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='shelf-pagination', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()

    spec = '# Spec\n' + '规范条目 ' * 1500  # >4KB when serialized, <20KB file

    class CountingSandbox(MemorySandbox):
        def __init__(self):
            super().__init__()
            self.read_count = 0
            self.files['/workspace/skill/references/spec.md'] = spec.encode()
        async def read_text(self, path, offset=0, limit=30000):
            self.read_count += 1
            return await super().read_text(path, offset=offset, limit=limit)

    def last_payload(messages, tool):
        return next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "%s"' % tool))

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            steps = [{'id': 'make', 'title': 'Make', 'status': 'in_progress', 'evidence': '', 'output_refs': ['/workspace/output/result.txt']},
                     {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'evidence': '', 'depends_on': ['make']}]
            base = {'action': 'update_plan', 'goal': 'answer 42', 'steps': steps, 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            spec_path = '/workspace/skill/references/spec.md'
            if self.turn == 1:
                action = base
            elif self.turn == 2:
                # First read with explicit offset/limit (as models actually do).
                action = {'action': 'read_file', 'path': spec_path, 'offset': 0, 'limit': 30000, 'reason': 'load spec'}
            elif self.turn == 3:
                pinned = last_payload(messages, 'read_file')
                assert pinned['reference'] and pinned['retained'] and pinned['content'] == spec
                # Paginated re-read the old code performed against the sandbox.
                action = {'action': 'read_file', 'path': spec_path, 'offset': 2000, 'limit': 30000, 'reason': 'later slice'}
            elif self.turn == 4:
                cached = last_payload(messages, 'read_file')
                assert cached['reference'] and cached['cached'] and cached['unchanged']
                assert cached['content'] == spec[2000:32000]
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif self.turn == 5:
                proof = last_payload(messages, 'run_verifier')
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif self.turn == 6:
                for step in steps:
                    step.update(status='completed', evidence='verified output')
                action = base
            elif self.turn == 7:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert self.turn == 8
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})

    sandbox = CountingSandbox()
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        result = asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=Gateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']
    # Exactly one sandbox read: the pin. The slice was served from the shelf.
    assert sandbox.read_count == 1


def test_command_timeout_is_recoverable_and_agent_resumes_with_sandbox_intact(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client, user_headers, slug='timeout-resume', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers, data={'version_id': version['id'], 'instruction': 'answer 42'}).json()

    class ResumableTimeoutSandbox(MemorySandbox):
        def __init__(self):
            super().__init__()
            self.batch_calls = 0
            self.timeouts_seen = []
        async def command(self, argv, **kwargs):
            if 'run_task.py' in ' '.join(argv):
                self.timeouts_seen.append(kwargs.get('timeout_seconds'))
                self.batch_calls += 1
                if self.batch_calls == 1:
                    return SandboxCommandResult(124, 'batch progress: 3/11 files done', '', timed_out=True)
                return SandboxCommandResult(0, json.dumps({'resumed': True, 'finished': 11}), '')
            return await super().command(argv, **kwargs)

    def last_payload(messages, tool):
        return next(json.loads(item['content'])['payload'] for item in reversed(messages) if isinstance(item.get('content'), str) and item['content'].startswith('{"tool_result": "%s"' % tool))

    class ScriptedGateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self):
            self.turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            steps = [{'id': 'make', 'title': 'Make', 'status': 'in_progress', 'evidence': '', 'output_refs': ['/workspace/output/result.txt']},
                     {'id': 'verify', 'title': 'Verify', 'status': 'pending', 'evidence': '', 'depends_on': ['make']}]
            base = {'action': 'update_plan', 'goal': 'answer 42', 'steps': steps, 'success_criteria': ['answer is 42'], 'validation_step_id': 'verify'}
            batch = {'action': 'command', 'argv': ['python3', 'scripts/run_task.py', 'run'],
                     'cwd': '/workspace/skill', 'timeout_seconds': 900, 'reason': 'resumable batch'}
            if self.turn == 1:
                action = base
            elif self.turn == 2:
                action = batch
            elif self.turn == 3:
                payload = last_payload(messages, 'command')
                assert payload['ok'] is False and payload['error_code'] == 'SANDBOX_COMMAND_TIMEOUT'
                assert '3/11 files' in payload['stdout'] and 'resume' in payload['hint'].lower()
                action = dict(batch, reason='resume batch from saved state')
            elif self.turn == 4:
                assert last_payload(messages, 'command')['exit_code'] == 0
                action = {'action': 'run_verifier', 'argv': ['verify']}
            elif self.turn == 5:
                proof = last_payload(messages, 'run_verifier')
                self.proof_id = proof['verification_id']
                assert proof['ok']
                action = {'action': 'record_validation', 'verification_id': self.proof_id, 'status': 'passed', 'summary': 'verified', 'evidence': 'observed 42', 'checks': ['answer 42']}
            elif self.turn == 6:
                for step in steps:
                    step.update(status='completed', evidence='verified output')
                action = base
            elif self.turn == 7:
                action = {'action': 'complete_skill', 'skill_index': 1, 'evidence': 'verified output'}
            else:
                assert self.turn == 8
                action = {'action': 'finish', 'summary': 'answer 42', 'artifacts': ['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})

    sandbox = ResumableTimeoutSandbox()
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        contexts = [{'name': 'Test', 'version': '1', 'root': '/workspace/skill', 'skill_md': '# Answer', 'runtime_requirements': {}}]
        result = asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=contexts, gateway=ScriptedGateway(), job_cancelled=lambda: False))
        assert result[1] == ['/workspace/output/result.txt']
    # The batch was attempted, timed out once, and resumed — sandbox never died.
    assert sandbox.batch_calls == 2
    assert sandbox.timeouts_seen == [900, 900]
    assert sandbox.files['/workspace/output/result.txt'] == b'42'


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
    async def environment(*args): return {'schema_version': 1, 'capabilities': [], 'providers': {}}
    monkeypatch.setattr(sandbox_worker, 'preflight_environment', environment)
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

@pytest.mark.parametrize('code', ['SANDBOX_ARTIFACT_SIZE', 'SANDBOX_ARTIFACT_INVALID'])
def test_artifact_failure_is_recoverable_and_repair_can_be_verified(code):
    class BrokenSandbox(MemorySandbox):
        broken = True
        def download_file(self, path):
            if self.broken:
                raise SandboxRuntimeError(code, f'{path}; size_bytes=101; limit_bytes=100')
            return super().download_file(path)
    sandbox = BrokenSandbox()
    proof = asyncio.run(run_verifier(sandbox, {'argv': ['verify']}, requirements=['correct']))
    assert proof['ok'] is False
    assert proof['error_code'] == 'VERIFIER_FAILED'
    assert '/workspace/output/result.txt' in proof['message']
    assert 'size_bytes=101' in proof['message']
    assert proof['full_result_path'] in sandbox.files
    sandbox.broken = False
    repaired = asyncio.run(run_verifier(sandbox, {'argv': ['verify']}, requirements=['correct']))
    assert repaired['ok'] is True
    assert repaired['verification_id'] != proof['verification_id']


def test_plan_snapshot_reads_overlapping_refs_once_but_refreshes_next_snapshot():
    from collections import Counter
    from app.workflow_tools import snapshot_step_files
    class Sandbox(MemorySandbox):
        def __init__(self):
            super().__init__()
            self.reads = Counter()
        async def list_files(self, path):
            return await super().list_files(path) + [
                {'path': '/workspace/output', 'type': 'directory'},
            ]
        def read_workspace_file(self, path):
            self.reads[path] += 1
            return super().read_workspace_file(path)
    sandbox = Sandbox()
    refs = ['/workspace/output', '/workspace/output/result.txt']
    first = asyncio.run(snapshot_step_files(sandbox, refs))
    assert sandbox.reads['/workspace/output/result.txt'] == 1
    sandbox.files['/workspace/output/result.txt'] = b'43'
    second = asyncio.run(snapshot_step_files(sandbox, refs))
    assert sandbox.reads['/workspace/output/result.txt'] == 2
    assert all(first[path] != second[path] for path in refs)
