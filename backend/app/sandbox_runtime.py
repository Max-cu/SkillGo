from __future__ import annotations

import asyncio
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from .config import settings


WORKSPACE_ROOT = PurePosixPath("/workspace")


class SandboxRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SandboxCommandResult:
    exit_code: int
    stdout: str
    stderr: str


def _workspace_path(value: str, *, allow_root: bool = True) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    if ".." in path.parts or not path.is_relative_to(WORKSPACE_ROOT):
        raise SandboxRuntimeError("SANDBOX_PATH_DENIED", "Path must stay inside /workspace")
    if not allow_root and path == WORKSPACE_ROOT:
        raise SandboxRuntimeError("SANDBOX_PATH_DENIED", "A file path is required")
    return str(path)


def _decode(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


class DockerSandbox:
    """One short-lived gVisor container for one workflow job.

    The trusted Worker owns the Docker socket. The untrusted sandbox never sees
    that socket, database credentials, model credentials, or another tenant's
    files.
    """

    def __init__(
        self,
        client: docker.DockerClient,
        *,
        job_id: str,
        execution_id: str | None = None,
        network_enabled: bool = False,
    ) -> None:
        self.client = client
        self.job_id = job_id
        self.execution_id = execution_id or job_id
        self.network_enabled = network_enabled
        self.container = None
        self.volume = None

    def start(self) -> None:
        try:
            self.volume = self.client.volumes.create(
                name=f"skillgo-workspace-{self.execution_id}",
                labels={
                    "skillgo.workspace": "true",
                    "skillgo.job_id": self.job_id,
                    "skillgo.execution_id": self.execution_id,
                },
            )
        except DockerException as exc:
            raise SandboxRuntimeError(
                "SANDBOX_WORKSPACE_FAILED", f"Could not create isolated workspace: {exc}"
            ) from exc

    def _ensure_container(self) -> None:
        if self.container is not None:
            return
        if self.volume is None:
            raise SandboxRuntimeError("SANDBOX_NOT_RUNNING", "Sandbox workspace is not ready")
        kwargs = {
            "image": settings.sandbox_image,
            "command": ["sleep", "infinity"],
            "name": f"skillgo-job-{self.execution_id}",
            "detach": True,
            "user": "10001:10001",
            "network_mode": "bridge" if self.network_enabled else "none",
            "read_only": True,
            "volumes": {self.volume.name: {"bind": "/workspace", "mode": "rw"}},
            "tmpfs": {
                "/tmp": "rw,nosuid,nodev,noexec,size=128m,uid=10001,gid=10001,mode=0700",
            },
            "mem_limit": settings.sandbox_memory,
            "nano_cpus": settings.sandbox_nano_cpus,
            "pids_limit": settings.sandbox_pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "environment": {
                "HOME": "/workspace/home",
                "PIP_TARGET": "/workspace/deps/python",
                "PYTHONPATH": "/workspace/deps/python",
                "NPM_CONFIG_CACHE": "/workspace/.npm-cache",
                "NPM_CONFIG_PREFIX": "/workspace/deps/node",
                "NODE_PATH": "/workspace/deps/node/lib/node_modules:/workspace/node_modules",
            },
            "labels": {
                "skillgo.sandbox": "true",
                "skillgo.job_id": self.job_id,
                "skillgo.execution_id": self.execution_id,
            },
        }
        if settings.sandbox_runtime:
            kwargs["runtime"] = settings.sandbox_runtime
        try:
            self.container = self.client.containers.create(**kwargs)
            self.container.start()
        except ImageNotFound as exc:
            raise SandboxRuntimeError(
                "SANDBOX_IMAGE_MISSING", f"Sandbox image is unavailable: {settings.sandbox_image}"
            ) from exc
        except DockerException as exc:
            raise SandboxRuntimeError("SANDBOX_START_FAILED", f"Could not start sandbox: {exc}") from exc

    def close(self) -> None:
        if self.container is not None:
            try:
                self.container.remove(force=True)
            except NotFound:
                pass
            except DockerException:
                pass
            finally:
                self.container = None
        if self.volume is not None:
            try:
                self.volume.remove(force=True)
            except DockerException:
                pass
            finally:
                self.volume = None

    def __enter__(self) -> "DockerSandbox":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def put_files(self, files: dict[str, bytes]) -> None:
        if self.volume is None:
            raise SandboxRuntimeError("SANDBOX_NOT_RUNNING", "Sandbox workspace is not ready")
        if self.container is not None:
            raise SandboxRuntimeError(
                "SANDBOX_ALREADY_RUNNING", "Initial files must be staged before command execution"
            )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as output:
            for raw_path, data in files.items():
                absolute = PurePosixPath(_workspace_path(raw_path, allow_root=False))
                relative = str(absolute.relative_to(WORKSPACE_ROOT))
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = 0o600
                info.uid = 10001
                info.gid = 10001
                output.addfile(info, io.BytesIO(data))
        archive.seek(0)
        helper = None
        try:
            helper = self.client.containers.create(
                image=settings.sandbox_image,
                command=["sleep", "infinity"],
                name=f"skillgo-stage-{self.execution_id}",
                detach=True,
                user="0:0",
                network_mode="none",
                volumes={self.volume.name: {"bind": "/workspace", "mode": "rw"}},
                mem_limit="128m",
                pids_limit=32,
                cap_drop=["ALL"],
                cap_add=["CHOWN"],
                security_opt=["no-new-privileges:true"],
                labels={
                    "skillgo.stager": "true",
                    "skillgo.job_id": self.job_id,
                    "skillgo.execution_id": self.execution_id,
                },
            )
            helper.start()
            if not helper.put_archive(str(WORKSPACE_ROOT), archive.getvalue()):
                raise SandboxRuntimeError("SANDBOX_UPLOAD_FAILED", "Sandbox rejected uploaded files")
            ownership = helper.exec_run(
                ["chown", "-R", "10001:10001", "/workspace"],
                user="0:0",
            )
            if ownership.exit_code != 0:
                raise SandboxRuntimeError("SANDBOX_UPLOAD_FAILED", "Could not secure workspace ownership")
        except DockerException as exc:
            raise SandboxRuntimeError("SANDBOX_UPLOAD_FAILED", f"Could not upload files: {exc}") from exc
        finally:
            if helper is not None:
                try:
                    helper.remove(force=True)
                except DockerException:
                    pass

    async def command(
        self,
        argv: list[str],
        *,
        cwd: str = "/workspace",
        timeout_seconds: int | None = None,
        allow_large_arguments: bool = False,
    ) -> SandboxCommandResult:
        self._ensure_container()
        max_argument_chars = 384 * 1024 if allow_large_arguments else 4096
        if (
            not argv
            or len(argv) > 64
            or any(
                not isinstance(item, str) or len(item) > max_argument_chars
                for item in argv
            )
        ):
            max_actual_chars = max(
                (len(item) for item in argv if isinstance(item, str)),
                default=0,
            )
            raise SandboxRuntimeError(
                "SANDBOX_COMMAND_INVALID",
                (
                    "Command argv is invalid "
                    f"(items={len(argv)}, max_argument_chars={max_actual_chars}, "
                    f"allowed={max_argument_chars})"
                ),
            )
        workdir = _workspace_path(cwd)
        timeout = min(
            max(int(timeout_seconds or settings.sandbox_command_timeout_seconds), 1),
            settings.sandbox_command_timeout_seconds,
        )

        def execute() -> object:
            return self.container.exec_run(
                argv,
                workdir=workdir,
                user="10001:10001",
                demux=True,
                environment={"HOME": "/tmp", "NODE_PATH": "/usr/local/lib/node_modules"},
            )

        try:
            result = await asyncio.wait_for(asyncio.to_thread(execute), timeout=timeout)
        except TimeoutError as exc:
            self.close()
            raise SandboxRuntimeError(
                "SANDBOX_COMMAND_TIMEOUT", f"Command exceeded {timeout} seconds; sandbox was destroyed"
            ) from exc
        except DockerException as exc:
            raise SandboxRuntimeError("SANDBOX_COMMAND_FAILED", f"Command transport failed: {exc}") from exc
        stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
        return SandboxCommandResult(
            exit_code=int(result.exit_code),
            stdout=_decode(stdout)[:40_000],
            stderr=_decode(stderr)[:20_000],
        )

    async def list_files(self, path: str = "/workspace") -> list[dict]:
        target = _workspace_path(path)
        source = """
import json
import sys
from pathlib import Path

root = Path("/workspace").resolve()
path = Path(sys.argv[1]).resolve()
if path != root and root not in path.parents:
    raise PermissionError("path must stay inside /workspace")
if not path.exists():
    raise FileNotFoundError(f"directory does not exist: {path}")
if not path.is_dir():
    raise NotADirectoryError(f"not a directory: {path}")
items = []
for item in sorted(path.rglob("*"))[:500]:
    if item.is_symlink():
        continue
    items.append({
        "path": str(item),
        "size": item.stat().st_size,
        "type": "dir" if item.is_dir() else "file",
    })
print(json.dumps(items, ensure_ascii=False))
"""
        result = await self.command(["python3", "-c", source, target], timeout_seconds=30)
        if result.exit_code != 0:
            raise SandboxRuntimeError("SANDBOX_LIST_FAILED", result.stderr or "Could not list files")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxRuntimeError("SANDBOX_LIST_FAILED", "Sandbox returned an invalid file list") from exc
        return value if isinstance(value, list) else []

    async def read_text(self, path: str, *, offset: int = 0, limit: int = 30_000) -> str:
        target = _workspace_path(path, allow_root=False)
        offset = max(offset, 0)
        limit = min(max(limit, 1), 60_000)
        source = """
import sys
from pathlib import Path

root = Path("/workspace").resolve()
path = Path(sys.argv[1]).resolve()
if root not in path.parents:
    raise PermissionError("path must stay inside /workspace")
if not path.exists():
    raise FileNotFoundError(f"file does not exist: {path}")
if not path.is_file():
    raise IsADirectoryError(f"not a file: {path}")
if path.is_symlink():
    raise PermissionError(f"symbolic links cannot be read: {path}")
data = path.read_text(encoding="utf-8", errors="replace")
start = int(sys.argv[2])
limit = int(sys.argv[3])
print(data[start:start + limit], end="")
"""
        result = await self.command(
            ["python3", "-c", source, target, str(offset), str(limit)], timeout_seconds=30
        )
        if result.exit_code != 0:
            raise SandboxRuntimeError("SANDBOX_READ_FAILED", result.stderr or "Could not read file")
        return result.stdout

    async def write_text(self, path: str, content: str) -> None:
        data = content.encode("utf-8")
        if len(data) > 256 * 1024:
            raise SandboxRuntimeError("SANDBOX_WRITE_TOO_LARGE", "One write is limited to 256 KiB")
        import base64

        target = _workspace_path(path, allow_root=False)
        encoded = base64.b64encode(data).decode("ascii")
        source = """
import base64
import sys
from pathlib import Path

root = Path("/workspace").resolve()
path = Path(sys.argv[1]).resolve()
if root not in path.parents:
    raise PermissionError("path must stay inside /workspace")
if path.exists() and path.is_dir():
    raise IsADirectoryError(f"not a file: {path}")
if path.is_symlink():
    raise PermissionError(f"symbolic links cannot be written: {path}")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(base64.b64decode(sys.argv[2]))
"""
        result = await self.command(
            ["python3", "-c", source, target, encoded],
            timeout_seconds=30,
            # Content is capped at 256 KiB above and Base64 is data, not code.
            # Agent-authored command calls never receive this exemption.
            allow_large_arguments=True,
        )
        if result.exit_code != 0:
            raise SandboxRuntimeError("SANDBOX_WRITE_FAILED", result.stderr or "Could not write file")

    def download_file(self, path: str) -> bytes:
        if self.container is None:
            raise SandboxRuntimeError("SANDBOX_NOT_RUNNING", "Sandbox is not running")
        target = _workspace_path(path, allow_root=False)
        if not target.startswith("/workspace/output/"):
            raise SandboxRuntimeError("SANDBOX_ARTIFACT_DENIED", "Artifacts must be under /workspace/output")
        try:
            stream, stat = self.container.get_archive(target)
            size = int(stat.get("size") or 0)
            if size <= 0 or size > settings.sandbox_max_artifact_bytes:
                raise SandboxRuntimeError(
                    "SANDBOX_ARTIFACT_SIZE", "Artifact is empty or exceeds the configured limit"
                )
            payload = b"".join(stream)
        except NotFound as exc:
            raise SandboxRuntimeError("SANDBOX_ARTIFACT_MISSING", f"Artifact does not exist: {target}") from exc
        except DockerException as exc:
            raise SandboxRuntimeError("SANDBOX_ARTIFACT_DOWNLOAD_FAILED", str(exc)) from exc
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members = [item for item in archive.getmembers() if item.isfile() and not item.issym()]
            if len(members) != 1:
                raise SandboxRuntimeError("SANDBOX_ARTIFACT_INVALID", "Expected one regular artifact file")
            source = archive.extractfile(members[0])
            data = source.read() if source else b""
        if len(data) != size:
            raise SandboxRuntimeError("SANDBOX_ARTIFACT_INVALID", "Artifact size changed during collection")
        return data


def docker_client() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
        info = client.info()
    except DockerException as exc:
        raise SandboxRuntimeError("DOCKER_UNAVAILABLE", f"Docker daemon is unavailable: {exc}") from exc
    if settings.sandbox_runtime and settings.sandbox_runtime not in (info.get("Runtimes") or {}):
        raise SandboxRuntimeError(
            "SANDBOX_RUNTIME_MISSING",
            f"Docker runtime {settings.sandbox_runtime!r} is not registered",
        )
    try:
        client.images.get(settings.sandbox_image)
    except ImageNotFound as exc:
        raise SandboxRuntimeError(
            "SANDBOX_IMAGE_MISSING", f"Sandbox image is unavailable: {settings.sandbox_image}"
        ) from exc
    return client


def cleanup_stale_sandboxes(
    client: docker.DockerClient,
    *,
    protected_job_ids: set[str] | None = None,
) -> None:
    protected = protected_job_ids or set()
    for label in ("skillgo.sandbox=true", "skillgo.stager=true"):
        for container in client.containers.list(all=True, filters={"label": label}):
            if str((container.labels or {}).get("skillgo.job_id") or "") in protected:
                continue
            try:
                container.remove(force=True)
            except DockerException:
                continue
    for volume in client.volumes.list(filters={"label": "skillgo.workspace=true"}):
        if str((volume.attrs.get("Labels") or {}).get("skillgo.job_id") or "") in protected:
            continue
        try:
            volume.remove(force=True)
        except DockerException:
            continue


def cleanup_job_sandboxes(client: docker.DockerClient, job_id: str) -> None:
    """Remove only containers and workspaces owned by one expired job lease."""

    for label in ("skillgo.sandbox=true", "skillgo.stager=true"):
        for container in client.containers.list(
            all=True,
            filters={"label": [label, f"skillgo.job_id={job_id}"]},
        ):
            try:
                container.remove(force=True)
            except DockerException:
                continue
    for volume in client.volumes.list(
        filters={"label": ["skillgo.workspace=true", f"skillgo.job_id={job_id}"]}
    ):
        try:
            volume.remove(force=True)
        except DockerException:
            continue


def cleanup_execution_sandbox(
    client: docker.DockerClient,
    *,
    job_id: str,
    execution_id: str,
) -> None:
    """Remove one expired attempt without touching a newer attempt of the job."""

    for label in ("skillgo.sandbox=true", "skillgo.stager=true"):
        for container in client.containers.list(
            all=True, filters={"label": [label, f"skillgo.job_id={job_id}"]}
        ):
            labels = container.labels or {}
            recorded_execution = str(labels.get("skillgo.execution_id") or job_id)
            if recorded_execution != execution_id:
                continue
            try:
                container.remove(force=True)
            except DockerException:
                continue
    for volume in client.volumes.list(
        filters={"label": ["skillgo.workspace=true", f"skillgo.job_id={job_id}"]}
    ):
        labels = volume.attrs.get("Labels") or {}
        recorded_execution = str(labels.get("skillgo.execution_id") or job_id)
        if recorded_execution != execution_id:
            continue
        try:
            volume.remove(force=True)
        except DockerException:
            continue


def package_skill_root(file_names: Iterable[str], *, base_root: str = "/workspace/skill") -> str:
    candidates = []
    for name in file_names:
        path = PurePosixPath(name.replace("\\", "/"))
        if path.name.lower() == "skill.md" and ".." not in path.parts:
            candidates.append(path.parent)
    if len(candidates) != 1:
        raise SandboxRuntimeError("SKILL_PACKAGE_INVALID", "Package must contain exactly one SKILL.md")
    relative = str(candidates[0])
    normalized_base = str(PurePosixPath(base_root))
    return normalized_base if relative == "." else f"{normalized_base}/{relative}"
