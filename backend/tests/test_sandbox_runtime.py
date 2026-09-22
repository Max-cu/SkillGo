from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.sandbox_runtime import DockerSandbox, SandboxRuntimeError, _workspace_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("input/test.docx", "/workspace/input/test.docx"),
        ("/workspace/input/test.docx", "/workspace/input/test.docx"),
        ("work/fulltext.txt", "/workspace/work/fulltext.txt"),
    ],
)
def test_workspace_path_normalizes_safe_paths(raw, expected):
    assert _workspace_path(raw, allow_root=False) == expected


@pytest.mark.parametrize("raw", ["../secret", "/etc/passwd", "/workspace/../etc/passwd"])
def test_workspace_path_rejects_escape(raw):
    with pytest.raises(SandboxRuntimeError) as caught:
        _workspace_path(raw, allow_root=False)
    assert caught.value.code == "SANDBOX_PATH_DENIED"


def test_write_payload_limit_covers_base64_of_max_text_file():
    max_text_bytes = 256 * 1024
    base64_chars = ((max_text_bytes + 2) // 3) * 4

    assert base64_chars < 384 * 1024


async def _drive_write(content, *, fail_on_chunk=None):
    """Run DockerSandbox.write_text with a fake transport; return the sandbox
    and the per-chunk argv list the in-container interpreter would receive."""
    from app.sandbox_runtime import SandboxCommandResult

    sandbox = DockerSandbox(SimpleNamespace(), job_id="chunk-job")
    calls = []

    async def fake_command(argv, *, timeout_seconds=30, allow_large_arguments=False):
        assert allow_large_arguments is True
        calls.append(argv)
        if fail_on_chunk is not None and len(calls) == fail_on_chunk:
            return SandboxCommandResult(exit_code=1, stdout="", stderr="disk full")
        return SandboxCommandResult(exit_code=0, stdout="", stderr="")

    sandbox.command = fake_command
    await sandbox.write_text("/workspace/work/out.json", content)
    return calls


def test_write_text_streams_large_payload_in_truncate_then_append_chunks():
    import asyncio
    import base64
    from app.sandbox_runtime import WRITE_CHUNK_BYTES

    payload = "字" * 200000  # ~600 KiB of UTF-8
    assert len(payload.encode("utf-8")) > WRITE_CHUNK_BYTES

    calls = asyncio.run(_drive_write(payload))

    assert len(calls) == 3
    # First chunk truncates, every later chunk appends.
    assert [argv[5] for argv in calls] == ["wb", "ab", "ab"]
    # Base64 of a raw chunk must stay under the 384 KiB internal argv ceiling.
    assert all(len(argv[4]) < 384 * 1024 for argv in calls)
    reassembled = b"".join(base64.b64decode(argv[4]) for argv in calls)
    assert reassembled.decode("utf-8") == payload


def test_write_text_empty_string_still_emits_one_truncating_chunk():
    import asyncio

    calls = asyncio.run(_drive_write(""))
    assert len(calls) == 1 and calls[0][5] == "wb"


def test_write_text_rejects_above_effective_cap_without_any_transport_call():
    import asyncio
    from app.sandbox_runtime import MAX_WRITE_BYTES

    sentinel = []

    async def fake_command(argv, **kwargs):
        sentinel.append(argv)
        raise AssertionError("oversized write must be rejected before transport")

    sandbox = DockerSandbox(SimpleNamespace(), job_id="cap-job")
    sandbox.command = fake_command
    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(sandbox.write_text("/workspace/work/x.json", "x" * (MAX_WRITE_BYTES + 1)))
    assert caught.value.code == "SANDBOX_WRITE_TOO_LARGE"
    assert sentinel == []


def test_write_text_mid_stream_chunk_failure_surfaces_write_failed():
    import asyncio

    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(_drive_write("y" * (256 * 1024 + 10), fail_on_chunk=2))
    assert caught.value.code == "SANDBOX_WRITE_FAILED"
    assert "disk full" in str(caught.value)



class _FakeContainer:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True


class _FakeContainers:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeContainer()


@pytest.mark.parametrize(
    ("enabled", "expected_mode"),
    [(False, "none"), (True, "bridge")],
)
def test_sandbox_network_mode_is_selected_per_task(enabled, expected_mode):
    containers = _FakeContainers()
    sandbox = DockerSandbox(
        SimpleNamespace(containers=containers),
        job_id="job-1",
        network_enabled=enabled,
    )
    sandbox.volume = SimpleNamespace(name="volume-1")

    sandbox._ensure_container()

    assert containers.kwargs["network_mode"] == expected_mode
    assert containers.kwargs["environment"]["PIP_TARGET"].startswith("/workspace/")

@pytest.mark.parametrize('size', [0, 101])
def test_artifact_size_error_identifies_file_and_limit(monkeypatch, size):
    monkeypatch.setattr('app.sandbox_runtime.settings', SimpleNamespace(sandbox_max_artifact_bytes=100, sandbox_image="test-image"))
    sandbox = DockerSandbox(SimpleNamespace(), job_id='size-job')
    def stream():
        raise AssertionError('Rejected artifact must not be downloaded')
        yield b''
    sandbox.container = SimpleNamespace(get_archive=lambda path: (stream(), {'size': size}))
    with pytest.raises(SandboxRuntimeError) as caught:
        sandbox.download_file('/workspace/output/report.pdf')
    assert caught.value.code == 'SANDBOX_ARTIFACT_SIZE'
    assert '/workspace/output/report.pdf' in str(caught.value)
    assert f'size_bytes={size}' in str(caught.value)
    assert 'limit_bytes=100' in str(caught.value)


def test_cleanup_transport_errors_preserve_original_failure_and_attempt_volume(caplog):
    from requests.exceptions import ReadTimeout
    calls = []
    def remove_container(**kwargs):
        calls.append('container')
        raise ReadTimeout('container timeout')
    def remove_volume(**kwargs):
        calls.append('volume')
        raise ReadTimeout('volume timeout')
    sandbox = DockerSandbox(SimpleNamespace(), job_id='cleanup-job')
    sandbox.container = SimpleNamespace(remove=remove_container)
    sandbox.volume = SimpleNamespace(remove=remove_volume)
    original = SandboxRuntimeError('SANDBOX_ARTIFACT_SIZE', 'original failure')
    with pytest.raises(SandboxRuntimeError) as caught:
        try:
            raise original
        finally:
            sandbox.__exit__(SandboxRuntimeError, original, None)
    assert caught.value is original
    assert calls == ['container', 'volume']
    assert 'container cleanup failed job_id=cleanup-job' in caplog.text
    assert 'volume cleanup failed job_id=cleanup-job' in caplog.text
