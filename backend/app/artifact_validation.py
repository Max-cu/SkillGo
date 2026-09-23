from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .sandbox_runtime import SandboxRuntimeError

if TYPE_CHECKING:
    from .sandbox_runtime import DockerSandbox


OUTPUT_ROOT = PurePosixPath("/workspace/output")
ARTIFACT_FILENAME_MAX = 180
# Characters that are unsafe in storage keys, download filenames or common
# filesystems; unicode letters/digits and CJK names are intentionally allowed.
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f<>:\"/\\|?*\u007f]+")


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


def _sanitize_name_segment(segment: str) -> str:
    """Make one path segment safe to use inside a flattened stored filename."""

    cleaned = _UNSAFE_NAME_CHARS.sub("_", segment).strip(" ._")
    return cleaned or "file"


def _cap_filename_length(name: str, limit: int = ARTIFACT_FILENAME_MAX) -> str:
    if len(name) <= limit:
        return name
    suffix = PurePosixPath(name).suffix
    if suffix and len(suffix) < limit - 8:
        return PurePosixPath(name).stem[: limit - len(suffix)] + suffix
    return name[:limit]


def unique_artifact_filenames(paths: list[str]) -> dict[str, str]:
    """Map every declared artifact path to a unique, safe stored filename.

    Output trees commonly contain same-named reports in different folders
    (for example 目录校验报告/report.md and 综合报告/report.md). Flattening
    with only the basename used to silently drop every duplicate; instead such
    files are prefixed with their parent directory (recursively, until unique).
    A numeric suffix is the final guarantee when flattened names still collide.
    The same path declared more than once collapses to a single entry (it is
    the same file); iteration order follows first occurrence.
    """

    chosen: dict[str, str] = {}
    seen_paths: set[str] = set()

    def relative_dirs(path: PurePosixPath) -> list[str]:
        try:
            return list(path.relative_to(OUTPUT_ROOT).parts[:-1])
        except ValueError:
            # Paths should already be normalized under OUTPUT_ROOT; keep a sane
            # fallback so naming never fails artifact collection.
            parts = [part for part in path.parent.parts if part not in ("/", "")]
            return parts[1:] if parts and parts[0] == "workspace" else parts

    for raw_path in paths:
        if raw_path in seen_paths:
            continue
        seen_paths.add(raw_path)
        path = PurePosixPath(raw_path)
        base = _sanitize_name_segment(path.name)
        dirs = [_sanitize_name_segment(part) for part in relative_dirs(path)]
        candidate = base
        if candidate in chosen:
            for depth in range(1, len(dirs) + 1):
                candidate = "_".join(dirs[-depth:] + [base])
                if candidate not in chosen:
                    break
        candidate = _cap_filename_length(candidate)
        if candidate in chosen:
            stem = PurePosixPath(candidate).stem
            suffix = PurePosixPath(candidate).suffix
            if len(suffix) >= ARTIFACT_FILENAME_MAX - 8:
                stem, suffix = candidate, ""
            counter = 2
            while True:
                suffix_text = f"_{counter}{suffix}"
                candidate = f"{stem[:ARTIFACT_FILENAME_MAX - len(suffix_text)]}{suffix_text}"
                if candidate not in chosen:
                    break
                counter += 1
        chosen[candidate] = raw_path

    # Invert so callers can look the stored name up by declared path.
    return {raw_path: name for name, raw_path in chosen.items()}


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
