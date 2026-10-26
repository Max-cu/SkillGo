"""Recovery must preserve spec failures, shared environments and lease fencing."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound
from requests import Response
from requests.exceptions import ConnectionError, ReadTimeout, SSLError
from sqlalchemy import select

from app import environment_preparation as prep
from app import environment_worker as worker
from app.environment_failures import transient_build_failure
from app.models import PreparedEnvironment, utcnow
from test_environment_preparation import BASE, db, version


def failed_environment(db, *, age=301, message='No such container: old-builder', inventory=None):
    env = prep.enqueue_environment(db, prep.environment_spec(['pdf.read']))
    env.status = 'failed'
    env.error_message = message
    env.updated_at = utcnow() - timedelta(seconds=age)
    env.inventory = inventory or {}
    env.attempt = 'old-attempt'
    env.lease_until = utcnow() - timedelta(seconds=1)
    db.commit()
    return env


@pytest.mark.parametrize('age,expected', [(299, 'failed'), (300, 'queued'), (301, 'queued')])
def test_failure_cooldown_boundary(db, monkeypatch, age, expected):
    now = utcnow()
    monkeypatch.setattr(prep, 'utcnow', lambda: now)
    env = failed_environment(db)
    env.updated_at = now - timedelta(seconds=age)
    db.commit()
    assert prep.enqueue_environment(db, env.spec).status == expected


def test_new_binding_reuses_and_requeues_failed_digest(db):
    first = version(db)
    env = prep.bind_version_environment(db, first)
    env.status = 'failed'
    env.error_message = 'No such container: old-builder'
    env.updated_at = utcnow() - timedelta(minutes=10)
    env.image_id = BASE
    env.inventory = {'old': 'unverified'}
    db.commit()
    second = version(db, 'second')
    reused = prep.bind_version_environment(db, second)
    assert reused.digest == env.digest and reused.status == 'queued'
    assert reused.error_message is reused.image_id is reused.attempt is reused.lease_until is None
    assert reused.inventory == {}
    db.commit()
    assert len(db.scalars(select(PreparedEnvironment)).all()) == 1
    claim = worker.claim_environment()
    assert claim and claim[0] == env.digest
    assert worker.claim_environment() is None


def test_existing_binding_recovers_without_reupload(db):
    v = version(db)
    env = prep.bind_version_environment(db, v)
    env.status = 'failed'
    env.error_message = 'Environment builder lease expired; retry preparation'
    env.updated_at = utcnow() - timedelta(minutes=10)
    db.commit()
    assert prep.prepare_job_environment(db, [v]).status == 'queued'
    assert v.environment_binding.environment_digest == env.digest
    db.commit()
    with worker.SessionLocal() as other:
        assert other.get(PreparedEnvironment, env.digest).status == 'queued'


@pytest.mark.parametrize('message,inventory', [
    ('Capability probes failed: pdf.read', {}),
    ('Invalid Python requirement', {}),
    ('No such container: quoted validation input', {'build_failure_retryable': False}),
])
def test_permanent_failures_stay_failed_on_existing_binding(db, message, inventory):
    env = failed_environment(db, message=message, inventory=inventory)
    v = SimpleNamespace(environment_binding=SimpleNamespace(environment=env))
    assert prep.bind_version_environment(db, v).status == 'failed'
    assert prep.enqueue_environment(db, env.spec).status == 'failed'


def test_validation_error_cannot_trigger_requeue(db):
    env = failed_environment(db)
    assert prep.enqueue_environment(db, env.spec, error='Invalid spec').status == 'failed'
    assert env.error_message == 'No such container: old-builder'


def test_validation_failure_is_marked_nonretryable(db):
    env = prep.enqueue_environment(db, {'invalid': True}, error='No such container: invalid requirement')
    env.updated_at = utcnow() - timedelta(minutes=10)
    db.commit()
    assert prep.requeue_failed_environment(db, env).status == 'failed'


def test_disabled_preparation_preserves_failed_binding(db, monkeypatch):
    from dataclasses import replace
    env = failed_environment(db)
    monkeypatch.setattr(prep, 'settings', replace(prep.settings, environment_preparation_enabled=False))
    assert prep.requeue_failed_environment(db, env).status == 'failed'


@pytest.mark.parametrize('status', ['queued', 'building', 'probing', 'ready', 'revoked'])
def test_nonfailed_environments_are_never_reset(db, status):
    env = failed_environment(db)
    env.status = status
    env.attempt = 'current'
    env.image_id = BASE
    db.commit()
    assert prep.enqueue_environment(db, env.spec).status == status
    assert env.attempt == 'current' and env.image_id == BASE


@pytest.mark.parametrize('status', ['building', 'ready', 'failed'])
def test_stale_request_cannot_reset_newer_state(db, status):
    env = failed_environment(db)
    with worker.SessionLocal() as other:
        current = other.get(PreparedEnvironment, env.digest)
        current.status = status
        current.attempt = 'new-owner'
        current.updated_at = utcnow()
        other.commit()
    assert env.status == 'failed'  # This request still holds a stale identity-map snapshot.
    prep.requeue_failed_environment(db, env)
    db.commit()
    assert env.status == status and env.attempt == 'new-owner'


def api_error(code, message):
    response = Response()
    response.status_code = code
    return APIError(message, response=response, explanation=message)


def missing_container(message):
    response = Response()
    response.status_code = 404
    return NotFound('missing', response=response, explanation=message)


@pytest.mark.parametrize('error,expected', [
    (missing_container('No such container: builder'), True),
    (api_error(404, 'No such image: base'), False),
    (api_error(500, 'daemon unavailable'), True),
    (api_error(503, 'daemon unavailable'), True),
    (api_error(403, 'permission denied'), False),
    (ReadTimeout('daemon read timed out'), True),
    (ConnectionError('daemon unreachable'), True),
    (SSLError('certificate verification failed'), False),
    (TimeoutError('Environment preparation exceeded its total budget'), True),
    (ValueError('connection reset is invalid input'), False),
    (RuntimeError('Capability probes failed: pdf.read'), False),
])
def test_retry_classification(error, expected):
    assert transient_build_failure(error) == expected


def test_transient_build_retries_after_probing_and_succeeds(db, monkeypatch):
    env = prep.enqueue_environment(db, prep.environment_spec([]))
    db.commit()
    attempts = []
    sleeps = []
    monkeypatch.setattr(worker.time, 'sleep', sleeps.append)
    def build(client, spec, attempt, on_probing):
        with worker.SessionLocal() as other:
            current = other.get(PreparedEnvironment, env.digest)
            assert current.status == 'building' and current.attempt == attempt
        attempts.append(attempt)
        on_probing()
        if len(attempts) == 1:
            raise missing_container('No such container: probe')
        return BASE, {'capabilities': []}
    monkeypatch.setattr(worker, 'build_environment', build)
    assert worker.process_one(None)
    db.refresh(env)
    assert env.status == 'ready' and env.image_id == BASE
    assert len(attempts) == 2 and len(set(attempts)) == 1
    assert sleeps == [worker.TRANSIENT_RETRY_DELAY_SECONDS]
    assert not worker.process_one(None)


@pytest.mark.parametrize('error,retryable,expected_calls', [
    (ConnectionError('daemon unavailable'), True, 3),
    (ValueError('connection reset is invalid input'), False, 1),
])
def test_retry_limit_and_terminal_failure_metadata(db, monkeypatch, error, retryable, expected_calls):
    env = prep.enqueue_environment(db, prep.environment_spec([]))
    db.commit()
    calls = []
    monkeypatch.setattr(worker.time, 'sleep', lambda _: None)
    def build(*args, **kwargs):
        calls.append(args)
        raise error
    monkeypatch.setattr(worker, 'build_environment', build)
    assert worker.process_one(None)
    db.refresh(env)
    assert len(calls) == expected_calls
    assert env.status == 'failed' and env.image_id is None
    assert env.inventory['build_failure_retryable'] is retryable
    env.updated_at = utcnow() - timedelta(minutes=10)
    db.commit()
    assert prep.requeue_failed_environment(db, env).status == ('queued' if retryable else 'failed')


@pytest.mark.parametrize('fence', ['expired', 'other-owner', 'ready'])
def test_renewal_never_resurrects_lost_attempt(db, fence):
    env = prep.enqueue_environment(db, prep.environment_spec([]))
    db.commit()
    _, _, token = worker.claim_environment()
    db.refresh(env)
    if fence == 'expired':
        env.lease_until = utcnow() - timedelta(seconds=1)
    elif fence == 'other-owner':
        env.attempt = 'new-owner'
    else:
        env.status = 'ready'
    db.commit()
    db.refresh(env)
    before = (env.status, env.attempt, env.lease_until)
    with pytest.raises(worker.EnvironmentBuildLeaseLost):
        worker.renew_lease(env.digest, token)
    db.refresh(env)
    assert (env.status, env.attempt, env.lease_until) == before


@pytest.mark.parametrize('lost_owner', [False, True])
def test_failed_renewal_stops_before_another_build(db, monkeypatch, lost_owner):
    env = prep.enqueue_environment(db, prep.environment_spec([]))
    db.commit()
    calls = []
    def build(*args, **kwargs):
        calls.append(args)
        raise ConnectionError('daemon unavailable')
    def sleep(_):
        if lost_owner:
            with worker.SessionLocal() as other:
                row = other.get(PreparedEnvironment, env.digest)
                row.attempt = 'new-owner'
                other.commit()
    monkeypatch.setattr(worker.time, 'sleep', sleep)
    monkeypatch.setattr(worker, 'build_environment', build)
    if not lost_owner:
        def fail_renewal(*args):
            raise RuntimeError('database unavailable')
        monkeypatch.setattr(worker, 'renew_lease', fail_renewal)
    assert worker.process_one(None)
    assert len(calls) == 1
    db.refresh(env)
    assert env.status == 'building'
    if lost_owner:
        assert env.attempt == 'new-owner'


def test_retry_renews_full_build_budget(db, monkeypatch):
    now = [utcnow()]
    monkeypatch.setattr(worker, 'utcnow', lambda: now[0])
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)))
    env = prep.enqueue_environment(db, prep.environment_spec([]))
    db.commit()
    calls = []
    def build(client, spec, attempt, on_probing):
        calls.append(attempt)
        now[0] += timedelta(seconds=worker.settings.environment_build_seconds - 1)
        if len(calls) == 1:
            raise ReadTimeout('temporary daemon timeout')
        on_probing()
        return BASE, {'capabilities': []}
    monkeypatch.setattr(worker, 'build_environment', build)
    assert worker.process_one(None)
    db.refresh(env)
    assert env.status == 'ready' and len(calls) == 2


def test_stale_success_cannot_replace_new_build(db, monkeypatch):
    env = prep.enqueue_environment(db, prep.environment_spec([]))
    db.commit()
    def build(client, spec, attempt, on_probing):
        with worker.SessionLocal() as other:
            current = other.get(PreparedEnvironment, env.digest)
            current.attempt = 'replacement-attempt'
            current.image_id = 'sha256:' + 'b' * 64
            other.commit()
        return BASE, {'capabilities': []}
    monkeypatch.setattr(worker, 'build_environment', build)
    assert worker.process_one(None)
    db.refresh(env)
    assert env.status == 'building' and env.attempt == 'replacement-attempt'
    assert env.image_id == 'sha256:' + 'b' * 64


def test_job_submission_commits_requeue_and_waits_for_environment(client, user_headers, monkeypatch):
    from dataclasses import replace
    from app import config, sandbox_worker
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version, sandbox_skill_zip
    monkeypatch.setattr(prep, 'settings', replace(prep.settings,
        environment_preparation_enabled=True, environment_base_image=BASE))
    monkeypatch.setattr(config, 'settings', replace(config.settings, sandbox_worker_enabled=True))
    _, uploaded = create_version(client, user_headers, slug='retry-environment-job', package=sandbox_skill_zip())
    digest = uploaded['environment_preparation']['digest']
    with SessionLocal() as session:
        env = session.get(PreparedEnvironment, digest)
        env.status = 'failed'
        env.error_message = 'No such container: previous-builder'
        env.updated_at = utcnow() - timedelta(minutes=10)
        session.commit()
    response = client.post('/api/v1/jobs', headers=user_headers,
        data={'version_id': uploaded['id'], 'instruction': 'Create report'})
    assert response.status_code == 201, response.text
    assert sandbox_worker._claim_job() is None
    with SessionLocal() as session:
        env = session.get(PreparedEnvironment, digest)
        job = session.get(WorkflowJob, response.json()['id'])
        assert env.status == 'queued'
        assert job.memory.data['prepared_environment_digest'] == digest
        assert job.status.value == 'queued'
