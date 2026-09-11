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
    monkeypatch.setattr('app.sandbox_runtime.settings', SimpleNamespace(sandbox_max_artifact_bytes=100))
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
