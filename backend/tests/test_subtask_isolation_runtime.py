"""Read-only validator containers and alias-safe subtask output writes."""
from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from docker.errors import DockerException

from app import sandbox_runtime as runtime
from app.sandbox_runtime import DockerSandbox, SandboxRuntimeError


class Helper:
    def __init__(self):
        self.removed = []
        self.exec_calls = []

    def start(self):
        pass

    def exec_run(self, argv, **kwargs):
        self.exec_calls.append((argv, kwargs))
        return SimpleNamespace(exit_code=0, output=(b"checked", b""))

    def remove(self, **kwargs):
        self.removed.append(kwargs)


def fake_runtime(create):
    sandbox = DockerSandbox(SimpleNamespace(containers=SimpleNamespace(create=create)), job_id="validator-test")
    sandbox.volume = SimpleNamespace(name="job-volume")
    return sandbox


def test_validator_uses_readonly_volume_and_removes_helper():
    helper = Helper()
    configurations = []

    def create(**kwargs):
        configurations.append(kwargs)
        return helper

    sandbox = fake_runtime(create)
    result = asyncio.run(sandbox.validation_command(["python3", "check.py"], cwd="/workspace/work"))
    config = configurations[0]
    assert config["volumes"] == {"job-volume": {"bind": "/workspace", "mode": "ro"}}
    assert config["read_only"] is True
    assert config["user"] == "10001:10001"
    assert config["cap_drop"] == ["ALL"]
    assert config["security_opt"] == ["no-new-privileges:true"]
    assert config["network_mode"] == "none"
    assert "/tmp" in config["tmpfs"]
    assert config["labels"]["skillgo.deploy_id"] == runtime.settings.deploy_id
    assert result.stdout == "checked"
    assert helper.removed == [{"force": True}]
    assert sandbox.container is None


def test_validator_creation_failure_has_no_writable_fallback():
    def create(**kwargs):
        raise DockerException("unavailable")

    sandbox = fake_runtime(create)
    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(sandbox.validation_command(["python3", "check.py"]))
    assert caught.value.code == "SUBTASK_ISOLATION_UNAVAILABLE"
    assert sandbox.container is None


def test_validator_cancel_during_creation_still_removes_helper():
    helper = Helper()
    entered, finish = threading.Event(), threading.Event()

    def create(**kwargs):
        entered.set()
        assert finish.wait(5)
        return helper

    async def exercise():
        sandbox = fake_runtime(create)
        task = asyncio.create_task(sandbox.validation_command(["python3", "check.py"]))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert helper.removed == [{"force": True}]


def test_validator_cancel_during_exec_removes_helper():
    entered, removed = threading.Event(), threading.Event()

    class BlockingHelper(Helper):
        def exec_run(self, argv, **kwargs):
            entered.set()
            assert removed.wait(5)
            return SimpleNamespace(exit_code=137, output=(b"", b"killed"))

        def remove(self, **kwargs):
            super().remove(**kwargs)
            removed.set()

    helper = BlockingHelper()

    async def exercise():
        sandbox = fake_runtime(lambda **kwargs: helper)
        task = asyncio.create_task(sandbox.validation_command(["python3", "check.py"]))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert helper.removed == [{"force": True}]


@pytest.fixture
def docker_sandbox(monkeypatch):
    if os.environ.get("SKILLGO_TEST_DOCKER_ISOLATION") != "1":
        pytest.skip("Set SKILLGO_TEST_DOCKER_ISOLATION=1 for real container boundary checks")
    import docker

    client = docker.from_env()
    image = "python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    client.images.pull(image)
    monkeypatch.setattr(runtime, "settings", replace(runtime.settings, sandbox_runtime="", sandbox_memory="256m"))
    sandbox = DockerSandbox(client, job_id=f"isolation-{uuid4().hex}", image_id=image)
    try:
        sandbox.start()
        sandbox.put_files({"work/a.txt": b"A", "work/b.txt": b"B", "spec/style.md": b"style"})
        yield sandbox
    finally:
        sandbox.close()
        client.close()


@pytest.mark.parametrize("operation", [
    "Path('/workspace/spec/style.md').write_text('bad')",
    "Path('/workspace/work/b.txt').write_text('bad')",
    "Path('/workspace/work/a.txt').write_text('bad')",
    "Path('/workspace/work/b.txt').unlink()",
    "Path('/workspace/work/b.txt').rename('/workspace/work/a.txt')",
    "Path('/workspace/work/new.txt').write_text('bad')",
])
def test_docker_validation_cannot_mutate_any_workspace_file(docker_sandbox, operation):
    sandbox = docker_sandbox

    async def exercise():
        result = await sandbox.validation_command(["python3", "-c", "from pathlib import Path; " + operation])
        assert result.exit_code != 0
        assert await sandbox.read_text("/workspace/spec/style.md") == "style"
        assert await sandbox.read_text("/workspace/work/a.txt") == "A"
        assert await sandbox.read_text("/workspace/work/b.txt") == "B"
        clean = await sandbox.validation_command([
            "python3", "-c", "from pathlib import Path; assert Path('/workspace/work/a.txt').read_text() == 'A'; "
            "Path('/tmp/check.txt').write_text('scratch'); print('valid')",
        ])
        assert clean.exit_code == 0
        assert clean.stdout.strip() == "valid"

    asyncio.run(exercise())
    assert not sandbox.client.containers.list(all=True, filters={"label": [
        "skillgo.validator=true", f"skillgo.job_id={sandbox.job_id}",
    ]})


@pytest.mark.parametrize("alias", ["symlink", "directory", "hardlink"])
def test_docker_subtask_write_rejects_aliases(docker_sandbox, alias):
    sandbox = docker_sandbox
    code = {
        "symlink": "os.symlink('/workspace/work/b.txt', '/workspace/work/link.txt')",
        "directory": "os.symlink('/workspace/work', '/workspace/link-dir')",
        "hardlink": "os.link('/workspace/work/b.txt', '/workspace/work/link.txt')",
    }[alias]
    target = "/workspace/link-dir/b.txt" if alias == "directory" else "/workspace/work/link.txt"

    async def exercise():
        assert (await sandbox.command(["python3", "-c", "import os; " + code])).exit_code == 0
        with pytest.raises(SandboxRuntimeError):
            await sandbox.write_subtask_text(target, "bad")
        assert await sandbox.read_text("/workspace/work/b.txt") == "B"
        await sandbox.write_subtask_text("/workspace/work/a.txt", "fixed")
        assert await sandbox.read_text("/workspace/work/a.txt") == "fixed"

    asyncio.run(exercise())
