from dataclasses import replace
from types import SimpleNamespace
import asyncio
import json
import runpy
from pathlib import Path

import pytest
from app import environment_preparation as prep
from app import environment_cleanup as cleanup
from app.environment_capabilities import preflight_environment
from app.models import PreparedEnvironment, SkillEnvironmentBinding, WorkflowJob, WorkflowJobMemory
from test_environment_preparation import db, version, BASE
from test_environment_capabilities import ProbeSandbox


def test_minimal_recipes_are_explicit_and_deduplicated(db, monkeypatch):
    monkeypatch.setattr(prep, 'settings', replace(prep.settings, environment_minimal_base_image=BASE))
    a = prep.environment_spec(['pdf.render', 'office.xlsx'])
    b = prep.environment_spec(['office.xlsx', 'pdf.render'])
    assert a == b and a['layout'] == 'minimal-v1'
    assert a['capability_packages'] == ['PyMuPDF==1.26.4', 'openpyxl==3.1.5']
    assert not a['system_packages']
    assert 'python-pptx' not in json.dumps(a) and 'libreoffice' not in json.dumps(a)
    assert prep.enqueue_environment(db, a).digest == prep.enqueue_environment(db, b).digest
    assert prep.environment_spec(a['capabilities'], BASE, layout=a['layout']) == a


def test_minimal_multiskill_union_uses_combined_recipe(db, monkeypatch):
    monkeypatch.setattr(prep, 'settings', replace(prep.settings, environment_minimal_base_image=BASE))
    a, b = version(db, 'a', 'pdf.render'), version(db, 'b', 'office.xlsx')
    result = prep.prepare_job_environment(db, [a, b])
    assert result.spec['capabilities'] == ['office.xlsx', 'pdf.read', 'pdf.render']
    assert result.spec['layout'] == 'minimal-v1'
    assert 'python-pptx' not in json.dumps(result.spec)


def test_task_probe_selects_required_providers():
    sandbox = ProbeSandbox()
    asyncio.run(preflight_environment(sandbox, [{'runtime_requirements': {
        'prepared_capabilities': ['pdf.read', 'office.xlsx']}}]))
    selected = sandbox.calls[0][0][3].splitlines()[0]
    assert selected.startswith('SELECTED_PROVIDERS=')
    assert 'openpyxl' in selected and 'pymupdf' in selected
    assert 'python-pptx' not in selected and 'python-docx' not in selected


def test_inventory_reports_private_package_before_base(monkeypatch):
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [
        SimpleNamespace(metadata={'Name': 'pillow'}, version='12.3.0'),
        SimpleNamespace(metadata={'Name': 'Pillow'}, version='13.0.0')])
    probe = runpy.run_path(str(Path(prep.__file__).with_name('environment_probe.py')))['probe']
    assert probe([])['python_distributions']['pillow'] == '12.3.0'


def docker_client(*, containers=(), fail_remove=False):
    removed = []
    def remove(image, **kwargs):
        assert kwargs == {'force': False, 'noprune': True}
        if fail_remove:
            raise RuntimeError('image busy')
        removed.append(image)
    return SimpleNamespace(
        containers=SimpleNamespace(list=lambda **kwargs: list(containers)),
        images=SimpleNamespace(get=lambda image: SimpleNamespace(
            labels={'skillgo.prepared_environment': 'true'}), remove=remove)), removed


@pytest.mark.parametrize('status', ['ready', 'failed', 'queued'])
def test_unreferenced_environment_is_reclaimed_without_retention(db, status):
    env = prep.enqueue_environment(db, prep.environment_spec([], BASE))
    env.status = status
    env.image_id = 'sha256:' + 'b' * 64 if status == 'ready' else None
    cleanup.request_environment_cleanup(db, [env.digest])
    db.commit()
    client, removed = docker_client()
    cleanup.cleanup_released_environments(db, client)
    assert db.get(PreparedEnvironment, env.digest) is None
    assert len(removed) == (status == 'ready')


def test_shared_environment_is_preserved(db):
    v = version(db)
    env = prep.bind_version_environment(db, v)
    env.status = 'ready'; env.image_id = 'sha256:' + 'b' * 64
    cleanup.request_environment_cleanup(db, [env.digest])
    db.commit()
    client, removed = docker_client()
    cleanup.cleanup_released_environments(db, client)
    assert db.get(PreparedEnvironment, env.digest) is not None
    assert not removed


@pytest.mark.parametrize('busy', ['container', 'running_job', 'building', 'image_conflict'])
def test_in_use_environments_are_preserved(db, busy):
    v = version(db)
    env = prep.enqueue_environment(db, prep.environment_spec([], BASE))
    env.status = 'building' if busy == 'building' else 'ready'
    env.image_id = 'sha256:' + 'b' * 64
    if busy == 'running_job':
        job = WorkflowJob(user_id=v.created_by_id, skill_id=v.skill_id,
                          skill_version_id=v.id, status='RUNNING', execution_mode='sandbox_required')
        db.add(job); db.flush()
        db.add(WorkflowJobMemory(job_id=job.id, data={'prepared_environment_digest': env.digest}))
    cleanup.request_environment_cleanup(db, [env.digest])
    db.commit()
    client, removed = docker_client(containers=['busy'] if busy == 'container' else (),
                                    fail_remove=busy == 'image_conflict')
    cleanup.cleanup_released_environments(db, client)
    assert db.get(PreparedEnvironment, env.digest) is not None
    assert not removed


def test_new_reference_cancels_pending_cleanup(db):
    spec = prep.environment_spec([], BASE)
    env = prep.enqueue_environment(db, spec)
    cleanup.request_environment_cleanup(db, [env.digest])
    db.commit()
    assert prep.enqueue_environment(db, spec).inventory.get('cleanup_requested') is None
