from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .sandbox_runtime import SandboxRuntimeError

if TYPE_CHECKING:
    from .sandbox_runtime import DockerSandbox


OUTPUT_ROOT = PurePosixPath("/workspace/output")


def normalize_artifact_paths(paths: list[str]) -> list[str]:
    """Normalize declared artifact paths and keep them below the output root."""

    normalized: list[str] = []
    for raw_path in paths:
        path = PurePosixPath(raw_path)
        if not path.is_absolute():
            path = PurePosixPath("/workspace") / path
        if ".." in path.parts or path == OUTPUT_ROOT or not path.is_relative_to(OUTPUT_ROOT):
            raise SandboxRuntimeError(
                "SANDBOX_ARTIFACT_DENIED",
                "Artifacts must be regular files under /workspace/output",
            )
        normalized.append(str(path))
    return normalized


def validate_artifact_content(filename: str, data: bytes) -> None:
    """Reject corrupt structured deliverables before they leave the sandbox."""

    if not data:
        raise SandboxRuntimeError(
            "ARTIFACT_CONTENT_INVALID", f"Artifact is empty: {filename}"
        )
    suffix = PurePosixPath(filename).suffix.lower()
    office_members = {
        ".docx": {"[Content_Types].xml", "word/document.xml"},
        ".xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
        ".pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
    }
    if suffix in office_members:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                members = archive.infolist()
                if (
                    len(members) > 10_000
                    or sum(item.file_size for item in members) > 512 * 1024 * 1024
                ):
                    raise SandboxRuntimeError(
                        "ARTIFACT_CONTENT_INVALID",
                        f"Office artifact expands beyond verification limits: {filename}",
                    )
                bad_member = archive.testzip()
        except SandboxRuntimeError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise SandboxRuntimeError(
                "ARTIFACT_CONTENT_INVALID",
                f"Invalid {suffix[1:].upper()} package: {filename}",
            ) from exc
        missing = sorted(office_members[suffix] - names)
        if bad_member or missing:
            raise SandboxRuntimeError(
                "ARTIFACT_CONTENT_INVALID",
                (
                    f"Corrupt {suffix[1:].upper()} artifact {filename}; "
                    f"missing={missing}, bad_member={bad_member}"
                ),
            )
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise SandboxRuntimeError(
                "ARTIFACT_CONTENT_INVALID", f"Invalid PDF structure: {filename}"
            )
    elif suffix == ".json":
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxRuntimeError(
                "ARTIFACT_CONTENT_INVALID", f"Invalid JSON artifact: {filename}"
            ) from exc
    elif suffix in {".txt", ".md", ".csv"}:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxRuntimeError(
                "ARTIFACT_CONTENT_INVALID", f"Text artifact is not UTF-8: {filename}"
            ) from exc
        if not text.strip():
            raise SandboxRuntimeError(
                "ARTIFACT_CONTENT_INVALID", f"Text artifact is blank: {filename}"
            )


async def snapshot_sandbox_artifacts(
    sandbox: "DockerSandbox",
    paths: list[str] | None = None,
) -> dict[str, str]:
    """Return a verified path-to-SHA256 snapshot of current output artifacts.

    The snapshot is the trusted binding between a verifier result and the exact
    bytes that were verified.  It is recomputed again before ``finish``.
    """

    if paths is None:
        try:
            tree = await sandbox.list_files(str(OUTPUT_ROOT))
        except SandboxRuntimeError as exc:
            if exc.code == "SANDBOX_LIST_FAILED":
                return {}
            raise
        paths = sorted(
            str(item.get("path"))
            for item in tree
            if item.get("type") == "file" and isinstance(item.get("path"), str)
        )
    normalized = normalize_artifact_paths(paths)
    snapshot: dict[str, str] = {}
    for path in normalized:
        data = sandbox.download_file(path)
        validate_artifact_content(PurePosixPath(path).name, data)
        snapshot[path] = hashlib.sha256(data).hexdigest()
    return snapshot


# Transitional aliases keep the existing internal/test imports stable while
# the worker is split into focused modules.
_normalize_artifact_paths = normalize_artifact_paths
_validate_artifact_content = validate_artifact_content
