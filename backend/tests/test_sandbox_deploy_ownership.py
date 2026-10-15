from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import app.sandbox_runtime as runtime
from app.sandbox_runtime import (
    cleanup_execution_sandbox,
    cleanup_job_sandboxes,
    cleanup_stale_sandboxes,
    ownership_labels,
)


def _container(labels: dict) -> MagicMock:
    c = MagicMock()
    c.labels = labels
    return c


def _volume(labels: dict) -> MagicMock:
    v = MagicMock()
    v.attrs = {"Labels": labels}
    return v


def test_ownership_labels_include_deploy_id(monkeypatch):
    monkeypatch.setattr(
        runtime, "settings", SimpleNamespace(deploy_id="prod-a", cleanup_untagged_sandboxes=False)
    )
    labels = ownership_labels(job_id="j1", execution_id="e1")
    assert labels["skillgo.deploy_id"] == "prod-a"
    assert labels["skillgo.job_id"] == "j1"
    assert labels["skillgo.execution_id"] == "e1"


def test_stale_cleanup_skips_other_deploy(monkeypatch):
    monkeypatch.setattr(
        runtime, "settings", SimpleNamespace(deploy_id="prod", cleanup_untagged_sandboxes=False)
    )
    ours = _container({"skillgo.sandbox": "true", "skillgo.job_id": "stale", "skillgo.deploy_id": "prod"})
    theirs = _container({"skillgo.sandbox": "true", "skillgo.job_id": "live", "skillgo.deploy_id": "bypass"})
    untagged = _container({"skillgo.sandbox": "true", "skillgo.job_id": "legacy"})
    protected = _container(
        {"skillgo.sandbox": "true", "skillgo.job_id": "leased", "skillgo.deploy_id": "prod"}
    )

    client = MagicMock()
    # Stale GC iterates sandbox + stager labels; return the set once per call.
    client.containers.list.side_effect = lambda **kwargs: [ours, theirs, untagged, protected]
    client.volumes.list.return_value = []

    cleanup_stale_sandboxes(client, protected_job_ids={"leased"})

    assert ours.remove.call_count == 2  # sandbox + stager passes
    theirs.remove.assert_not_called()
    untagged.remove.assert_not_called()
    protected.remove.assert_not_called()


def test_stale_cleanup_untagged_opt_in(monkeypatch):
    monkeypatch.setattr(
        runtime, "settings", SimpleNamespace(deploy_id="prod", cleanup_untagged_sandboxes=True)
    )
    untagged = _container({"skillgo.sandbox": "true", "skillgo.job_id": "legacy"})
    client = MagicMock()
    client.containers.list.side_effect = lambda **kwargs: [untagged]
    client.volumes.list.return_value = []

    cleanup_stale_sandboxes(client, protected_job_ids=set())
    assert untagged.remove.call_count == 2


def test_job_cleanup_scoped_to_deploy(monkeypatch):
    monkeypatch.setattr(
        runtime, "settings", SimpleNamespace(deploy_id="prod", cleanup_untagged_sandboxes=False)
    )
    ours = _container({"skillgo.job_id": "j1", "skillgo.deploy_id": "prod"})
    theirs = _container({"skillgo.job_id": "j1", "skillgo.deploy_id": "bypass"})
    client = MagicMock()
    client.containers.list.side_effect = lambda **kwargs: [ours, theirs]
    client.volumes.list.return_value = []

    cleanup_job_sandboxes(client, "j1")
    assert ours.remove.call_count == 2
    theirs.remove.assert_not_called()


def test_execution_cleanup_scoped_to_deploy(monkeypatch):
    monkeypatch.setattr(
        runtime, "settings", SimpleNamespace(deploy_id="prod", cleanup_untagged_sandboxes=False)
    )
    match = _container(
        {
            "skillgo.job_id": "j1",
            "skillgo.execution_id": "e1",
            "skillgo.deploy_id": "prod",
        }
    )
    other_deploy = _container(
        {
            "skillgo.job_id": "j1",
            "skillgo.execution_id": "e1",
            "skillgo.deploy_id": "bypass",
        }
    )
    other_exec = _container(
        {
            "skillgo.job_id": "j1",
            "skillgo.execution_id": "e2",
            "skillgo.deploy_id": "prod",
        }
    )
    vol = _volume(
        {
            "skillgo.job_id": "j1",
            "skillgo.execution_id": "e1",
            "skillgo.deploy_id": "prod",
        }
    )
    client = MagicMock()
    client.containers.list.side_effect = lambda **kwargs: [match, other_deploy, other_exec]
    client.volumes.list.return_value = [vol]

    cleanup_execution_sandbox(client, job_id="j1", execution_id="e1")
    assert match.remove.call_count == 2
    other_deploy.remove.assert_not_called()
    other_exec.remove.assert_not_called()
    vol.remove.assert_called_once_with(force=True)
