from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from jsonschema import validate

from app.agent_policy import AgentExecutionState
from app.artifact_collection import COPY_ARTIFACTS_PROGRAM, collect_declared_artifacts
from app.model_gateway import ModelResult, SANDBOX_AGENT_TOOLS
from app.sandbox_agent_loop import _run_agent_loop
from app.sandbox_runtime import SandboxCommandResult, SandboxRuntimeError, _workspace_path
from app.sandbox_tool_registry import validate_agent_action,normalize_agent_action
from app.workflow_tools import run_verifier
from test_orchestration import MemorySandbox

SOURCE='/workspace/skills/report/exports/report.txt'


class ExportSandbox(MemorySandbox):
    def __init__(self,*,change_source=False,change_delivery=False):
        super().__init__();self.files.pop('/workspace/output/result.txt')
        self.files[SOURCE]=b'42';self.change_source=change_source;self.change_delivery=change_delivery
        self.verifier_calls=0

    async def command(self,argv,**kwargs):
        self.calls.append(argv)
        if len(argv)>2 and argv[2]==COPY_ARTIFACTS_PROGRAM:
            for source,target in json.loads(argv[-2]): self.files[target]=self.files[source]
            return SandboxCommandResult(0,'{"ok":true}','')
        self.verifier_calls+=1
        if self.change_source:self.files[SOURCE]=b'43'
        if self.change_delivery:
            for path in list(self.files):
                if path.startswith('/workspace/output/'):self.files[path]=b'43'
        return SandboxCommandResult(0,json.dumps({'checks':[{'requirement_id':'r1','passed':True,'observed':'file exists and contains the expected report'}]}),'')


def test_explicit_export_paths_are_collected_before_verification():
    sandbox=ExportSandbox()
    proof=asyncio.run(run_verifier(sandbox,{'argv':['verify'],'artifacts':[SOURCE]},requirements=['report']))
    assert proof['ok'] and sandbox.verifier_calls==1
    assert proof['artifact_sources'][SOURCE] in proof['artifacts']
    assert list(proof['artifacts'].values())==[hashlib.sha256(b'42').hexdigest()]


def test_no_registered_file_is_a_preparation_error_not_a_fake_check_failure():
    sandbox=ExportSandbox()
    proof=asyncio.run(run_verifier(sandbox,{'argv':['verify']},requirements=['report']))
    assert proof['error_code']=='VERIFIER_ARTIFACTS_REQUIRED'
    assert sandbox.verifier_calls==0
    assert 'original export paths' in proof['hint']


@pytest.mark.parametrize('changes',[{'change_source':True},{'change_delivery':True}])
def test_verifier_cannot_change_original_or_delivery_bytes(changes):
    proof=asyncio.run(run_verifier(ExportSandbox(**changes),{'argv':['verify'],'artifacts':[SOURCE]},requirements=['report']))
    assert not proof['ok'] and proof['error_code']=='VERIFIER_FAILED'
    assert 'changed output bytes' in proof['message']


def test_original_and_delivery_alias_do_not_create_two_artifacts():
    sandbox=ExportSandbox()
    first=asyncio.run(collect_declared_artifacts(sandbox,[SOURCE]))
    second=asyncio.run(collect_declared_artifacts(sandbox,[SOURCE,*first['artifacts']]))
    assert second['artifacts']==first['artifacts']


@pytest.mark.skipif(os.name=='nt',reason='The production descriptor-based copier runs inside a Linux sandbox.')
def test_actual_copier_is_idempotent_and_does_not_replace_identical_bytes(tmp_path):
    source=tmp_path/'skills/report/exports/report.txt';source.parent.mkdir(parents=True);source.write_bytes(b'42')
    target=tmp_path/'output/collected/report.txt'
    program=COPY_ARTIFACTS_PROGRAM.replace("os.open('/workspace',",f"os.open({str(tmp_path)!r},",1)
    args=[sys.executable,'-c',program,json.dumps([[SOURCE,'/workspace/output/collected/report.txt']]),'1000']
    first=json.loads(subprocess.check_output(args,text=True));mtime=target.stat().st_mtime_ns
    second=json.loads(subprocess.check_output(args,text=True))
    assert first['copied'] and second['unchanged'] and not second['copied']
    assert target.stat().st_mtime_ns==mtime
    source.write_bytes(b'43')
    third=json.loads(subprocess.check_output(args,text=True))
    assert third['copied'] and target.read_bytes()==b'43'


def test_real_collection_unblocks_retry_but_identical_collection_preserves_proof():
    state=AgentExecutionState(skill_count=1)
    state.register_verifier_failure({'ok':False,'checks':[{'passed':False}],'artifacts':{}})
    assert state.verifier_retry_blocked() is not None
    snapshot={'/workspace/output/report.txt':'hash'}
    assert state.register_artifact_collection(snapshot,{SOURCE:'/workspace/output/report.txt'})
    assert state.verifier_retry_blocked() is None
    state.validation={'status':'passed','artifacts':snapshot,'mutation_epoch':state.mutation_epoch}
    epoch=state.mutation_epoch
    assert not state.register_artifact_collection(snapshot,{SOURCE:'/workspace/output/report.txt'})
    assert state.validation['status']=='passed' and state.mutation_epoch==epoch


def test_verifier_artifact_schema_and_validation_agree():
    function=next(t['function'] for t in SANDBOX_AGENT_TOOLS if t['function']['name']=='run_verifier')
    args={'argv':['verify'],'cwd':'/workspace/skills/report','timeout_seconds':60,'artifacts':[SOURCE],'reason':'check actual report'}
    validate(args,function['parameters'])
    assert validate_agent_action({'action':'run_verifier',**args}) is None
    assert validate_agent_action({'action':'run_verifier','argv':['verify'],'artifacts':[]})


def test_structured_writer_preserves_unicode_and_nested_data_with_ordinary_guards():
    data={'title':'产品 "报告"','rows':[['中文',42]],'enabled':True}
    action={'action':'write_json','path':'/workspace/work/plan.json','data':data,'reason':'write plan'}
    function=next(t['function'] for t in SANDBOX_AGENT_TOOLS if t['function']['name']=='write_json')
    validate({k:v for k,v in action.items() if k!='action'},function['parameters'])
    normalized=normalize_agent_action(action)
    assert json.loads(normalized['content'])==data
    assert normalized['action']=='write_file' and validate_agent_action(action) is None
    with pytest.raises(SandboxRuntimeError):_workspace_path(normalize_agent_action({**action,'path':'/etc/passwd'})['path'])
    assert validate_agent_action({**action,'data':{'bad':float('nan')}})


def test_export_verify_finish_completes_without_dummy_write_or_duplicate(client,user_headers):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version,sandbox_skill_zip
    _,version=create_version(client,user_headers,slug='export-verification',package=sandbox_skill_zip())
    created=client.post('/api/v1/jobs',headers=user_headers,data={'version_id':version['id'],'instruction':'report'}).json()
    sandbox=ExportSandbox()
    class Gateway:
        connection=SimpleNamespace(context_tokens=48000)
        turn=0
        async def agent_step(self,*,messages):
            self.turn+=1
            if self.turn==1:
                action={'action':'update_plan','goal':'report','steps':[{'id':'make','title':'Make report'},{'id':'verify','title':'Verify'}],
                        'validation_step_id':'verify','success_criteria':['Report exists']}
            elif self.turn==2:
                action={'action':'run_verifier','argv':['verify'],'artifacts':[SOURCE]}
            else:
                assert self.turn==3
                action={'action':'finish','summary':'report ready','artifacts':[SOURCE]}
            return ModelResult(action,'test',{})
    with SessionLocal() as db:
        job=db.get(WorkflowJob,created['id']);gateway=Gateway()
        result=asyncio.run(_run_agent_loop(db,job,sandbox,skill_contexts=[{'root':'/workspace/skills/report','name':'report',
          'version':'1','skill_md':'Create and verify a report','runtime_requirements':{}}],gateway=gateway,job_cancelled=lambda:False))
        assert gateway.turn==3 and len(result[1])==1
        assert sandbox.verifier_calls==1
        assert not any(e.data.get('error_code') in {'VERIFIER_RETRY_WITHOUT_PROGRESS','AGENT_PLAN_INCOMPLETE'} for e in job.events)


def test_optimized_skill_exposes_only_fixed_build_finalize_export_workflow():
    from pathlib import Path
    from app.skill_phase_contracts import stage_contracts
    guide = (Path(__file__).resolve().parents[2] / 'skill-packages/presentation/overlay/SKILL.md').read_text(encoding='utf8')
    contracts = stage_contracts({'root':'/workspace/skills/presentation','skill_md':guide})
    workflow = contracts['workflows']['steps-3-5']
    assert workflow['stages'] == [3,4,5]
    assert [command[1] for command in workflow['commands']] == [
        'scripts/validators/check_templates.py','scripts/cleaners/clear_downstream.py',
        'scripts/build_svgs.py','scripts/validators/verify_build_output.py',
        'scripts/finalize_svg.py','scripts/validators/verify_finalize_output.py',
        'scripts/export_flat.py','scripts/validators/verify_export_output.py',
        'scripts/cleaners/clear_downstream.py']
    assert all(1 not in item['stages'] for item in contracts['workflows'].values())
