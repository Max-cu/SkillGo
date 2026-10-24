import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.agent_policy import AgentExecutionState
from app.database import Base, SessionLocal, initialize_schema
from app.model_gateway import ModelResult
from app.models import AuditEvent, WorkflowJob
from app.sandbox_agent_loop import _agent_messages, _run_agent_loop
from test_orchestration import MemorySandbox
from test_skill_flow import skill_zip
from test_workflow_jobs import create_version, sandbox_skill_zip


def test_admin_switch_is_version_scoped_and_jobs_keep_creation_policy(client, user_headers, owner_headers):
    skill, version = create_version(client, user_headers, slug='skill-flow-policy', package=sandbox_skill_zip())
    url = f"/api/v1/admin/skill-versions/{version['id']}/skill-flow"
    assert version['follow_skill_flow'] is False
    assert client.patch(url, headers=user_headers, json={'enabled': True}).status_code == 403
    old = client.post('/api/v1/jobs', headers=user_headers, data={
        'version_id': version['id'], 'instruction': 'Create report', 'follow_skill_flow': 'true'}).json()
    assert old['follow_skill_flow'] is False
    enabled = client.patch(url, headers=owner_headers, json={'enabled': True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()['follow_skill_flow'] is True
    _, second = create_version(client, user_headers, slug='second-flow-policy', package=sandbox_skill_zip())
    multi = client.post('/api/v1/jobs', headers=user_headers, data={
        'version_ids': json.dumps([version['id'], second['id']]), 'instruction': 'Combine both Skills'}).json()
    assert multi['follow_skill_flow'] is False
    new = client.post('/api/v1/jobs', headers=user_headers, data={
        'version_id': version['id'], 'instruction': 'Create report'}).json()
    assert new['follow_skill_flow'] is True
    assert client.patch(url, headers=owner_headers, json={'enabled': False}).status_code == 200
    assert client.get(f"/api/v1/jobs/{new['id']}", headers=user_headers).json()['follow_skill_flow'] is True
    assert client.get(f"/api/v1/jobs/{old['id']}", headers=user_headers).json()['follow_skill_flow'] is False
    uploaded = client.post(f"/api/v1/skills/{skill['id']}/versions", headers=user_headers,
        files={'package': ('v2.zip', sandbox_skill_zip(), 'application/zip')}).json()
    assert uploaded['follow_skill_flow'] is False
    with SessionLocal() as db:
        assert db.query(AuditEvent).filter_by(action='skill.version.skill_flow.update').count() == 2


def test_non_sandbox_and_missing_versions_cannot_enable_switch(client, user_headers, owner_headers):
    _, version = create_version(client, user_headers, slug='instruction-flow', package=skill_zip())
    assert client.patch(f"/api/v1/admin/skill-versions/{version['id']}/skill-flow",
                        headers=owner_headers, json={'enabled': True}).status_code == 409
    assert client.patch('/api/v1/admin/skill-versions/missing/skill-flow',
                        headers=owner_headers, json={'enabled': True}).status_code == 404


@pytest.mark.parametrize('enabled', [False, True])
def test_single_skill_can_write_and_finish_without_forced_plan_only_when_enabled(client, user_headers, monkeypatch, enabled):
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: True)
    _, version = create_version(client, user_headers, slug='direct-flow', package=sandbox_skill_zip())
    jid = client.post('/api/v1/jobs', headers=user_headers, data={
        'version_id': version['id'], 'instruction': 'Create answer'}).json()['id']
    context = {'root': '/workspace/skill', 'name': 'Skill', 'version': '1',
               'skill_md': 'Prepare sources, create answer, then check it.', 'runtime_requirements': {}}
    class Observed(Exception): pass
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        count = 0
        async def agent_step(self, *, messages):
            self.count += 1
            if self.count == 1:
                prompt = messages[0]['content']
                assert ('Administrator-selected Skill flow mode' in prompt) == enabled
                assert ('call update_plan before producing files' in prompt) != enabled
                assert ('Planning is optional: use update_plan only when coordination or recovery needs it' in prompt) == enabled
                assert context['skill_md'] in prompt
                return ModelResult({'action': 'write_file', 'path': '/workspace/output/answer.txt',
                                    'content': '42', 'reason': 'Follow Skill'}, 'test', {})
            if not enabled:
                assert 'PLAN_REQUIRED' in str(messages)
                raise Observed
            assert 'PLAN_REQUIRED' not in str(messages)
            return ModelResult({'action': 'finish', 'summary': 'Done',
                                'artifacts': ['/workspace/output/answer.txt']}, 'test', {})
    with SessionLocal() as db:
        job = db.get(WorkflowJob, jid); job.follow_skill_flow = enabled
        sandbox = MemorySandbox(); gateway = Gateway()
        run = _run_agent_loop(db, job, sandbox, skill_contexts=[context], gateway=gateway, job_cancelled=lambda: False)
        if enabled:
            result = asyncio.run(run)
            assert gateway.count == 2 and result[1] == ['/workspace/output/answer.txt']
            assert job.memory.data.get('plan') is None
            assert any(e.data.get('planning_mode') == 'skill_flow' for e in job.events)
        else:
            with pytest.raises(Observed): asyncio.run(run)


def test_optional_planning_preserves_verification_and_loaded_skill_guards():
    state = AgentExecutionState(1, plan_required=False, plan_status_tracking=False, loaded_skills={1})
    assert state.ensure_active_step()['ok']
    assert not state.pre_plan_inspection_allowed()
    assert 'verifier' in state.finish_blocker(current_artifacts={'/workspace/output/a.txt': 'hash'})
    state.delivery_only = True
    assert state.finish_blocker(current_artifacts={'/workspace/output/a.txt': 'hash'}) is None
    state.loaded_skills.clear()
    assert 'unread' in state.finish_blocker(current_artifacts={})


def test_multiple_skill_prompt_keeps_planning_even_with_job_flag(monkeypatch):
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: True)
    job = SimpleNamespace(instruction='Combine Skills', follow_skill_flow=True, input_files=[], memory=None)
    context = {'root': '/workspace/one', 'name': 'One', 'version': '1', 'skill_md': 'Rules', 'runtime_requirements': {}}
    prompt = _agent_messages(job, [context, {**context, 'root': '/workspace/two'}], [])[0]['content']
    assert 'call update_plan before producing files' in prompt
    assert 'Administrator-selected Skill flow mode' not in prompt


def test_old_database_migration_adds_disabled_flags_and_keeps_rows():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,email,display_name,password_hash,role,is_active,created_at,updated_at) VALUES ('kept','kept@example.org','Kept','hash','USER',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        c.execute(Base.metadata.tables['skills'].insert().values(
            id='skill-kept', owner_id='kept', slug='kept', name='Kept', summary='Kept'))
        c.execute(Base.metadata.tables['skill_versions'].insert().values(
            id='version-kept', skill_id='skill-kept', created_by_id='kept', version='1',
            package_sha256='0'*64, package_path='kept.zip', manifest={}, skill_md='Original rules',
            follow_skill_flow=True))
        c.execute(Base.metadata.tables['workflow_jobs'].insert().values(
            id='job-kept', user_id='kept', skill_id='skill-kept', skill_version_id='version-kept',
            execution_mode='sandbox_required', instruction='Original request', follow_skill_flow=True))
        for table in ['skill_versions', 'workflow_jobs']:
            c.execute(text(f'ALTER TABLE {table} DROP COLUMN follow_skill_flow'))
        c.execute(text('CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL)'))
        c.execute(text("INSERT INTO alembic_version VALUES ('20260911_0008')"))
    initialize_schema(engine)
    with engine.connect() as c:
        assert c.execute(text('select count(*) from users')).scalar() == 1
        assert c.execute(text('select version_num from alembic_version')).scalar() == '20261022_0009'
        assert c.execute(text("select follow_skill_flow from skill_versions where id='version-kept'")).scalar() == 0
        assert c.execute(text("select follow_skill_flow from workflow_jobs where id='job-kept'")).scalar() == 0
        assert c.execute(text("select instruction from workflow_jobs where id='job-kept'")).scalar() == 'Original request'
    from alembic import command
    from app.database import _migration_config
    with engine.begin() as c:
        command.downgrade(_migration_config(c), '20260911_0008')
        assert c.execute(text("select instruction from workflow_jobs where id='job-kept'")).scalar() == 'Original request'
    initialize_schema(engine)
    engine.dispose()


def test_optional_mode_still_allows_explicit_model_planning():
    from test_agent_policy import _completed_plan
    state = AgentExecutionState(1, plan_required=False, plan_status_tracking=False)
    assert state.update_plan(_completed_plan())['ok']
    assert state.plan is not None
