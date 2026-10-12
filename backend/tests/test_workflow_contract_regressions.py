from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from app.agent_policy import AgentExecutionState, PRE_PLAN_INSPECTION_HINT
from app.model_gateway import ModelResult, SANDBOX_AGENT_TOOLS
from app.sandbox_agent_loop import _agent_messages, _read_file_payload, _run_agent_loop
from app.sandbox_runtime import DockerSandbox, SandboxCommandResult, SandboxRuntimeError
from app.workflow_tools import _validated_workspace_path


ROOT = '/workspace/skills/01-tcc/tcc-ppt-skill-v4'
REF = ROOT + '/references/step_0_extract.md'


@pytest.mark.parametrize('delivery_only', [False, True])
def test_plan_path_contract_survives_acceptance_policy(monkeypatch, delivery_only):
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: delivery_only)
    job = SimpleNamespace(instruction='Generate LNG report', input_files=[])
    context = {'name': 'TCC', 'version': '0.1.42', 'root': ROOT,
               'skill_md': '# TCC\nUse assets/sources/文本.md.', 'runtime_requirements': {}}
    system = _agent_messages(job, [context], [])[0]['content']
    assert 'exact absolute workspace paths below /workspace' in system
    assert 'Resolve Skill-relative examples against that selected Skill Root' in system
    if delivery_only:
        assert 'Do not write an extra verifier or call run_verifier' in system
    plan = next(t['function'] for t in SANDBOX_AGENT_TOOLS if t['function']['name'] == 'update_plan')
    for key in ('input_refs', 'output_refs'):
        description = plan['parameters']['properties']['steps']['items']['properties'][key]['items']['description']
        assert 'absolute path under /workspace' in description
        assert 'Skill Root' in description


@pytest.mark.parametrize('path', ['assets/sources/文本.md', '../secret', '/etc/passwd', '/workspace/../etc/passwd'])
def test_invalid_plan_path_reports_offending_reference_and_keeps_boundary(path):
    with pytest.raises(SandboxRuntimeError) as error:
        _validated_workspace_path(path)
    assert error.value.code == 'PLAN_PATH_INVALID'
    assert repr(path) in str(error.value)
    assert 'Skill Root' in str(error.value)
    assert _validated_workspace_path(ROOT + '/assets/sources/文本.md') == ROOT + '/assets/sources/文本.md'


@pytest.mark.parametrize('tracking', [False, True])
def test_missing_plan_diagnostic_matches_tracking_mode(tracking):
    state = AgentExecutionState(skill_count=1, plan_status_tracking=tracking)
    result = state.ensure_active_step()
    assert result['error_code'] == 'PLAN_REQUIRED'
    assert ('in_progress' in result['message']) is tracking
    if not tracking:
        assert 'outline' in result['message']
    assert 'in_progress' not in PRE_PLAN_INSPECTION_HINT


class WindowSandbox:
    def __init__(self, text):
        self.text = text
        self.reads = []

    async def read_text_window(self, path, *, offset=0, limit=30000):
        self.reads.append((offset, limit))
        content = self.text[offset:offset+limit]
        end = offset + len(content)
        eof = end >= len(self.text)
        return {'ok': True, 'path': path, 'content': content, 'offset': offset, 'limit': limit,
                'total_chars': len(self.text), 'returned_chars': len(content),
                'eof': eof, 'complete': offset == 0 and eof, 'truncated': not eof,
                'next_offset': None if eof else end}


@pytest.mark.parametrize('length', [30000, 30504, 34609, 60000])
def test_reference_read_auto_completes_real_incident_lengths(length):
    text = ('规范条文：' * (length // 5 + 1))[:length]
    sandbox = WindowSandbox(text)
    state = AgentExecutionState(skill_count=1)
    result = asyncio.run(_read_file_payload(sandbox, state, REF, [ROOT],
                        offset=0, limit=30000, complete_reference=True))
    assert result['content'] == text
    assert result['total_chars'] == length
    assert result['returned_chars'] <= result['limit']
    assert result['eof'] and result['complete'] and not result['truncated']
    assert result['next_offset'] is None
    assert result['reference'] and not result['retained']  # UTF-8 file exceeds shelf cap.
    assert len(sandbox.reads) == (1 if length == 30000 else 2)


@pytest.mark.parametrize('length,auto', [(34609, False), (60001, True)])
def test_bounded_or_large_reference_has_explicit_continuation(length, auto):
    text = '字' * length
    sandbox = WindowSandbox(text)
    result = asyncio.run(_read_file_payload(sandbox, AgentExecutionState(skill_count=1), REF, [ROOT],
                        offset=0, limit=30000, complete_reference=auto))
    assert result['content'] == text[:30000]
    assert result['total_chars'] == length
    assert result['truncated'] and not result['eof'] and not result['complete']
    assert result['next_offset'] == 30000 and 'next_offset' in result['hint']
    assert len(sandbox.reads) == 1


def test_mutable_file_does_not_auto_expand_requested_window():
    sandbox = WindowSandbox('x' * 34609)
    result = asyncio.run(_read_file_payload(sandbox, AgentExecutionState(skill_count=1), ROOT+'/assets/data.txt', [ROOT],
                        offset=0, limit=30000, complete_reference=True))
    assert result['truncated'] and not result['reference']
    assert len(sandbox.reads) == 1


def test_eof_proof_can_pin_exact_limit_and_cached_slice_reports_completeness():
    state = AgentExecutionState(skill_count=1)
    sandbox = WindowSandbox('x' * 20000)
    first = asyncio.run(_read_file_payload(sandbox, state, REF, [ROOT], offset=0, limit=20000))
    assert first['retained'] and first['complete'] and first['eof']
    cached = asyncio.run(_read_file_payload(sandbox, state, REF, [ROOT], offset=19000, limit=2000))
    assert cached['cached'] and cached['eof'] and not cached['complete']
    assert cached['total_chars'] == 20000 and cached['returned_chars'] == 1000
    assert len(sandbox.reads) == 1


@pytest.mark.parametrize('length,offset', [(30000, 0), (34609, 0), (34609, 30000), (30504, 40000)])
def test_runtime_read_program_reports_measured_eof(monkeypatch, length, offset):
    text = '文' * length

    class VirtualPath(PurePosixPath):
        def resolve(self): return self
        def exists(self): return True
        def is_file(self): return True
        def is_symlink(self): return False
        def read_text(self, **kwargs): return text

    sandbox = DockerSandbox(SimpleNamespace(), job_id='read-window')

    async def command(argv, **kwargs):
        output = io.StringIO()
        with monkeypatch.context() as patch:
            patch.setattr('pathlib.Path', VirtualPath)
            patch.setattr('sys.argv', ['-c', *argv[3:]])
            with contextlib.redirect_stdout(output):
                exec(compile(argv[2], '<sandbox-read>', 'exec'), {})
        return SandboxCommandResult(0, output.getvalue(), '')

    sandbox.command = command
    result = asyncio.run(sandbox.read_text_window(REF, offset=offset))
    assert result['total_chars'] == length
    assert result['content'] == text[offset:offset+30000]
    assert result['eof'] == (offset + len(result['content']) >= length)
    assert result['next_offset'] == (None if result['eof'] else offset+len(result['content']))


def test_runtime_rejects_inconsistent_window_metadata():
    sandbox = DockerSandbox(SimpleNamespace(), job_id='invalid-window')

    async def command(argv, **kwargs):
        return SandboxCommandResult(0, json.dumps({'content':'text','total_chars':500,'offset':0,'limit':30000,
                    'returned_chars':4,'eof':True,'complete':True,'truncated':False,'next_offset':None}), '')

    sandbox.command = command
    with pytest.raises(SandboxRuntimeError) as error:
        asyncio.run(sandbox.read_text_window(REF))
    assert error.value.code == 'SANDBOX_READ_FAILED'


def test_loop_preserves_plan_required_code_and_outline_diagnostic(client, user_headers, fake_model_gateway, monkeypatch):
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobEvent
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from test_orchestration import MemorySandbox

    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: True)
    _, version = create_version(client, user_headers, slug='outline-contract', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers,
                          data={'version_id':version['id'], 'instruction':'answer 42'}).json()

    class ObservedPrecondition(Exception): pass

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        turn = 0

        async def agent_step(self, *, messages):
            self.turn += 1
            if self.turn == 1:
                return ModelResult({'action':'write_file','path':'/workspace/output/result.txt','content':'42'}, 'test', {})
            payload = next(json.loads(m['content'])['payload'] for m in reversed(messages)
                           if m.get('content','').startswith('{"tool_result": "write_file"'))
            assert payload['error_code'] == 'PLAN_REQUIRED'
            assert 'outline' in payload['message'] and 'in_progress' not in payload['message']
            raise ObservedPrecondition

    sandbox = MemorySandbox()
    with SessionLocal() as db:
        job = db.get(WorkflowJob, created['id'])
        context = {'name':'Test','version':'1','root':ROOT,'skill_md':'# Answer','runtime_requirements':{}}
        with pytest.raises(ObservedPrecondition):
            asyncio.run(_run_agent_loop(db, job, sandbox, skill_contexts=[context], gateway=Gateway(), job_cancelled=lambda:False))
        event = db.scalar(select(JobEvent).where(JobEvent.job_id==job.id, JobEvent.event_type=='tool'))
        assert event.title == '补充执行计划'
        assert event.data['error_code'] == 'PLAN_REQUIRED'
        assert '/workspace/work/skillgo-plan.json' not in sandbox.files
