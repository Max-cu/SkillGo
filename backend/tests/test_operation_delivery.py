import asyncio
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from jsonschema import validate

from app.agent_policy import AgentExecutionState
from app.durable_checkpoint import state_to_json
from app.model_gateway import AgentToolCall, ModelResult, SANDBOX_AGENT_TOOLS
from app.operation_delivery import FINGERPRINT_PROGRAM
from app.sandbox_agent_loop import AgentJobCancelled, _run_agent_loop
from app.sandbox_runtime import SandboxCommandResult
from app.sandbox_tool_registry import validate_agent_action
from test_artifact_collection import Sandbox as FileSandbox
from test_skill_phase_contracts import ROOT, context


def delivery(path):
    return {'summary': 'Requested report completed', 'artifacts': [path]}


@pytest.mark.parametrize('name,fields', [
    ('command', {'argv': ['python3', 'make.py'], 'cwd': ROOT, 'timeout_seconds': 60}),
    ('run_python', {'code': 'print("generate")', 'cwd': ROOT}),
    ('run_commands', {'commands': [{'argv': ['python3', 'make.py'], 'cwd': ROOT, 'timeout_seconds': 60}]}),
    ('run_skill_workflow', {'skill_index': 1, 'workflow_id': 'steps-1'}),
    ('write_file', {'path': '/workspace/output/report.md', 'content': 'Report'}),
    ('write_json', {'path': '/workspace/output/report.json', 'data': {'answer': 42}}),
])
def test_final_delivery_schema_and_dispatch_validation(name, fields):
    action = {'action': name, **fields, 'reason': 'Final work',
              'delivery': delivery('/workspace/output/report.csv')}
    schema = next(t['function']['parameters'] for t in SANDBOX_AGENT_TOOLS
                  if t['function']['name'] == name)
    validate({k: v for k, v in action.items() if k != 'action'}, schema)
    assert validate_agent_action(action) is None


@pytest.mark.parametrize('paths', [[], ['/etc/passwd'], ['/workspace/../report'],
    ['/workspace'], ['/workspace/output/*.csv'], ['/workspace/output/a', '/workspace/output/./a']])
def test_final_delivery_rejects_ambiguous_or_unsafe_paths(paths):
    action = {'action': 'command', 'argv': ['python3', 'make.py'],
              'delivery': {'summary': 'Done', 'artifacts': paths}}
    assert validate_agent_action(action)


def test_read_or_verifier_cannot_claim_final_generation():
    for action in [{'action': 'read_file', 'path': '/workspace/a'},
                   {'action': 'run_verifier', 'argv': ['python3', 'verify.py']}]:
        assert validate_agent_action({**action, 'delivery': delivery('/workspace/a')})


@pytest.mark.parametrize('mode', ['success_csv', 'success_docx', 'success_image', 'success_zip',
    'legacy', 'command', 'python', 'write_file', 'write_json', 'workflow', 'overwrite',
    'failed_check', 'empty', 'missing', 'unchanged', 'baseline_unavailable',
    'changed_during_collection', 'full_verification', 'cancel', 'plan_blocked', 'multiple_native'])
def test_generic_final_group_skips_finish_only_after_real_success(client, user_headers, monkeypatch, mode):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app import sandbox_agent_loop as loop
    from test_workflow_jobs import create_version, sandbox_skill_zip

    # An arbitrary newly uploaded version: no special recipe or version allowlist.
    _, version = create_version(client, user_headers, slug='generic-final', package=sandbox_skill_zip())
    jid = client.post('/api/v1/jobs', headers=user_headers,
                     data={'version_id': version['id'], 'instruction': 'Create a report'}).json()['id']
    monkeypatch.setattr(loop, 'delivery_only_job', lambda job: mode != 'full_verification')
    extension = {'success_docx': '.docx', 'success_image': '.png', 'success_zip': '.zip',
                 'write_file': '.md', 'write_json': '.json'}.get(mode, '.csv')
    source = ROOT + '/exports/result' + extension
    state = AgentExecutionState(skill_count=1, loaded_skills={1}, plan_status_tracking=False)
    state.plan_required = False
    if mode == 'plan_blocked':
        state.plan_status_tracking = True
        assert state.update_plan({'goal': 'Report', 'steps': [
            {'id': 'make', 'title': 'Create', 'status': 'in_progress'},
            {'id': 'review', 'title': 'Required review', 'status': 'pending'},
            {'id': 'check', 'title': 'Verify', 'status': 'pending'}],
            'success_criteria': ['Report ready'], 'validation_step_id': 'check'})['ok']
    cancelled = False

    class Sandbox(FileSandbox):
        def __init__(self):
            super().__init__({ROOT + '/exports/unrelated.csv': b'old unrelated',
                **({source: b'old'} if mode in {'overwrite', 'unchanged'} else {})})
            self.commands = []
            self.durable_resume = {'versions': [version['id']], 'model_name': '',
                'messages': [{'role': 'system', 'content': 'Rules'}, {'role': 'user', 'content': 'Report'}],
                'execution_state': state_to_json(state), 'next_turn': 1, 'repeated_action': '',
                'repeat_count': 0, 'recoverable_errors': 0, 'tool_operation_count': 0,
                'singleton_tool_turns': 0, 'fixed_executed': [False], 'memory': {}}

        async def read_text_window(self, path, **kwargs):
            return {'ok': True, 'path': path, 'content': 'Required checks', 'complete': True,
                    'eof': True, 'total_chars': 15, 'returned_chars': 15, 'offset': 0, 'limit': 30000}

        async def command(self, argv, **kwargs):
            nonlocal cancelled
            if len(argv) > 2 and argv[2] == FINGERPRINT_PROGRAM:
                if mode == 'baseline_unavailable':
                    return SandboxCommandResult(1, '', 'Could not read baseline')
                return SandboxCommandResult(0, json.dumps({'ok': True, 'files': {
                    p: ({'size_bytes': len(self.files[p]), 'sha256': hashlib.sha256(self.files[p]).hexdigest()}
                        if p in self.files else None) for p in json.loads(argv[3])}}), '')
            if argv[1] == '-c':
                if mode == 'changed_during_collection':
                    self.files[source] = b'mutated after fingerprint'
                return await super().command(argv, **kwargs)
            self.commands.append(argv[1])
            if 'make' in argv[1] or argv[1].startswith('/workspace/work/agent-'):
                if mode not in {'unchanged', 'missing'}:
                    self.files[source] = b'' if mode == 'empty' else b'new requested content'
            if argv[1].endswith('verify.py') and mode == 'failed_check':
                return SandboxCommandResult(1, '', 'Required check failed')
            if argv[1].endswith('cleanup.py') and mode == 'cancel':
                cancelled = True
            return SandboxCommandResult(0, 'OK', '')

    class FallbackReached(Exception):
        pass

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        count = 0
        async def agent_step(self, **kwargs):
            self.count += 1
            if self.count > 1:
                # A failed final group must keep a valid native conversation;
                # never emit a fake finish reply or omit its pending tool result.
                if mode != 'legacy':
                    ids = [m['tool_call_id'] for m in kwargs['messages'] if m.get('role') == 'tool']
                    assert ids == (['final', 'other'] if mode == 'multiple_native' else ['final'])
                raise FallbackReached
            action = {'action': 'run_commands', 'reason': 'Create, check and clean',
                'commands': [{'argv': ['python3', name], 'cwd': ROOT}
                             for name in ['make.py', 'verify.py', 'cleanup.py']]}
            if mode == 'command':
                action = {'action': 'command', 'argv': ['python3', 'make.py'], 'cwd': ROOT}
            if mode == 'python':
                action = {'action': 'run_python', 'code': 'generate()', 'cwd': ROOT}
            if mode == 'write_file':
                action = {'action': 'write_file', 'path': source, 'content': 'Final report'}
            if mode == 'write_json':
                action = {'action': 'write_json', 'path': source, 'data': {'result': 42}}
            if mode == 'workflow':
                action = {'action': 'run_skill_workflow', 'skill_index': 1, 'workflow_id': 'steps-3-3'}
            action['delivery'] = delivery(source)
            if mode == 'legacy':
                return ModelResult(action, 'test', {})
            actions = [('final', action)]
            if mode == 'multiple_native':
                actions.append(('other', {'action': 'command', 'argv': ['python3', 'make.py'], 'cwd': ROOT}))
            return ModelResult(action, 'test', {}, tool_calls=tuple(AgentToolCall(i, a) for i, a in actions),
                assistant_message={'role': 'assistant', 'tool_calls': [
                    {'id': i, 'type': 'function', 'function': {'name': a['action'],
                        'arguments': json.dumps({k: v for k, v in a.items() if k != 'action'})}}
                    for i, a in actions]})

    guide = '# Report workflow\nRun all required checks before delivery.'
    if mode == 'workflow':
        guide = '''### Step 3: make.py
加载 `references/final.md`。
| 子步骤 | 命令 |
| 3a | `python scripts/make.py` |
| 3b | `python scripts/verify.py` |
| 3c | `python scripts/cleanup.py` |
'''
    success = mode in {'success_csv', 'success_docx', 'success_image', 'success_zip', 'legacy',
                      'command', 'python', 'write_file', 'write_json', 'workflow', 'overwrite'}
    with SessionLocal() as db:
        job = db.get(WorkflowJob, jid)
        job.follow_skill_flow = True
        sandbox = Sandbox()
        sandbox.durable_resume['model_name'] = job.model_name
        gateway = Gateway()
        operation = _run_agent_loop(db, job, sandbox, skill_contexts=[context(guide)], gateway=gateway,
                                    job_cancelled=lambda: cancelled)
        if success:
            summary, paths, _, _ = asyncio.run(operation)
            assert gateway.count == 1 and summary == delivery(source)['summary']
            assert len(paths) == 1 and sandbox.files[paths[0]] == sandbox.files[source]
            assert any(e.data.get('tool') == 'operation_auto_delivery' for e in job.events)
            assert all(not p.endswith('unrelated.csv') for p in paths)
        else:
            with pytest.raises(AgentJobCancelled if mode == 'cancel' else FallbackReached):
                asyncio.run(operation)
            assert not any(e.data.get('tool') == 'operation_auto_delivery' for e in job.events)
            if mode == 'failed_check':
                assert sandbox.commands == ['make.py', 'verify.py']
            if mode in {'full_verification', 'multiple_native'}:
                assert sandbox.commands == []


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX directory descriptor semantics')
def test_real_fingerprint_program_missing_changed_symlinks_and_empty(tmp_path):
    program = FINGERPRINT_PROGRAM.replace("os.open('/workspace',", f'os.open({str(tmp_path)!r},')
    paths = ['/workspace/result.csv']
    def run(limit=100):
        p = subprocess.run([sys.executable, '-c', program, json.dumps(paths), str(limit)],
                           capture_output=True, text=True, check=True)
        return json.loads(p.stdout)
    assert run()['files'][paths[0]] is None
    target = tmp_path/'result.csv'
    target.write_bytes(b'content')
    assert run()['files'][paths[0]]['sha256'] == hashlib.sha256(b'content').hexdigest()
    assert run(2)['ok'] is False
    target.write_bytes(b'')
    assert run()['files'][paths[0]]['size_bytes'] == 0
    target.unlink()
    target.symlink_to(tmp_path/'outside')
    assert run()['ok'] is False
    paths[:] = ['/workspace/link/result.csv']
    (tmp_path/'link').symlink_to(tmp_path, target_is_directory=True)
    assert run()['ok'] is False
