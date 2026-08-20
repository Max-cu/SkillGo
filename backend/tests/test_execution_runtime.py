from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import config, sandbox_worker
from app.database import SessionLocal
from app.execution_runtime import cleanup_execution_history, fail_stale_conversation_runs
from app.models import AgentRun, AgentRunEvent, JobStatus, RunStatus, User, WorkflowJob, utcnow
from app.sandbox_worker import (
    JobLeaseLost,
    _assert_job_lease,
    _claim_job,
    _heartbeat_job,
    _recover_interrupted_jobs,
)
from app.sandbox_runtime import (
    cleanup_execution_sandbox,
    cleanup_job_sandboxes,
    cleanup_stale_sandboxes,
)
from conftest import TEST_CREATOR_EMAIL
from test_skill_flow import skill_zip
from test_workflow_jobs import create_version, sandbox_skill_zip


def test_conversation_turn_uses_durable_run_and_prunes_only_expired_detail(
    client, user_headers, fake_model_gateway
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    response = client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=user_headers,
        data={"message": "记录这次运行", "model_name": "test-fast-model"},
    )
    assert response.status_code == 200, response.text

    now = utcnow()
    with SessionLocal() as db:
        run = db.scalar(
            select(AgentRun).where(AgentRun.conversation_id == conversation["id"])
        )
        assert run is not None
        assert run.status == RunStatus.SUCCEEDED
        assert run.request_message_id
        assert run.response_message_id
        assert run.summary["model_name"] == "test-fast-model"
        assert [event.event_type for event in run.events] == [
            "turn.started",
            "run.completed",
        ]
        run.finished_at = now - timedelta(days=8)

        recent_failure = AgentRun(
            user_id=run.user_id,
            run_type="conversation_turn",
            conversation_id=conversation["id"],
            status=RunStatus.FAILED,
            attempt_count=1,
            started_at=now - timedelta(days=8, minutes=1),
            finished_at=now - timedelta(days=8),
            summary={"model_name": "test-fast-model"},
        )
        expired_failure = AgentRun(
            user_id=run.user_id,
            run_type="conversation_turn",
            conversation_id=conversation["id"],
            status=RunStatus.FAILED,
            attempt_count=1,
            started_at=now - timedelta(days=31, minutes=1),
            finished_at=now - timedelta(days=31),
            summary={"model_name": "test-fast-model"},
        )
        db.add_all([recent_failure, expired_failure])
        db.flush()
        db.add_all(
            [
                AgentRunEvent(
                    run_id=recent_failure.id,
                    sequence=1,
                    event_type="run.failed",
                    status="failed",
                    data={},
                ),
                AgentRunEvent(
                    run_id=expired_failure.id,
                    sequence=1,
                    event_type="run.failed",
                    status="failed",
                    data={},
                ),
            ]
        )
        db.commit()
        successful_run_id = run.id
        recent_failure_id = recent_failure.id
        expired_failure_id = expired_failure.id

    result = cleanup_execution_history(now=now)
    assert result["run_events_deleted"] == 3

    with SessionLocal() as db:
        assert db.get(AgentRun, successful_run_id) is not None
        assert db.get(AgentRun, recent_failure_id) is not None
        assert db.get(AgentRun, expired_failure_id) is not None
        remaining_event_run_ids = set(db.scalars(select(AgentRunEvent.run_id)))
        assert remaining_event_run_ids == {recent_failure_id}


def test_expired_worker_lease_is_requeued_and_fenced(
    client, user_headers, fake_model_gateway, monkeypatch
):
    enabled = replace(config.settings, sandbox_worker_enabled=True)
    monkeypatch.setattr(config, "settings", enabled)
    monkeypatch.setattr(
        sandbox_worker,
        "settings",
        replace(
            sandbox_worker.settings,
            sandbox_worker_enabled=True,
            sandbox_worker_lease_seconds=90,
            sandbox_worker_heartbeat_seconds=15,
            sandbox_worker_max_attempts=3,
        ),
    )
    _, version = create_version(
        client,
        user_headers,
        slug="lease-recovery",
        package=sandbox_skill_zip(),
    )
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "执行一个可恢复任务"},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    first = _claim_job("worker-a")
    assert first is not None
    assert first.job_id == job_id
    assert first.attempt == 1

    with SessionLocal() as db:
        run = db.get(AgentRun, first.run_id)
        assert run is not None
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    reclaimed = _recover_interrupted_jobs()
    assert [(item.job_id, item.execution_id) for item in reclaimed] == [
        (job_id, f"{job_id}-a1")
    ]
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        run = db.get(AgentRun, first.run_id)
        assert job is not None and job.status == JobStatus.QUEUED
        assert run is not None and run.status == RunStatus.QUEUED
        assert run.attempt_count == 1
        assert any(event.event_type == "attempt.interrupted" for event in run.events)

    second = _claim_job("worker-b")
    assert second is not None
    assert second.attempt == 2
    assert second.token != first.token
    assert _heartbeat_job(first) is False
    with SessionLocal() as db:
        with pytest.raises(JobLeaseLost):
            _assert_job_lease(db, first)
        _assert_job_lease(db, second)


def test_old_completed_job_keeps_final_timeline_but_drops_reasoning_detail(
    client, user_headers, fake_model_gateway
):
    _, version = create_version(
        client, user_headers, slug="retention-summary", package=skill_zip()
    )
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "生成留存测试结果"},
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]
    now = utcnow()
    with SessionLocal() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.workflow_job_id == job_id))
        assert run is not None and run.status == RunStatus.SUCCEEDED
        run.finished_at = now - timedelta(days=8)
        db.commit()

    cleanup_execution_history(now=now)

    job = client.get(f"/api/v1/jobs/{job_id}", headers=user_headers).json()
    event_types = [event["event_type"] for event in job["events"]]
    assert "reasoning" not in event_types
    assert {"input", "artifact", "result"}.issubset(event_types)


def test_stale_conversation_run_is_closed_without_deleting_its_summary(
    client, user_headers
):
    conversation = client.post(
        "/api/v1/agent/conversations", headers=user_headers, json={}
    ).json()
    with SessionLocal() as db:
        user_id = db.scalar(select(User.id).where(User.email == TEST_CREATOR_EMAIL))
        assert user_id is not None
        stale = AgentRun(
            user_id=user_id,
            run_type="conversation_turn",
            conversation_id=conversation["id"],
            status=RunStatus.RUNNING,
            attempt_count=1,
            started_at=utcnow() - timedelta(hours=1),
            summary={"model_name": "test-model"},
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    assert fail_stale_conversation_runs() == 1
    with SessionLocal() as db:
        stale = db.get(AgentRun, stale_id)
        assert stale is not None
        assert stale.status == RunStatus.FAILED
        assert stale.error_code == "API_PROCESS_INTERRUPTED"
        assert stale.summary == {"model_name": "test-model"}


class _FakeDockerResource:
    def __init__(self, labels: dict[str, str]):
        self.labels = labels
        self.attrs = {"Labels": labels}
        self.removed = False

    def remove(self, force: bool = False):
        self.removed = force


class _FakeDockerCollection:
    def __init__(self, resources: list[_FakeDockerResource]):
        self.resources = resources

    def list(self, **kwargs):
        requested = kwargs.get("filters", {}).get("label", [])
        requested = [requested] if isinstance(requested, str) else requested
        selected = []
        for resource in self.resources:
            if all(
                resource.labels.get(key) == value
                for key, value in (item.split("=", 1) for item in requested)
            ):
                selected.append(resource)
        return selected


class _FakeDockerClient:
    def __init__(self, containers, volumes):
        self.containers = _FakeDockerCollection(containers)
        self.volumes = _FakeDockerCollection(volumes)


def test_worker_startup_preserves_other_workers_active_job_sandbox():
    active_container = _FakeDockerResource(
        {"skillgo.sandbox": "true", "skillgo.job_id": "active-job"}
    )
    stale_container = _FakeDockerResource(
        {"skillgo.sandbox": "true", "skillgo.job_id": "stale-job"}
    )
    active_volume = _FakeDockerResource(
        {"skillgo.workspace": "true", "skillgo.job_id": "active-job"}
    )
    stale_volume = _FakeDockerResource(
        {"skillgo.workspace": "true", "skillgo.job_id": "stale-job"}
    )
    client = _FakeDockerClient(
        [active_container, stale_container], [active_volume, stale_volume]
    )

    cleanup_stale_sandboxes(client, protected_job_ids={"active-job"})

    assert active_container.removed is False
    assert active_volume.removed is False
    assert stale_container.removed is True
    assert stale_volume.removed is True

    cleanup_job_sandboxes(client, "active-job")
    assert active_container.removed is True
    assert active_volume.removed is True


def test_recovery_cleanup_does_not_remove_newer_attempt_of_same_job():
    old = _FakeDockerResource(
        {
            "skillgo.sandbox": "true",
            "skillgo.job_id": "one-job",
            "skillgo.execution_id": "one-job-a1",
        }
    )
    current = _FakeDockerResource(
        {
            "skillgo.sandbox": "true",
            "skillgo.job_id": "one-job",
            "skillgo.execution_id": "one-job-a2",
        }
    )
    client = _FakeDockerClient([old, current], [])

    cleanup_execution_sandbox(
        client, job_id="one-job", execution_id="one-job-a1"
    )

    assert old.removed is True
    assert current.removed is False
