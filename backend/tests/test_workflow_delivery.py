import asyncio
import hashlib
import json
from types import SimpleNamespace
from dataclasses import replace

import pytest

from app.agent_policy import AgentExecutionState
from app.durable_checkpoint import state_to_json
from app.model_gateway import ModelResult
from app.sandbox_agent_loop import _run_agent_loop, AgentJobCancelled
from app.sandbox_runtime import SandboxCommandResult
from app.skill_phase_contracts import stage_contracts
from app.workflow_delivery import (SUCCESS_PREFIX, BASELINE_PROGRAM, FINAL_COMMANDS, EXPORT_COMMAND,
    configured_export_command, capture_export, receive_workflow_delivery)
from test_artifact_collection import Sandbox as DeliverySandbox
from test_skill_phase_contracts import ROOT, context

GUIDE = """### Step 3: build_svgs.py
加载 `references/build.md`。
| 子步骤 | 命令 |
| 3a | `python scripts/validators/check_templates.py` |
| 3b | `python scripts/cleaners/clear_downstream.py` |
| 3c | `python scripts/build_svgs.py` |
| 3d | `python scripts/validators/verify_build_output.py` |
### Step 4: finalize_svg.py
加载 `references/finalize.md`。
| 子步骤 | 命令 |
| 4a | `python scripts/finalize_svg.py` |
| 4b | `python scripts/validators/verify_finalize_output.py` |
### Step 5: export_flat.py
加载 `references/export.md`。
| 子步骤 | 命令 |
| 5a | `python scripts/export_flat.py` |
| 5b | `python scripts/validators/verify_export_output.py` |
| 5c | `python scripts/cleaners/clear_downstream.py` |
"""
SOURCE = ROOT + '/exports/report.pptx'
CONTENT = b'current export bytes'


def declaration(path=SOURCE, content=CONTENT):
    return [{'path': path, 'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}]


def test_adapter_requires_explicit_version_and_exact_full_recipe():
    recipe = stage_contracts(context(GUIDE))['workflows']['steps-3-5']
    assert recipe['commands'] == FINAL_COMMANDS
    assert configured_export_command('reviewed', ('reviewed',), recipe) == EXPORT_COMMAND
    assert configured_export_command('unknown', ('reviewed',), recipe) is None
    assert configured_export_command('reviewed', (), recipe) is None
    assert configured_export_command('reviewed', ('reviewed',), {**recipe, 'commands': FINAL_COMMANDS[:-1]}) is None
    assert configured_export_command('reviewed', ('reviewed',), {**recipe, 'stages': [5]}) is None


@pytest.mark.parametrize('case', ['valid', 'old', 'unrelated', 'failed', 'missing', 'ambiguous', 'relative', 'no_baseline', 'no_signal'])
def test_export_capture_requires_current_success_path_and_new_file(case):
    sandbox = DeliverySandbox({SOURCE: CONTENT, ROOT + '/exports/old.pptx': b'old'})
    baseline = {'old.pptx'}
    path = SOURCE
    payload = {'exit_code': 0, 'stdout': SUCCESS_PREFIX + path}
    if case == 'old': baseline.add('report.pptx')
    if case == 'unrelated': payload['stdout'] = SUCCESS_PREFIX + '/workspace/unrelated/report.pptx'
    if case == 'failed': payload['exit_code'] = 1
    if case == 'missing': del sandbox.files[SOURCE]
    if case == 'ambiguous': payload['stdout'] += '\n' + payload['stdout']
    if case == 'relative': payload['stdout'] = SUCCESS_PREFIX + 'exports/report.pptx'
    if case == 'no_baseline': baseline = None
    if case == 'no_signal': payload['stdout'] = 'A PPT file exists'
    result = asyncio.run(capture_export(sandbox, payload, baseline, cwd=ROOT))
    assert bool(result) is (case == 'valid')


@pytest.mark.parametrize('case', ['valid', 'old_file', 'changed', 'missing', 'bad_path', 'size'])
def test_collection_only_accepts_signalled_current_bytes(case):
    files = {SOURCE: CONTENT, ROOT + '/exports/old.pptx': b'old PPT'}
    declared = declaration()
    if case == 'changed': files[SOURCE] = b'changed after export'
    if case == 'missing': del files[SOURCE]
    if case == 'bad_path': declared[0]['path'] = '/etc/passwd'
    if case == 'size': declared[0]['size_bytes'] += 1
    if case == 'old_file':
        del files[SOURCE]
    result = asyncio.run(receive_workflow_delivery(DeliverySandbox(files), declared, cwd=ROOT))
    assert result.get('ok') is (case == 'valid')
    if case == 'valid':
        assert len(result['artifacts']) == 1
        assert result['sources'] == [SOURCE]


@pytest.mark.parametrize('mode', ['success', 'no_signal', 'cleanup_failure', 'changed', 'cancel', 'undeclared', 'full_verification', 'unrecognized_recipe'])
def test_final_workflow_delivery_skips_model_finish_only_on_success(client, user_headers, monkeypatch, mode):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version, sandbox_skill_zip
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: mode != 'full_verification')
    _, version = create_version(client, user_headers, slug='auto-delivery', package=sandbox_skill_zip())
    jid = client.post('/api/v1/jobs', headers=user_headers, data={
        'version_id': version['id'], 'instruction': 'Create report'}).json()['id']
    guide = GUIDE + ('\n| 5d | `python scripts/extra.py` |\n' if mode == 'unrecognized_recipe' else '')
    from app import sandbox_agent_loop
    monkeypatch.setattr(sandbox_agent_loop, 'settings', replace(sandbox_agent_loop.settings,
        workflow_auto_delivery_version_ids=() if mode == 'undeclared' else (version['id'],)))
    state = AgentExecutionState(skill_count=1, loaded_skills={1}, plan_status_tracking=False)
    assert state.update_plan({'goal': 'Create report', 'steps': [
        {'id': 'make', 'title': 'Create'}, {'id': 'check', 'title': 'Check'}],
        'validation_step_id': 'check', 'success_criteria': ['Report ready']})['ok']
    cancelled = False

    class Sandbox(DeliverySandbox):
        def __init__(self):
            super().__init__({ROOT + '/exports/old.pptx': b'old unrelated PPT'})
            self.durable_resume = {'versions': [version['id']], 'model_name': '',
                'messages': [{'role': 'system', 'content': 'Rules'}, {'role': 'user', 'content': 'Create report'}],
                'execution_state': state_to_json(state), 'next_turn': 1, 'repeated_action': '', 'repeat_count': 0,
                'recoverable_errors': 0, 'tool_operation_count': 0, 'singleton_tool_turns': 0,
                'fixed_executed': [False], 'memory': {}}
            self.commands = []

        async def read_text_window(self, path, *, offset=0, limit=30000):
            return {'ok': True, 'path': path, 'content': 'Complete reference', 'complete': True,
                    'eof': True, 'total_chars': 18, 'returned_chars': 18, 'offset': 0, 'limit': limit}

        async def command(self, argv, **kwargs):
            nonlocal cancelled
            if argv[1] == '-c':
                if argv[2] == BASELINE_PROGRAM:
                    names = [p.rsplit('/', 1)[1] for p in self.files if p.startswith(ROOT + '/exports/')]
                    return SandboxCommandResult(0, json.dumps({'files': names}), '')
                return await super().command(argv, **kwargs)
            self.commands.append(argv[1])
            if argv[1] == 'scripts/export_flat.py':
                self.files[SOURCE] = CONTENT
                return SandboxCommandResult(0, 'no declaration' if mode == 'no_signal' else SUCCESS_PREFIX + SOURCE, '')
            if argv[1] == 'scripts/cleaners/clear_downstream.py' and SOURCE in self.files:
                if mode == 'changed': self.files[SOURCE] = b'modified'
                if mode == 'cancel': cancelled = True
                if mode == 'cleanup_failure': return SandboxCommandResult(1, '', 'Final check failed')
            return SandboxCommandResult(0, 'OK', '')

    class FallbackReached(Exception): pass
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        count = 0
        async def agent_step(self, **kwargs):
            self.count += 1
            if self.count > 1:
                assert mode not in {'success', 'cancel'}, 'Unexpected extra model call'
                raise FallbackReached
            return ModelResult({'action': 'run_skill_workflow', 'skill_index': 1,
                                'workflow_id': 'steps-3-5', 'reason': 'Final workflow'}, 'test', {})

    with SessionLocal() as db:
        job = db.get(WorkflowJob, jid)
        sandbox = Sandbox()
        sandbox.durable_resume['model_name'] = job.model_name
        gateway = Gateway()
        run = _run_agent_loop(db, job, sandbox, skill_contexts=[context(guide)],
                              gateway=gateway, job_cancelled=lambda: cancelled)
        if mode == 'success':
            result = asyncio.run(run)
            assert gateway.count == 1
            assert len(result[1]) == 1 and sandbox.files[result[1][0]] == CONTENT
            assert sandbox.commands[-1] == 'scripts/cleaners/clear_downstream.py'
            assert any(e.data.get('tool') == 'workflow_auto_delivery' for e in job.events)
        else:
            with pytest.raises(AgentJobCancelled if mode == 'cancel' else FallbackReached):
                asyncio.run(run)
            assert not any(e.data.get('tool') == 'workflow_auto_delivery' for e in job.events)


@pytest.mark.skipif(__import__('sys').platform == 'win32', reason='Linux directory-descriptor semantics')
def test_real_baseline_handles_absent_directory_and_rejects_symlink(tmp_path):
    import subprocess
    import sys
    exports = tmp_path / 'exports'
    def run():
        return subprocess.run([sys.executable, '-c', BASELINE_PROGRAM, str(exports)], capture_output=True, text=True)
    absent = run()
    assert absent.returncode == 0 and json.loads(absent.stdout) == {'files': []}
    exports.mkdir()
    (exports / 'old.pptx').write_bytes(b'old')
    assert json.loads(run().stdout) == {'files': ['old.pptx']}
    (exports / 'old.pptx').unlink()
    exports.rmdir()
    exports.symlink_to(tmp_path, target_is_directory=True)
    assert run().returncode != 0
