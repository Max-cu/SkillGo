from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from .config import settings
from .models import SkillType
from .skill_execution_spec import SkillExecutionSpecError, fixed_execution_spec
from .skill_metadata import SkillFrontmatterError, parse_skill_frontmatter


VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class PackageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedPackage:
    sha256: str
    manifest: dict
    skill_md: str
    version: str
    skill_type: SkillType
    input_schema: dict
    output_schema: dict
    permissions: dict
    package_format: str
    version_explicit: bool
    metadata_name: str
    metadata_description: str
    file_names: tuple[str, ...]
    warnings: tuple[str, ...]


def _safe_path(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise PackageValidationError(f"unsafe archive path: {name!r}")
    normalized_name = name.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", normalized_name):
        raise PackageValidationError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(normalized_name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PackageValidationError(f"unsafe archive path: {name!r}")
    if len(path.parts) > 12:
        raise PackageValidationError(f"archive path is too deep: {name!r}")
    return path


def _inferred_skill_name(skill_md: str, skill_path: PurePosixPath) -> str:
    parent_name = skill_path.parent.name if str(skill_path.parent) != "." else ""
    if parent_name and parent_name.casefold() not in {"skill", "skills"}:
        return parent_name
    heading = re.search(r"^#\s+(.+?)\s*$", skill_md, flags=re.MULTILINE)
    if heading:
        return re.sub(r"[*_`#]", "", heading.group(1)).strip()
    return "Imported Skill"


def _inferred_skill_description(skill_md: str, name: str) -> str:
    paragraph: list[str] = []
    lines = skill_md.splitlines()
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        if closing is not None:
            lines = lines[closing + 1 :]
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith(("#", "---", "```", "|", ">")):
            continue
        cleaned = re.sub(r"[*_`\[\]()]", "", line).strip(" -")
        if cleaned:
            paragraph.append(cleaned)
    description = " ".join(paragraph).strip()
    return description[:1024] or f"{name}：从非标准 SKILL.md 兼容导入，请在发布前确认用途说明。"


def validate_skill_package(data: bytes) -> ValidatedPackage:
    if not data or len(data) > settings.max_upload_bytes:
        raise PackageValidationError("package is empty or exceeds the upload limit")

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageValidationError("package must be a valid ZIP archive") from exc

    files: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    casefold_names: set[str] = set()
    for info in archive.infolist():
        path = _safe_path(info.filename)
        if info.is_dir():
            continue
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
            raise PackageValidationError(f"links and special files are forbidden: {info.filename}")
        normalized = path.as_posix()
        folded = normalized.casefold()
        if folded in casefold_names:
            raise PackageValidationError(f"duplicate or case-colliding path: {normalized}")
        casefold_names.add(folded)
        files[normalized] = info
        total_size += info.file_size
        if info.file_size > settings.max_uncompressed_bytes:
            raise PackageValidationError(f"file is too large: {normalized}")

    if len(files) > settings.max_archive_files:
        raise PackageValidationError("archive contains too many files")
    if total_size > settings.max_uncompressed_bytes:
        raise PackageValidationError("archive expands beyond the allowed size")

    warnings: list[str] = []
    skill_candidates = [
        name
        for name in files
        if PurePosixPath(name).name.casefold() == "skill.md"
        and "__macosx" not in {part.casefold() for part in PurePosixPath(name).parts}
        and not any(part.startswith("._") for part in PurePosixPath(name).parts)
    ]
    if not skill_candidates:
        raise PackageValidationError("SKILL.md is required")
    if len(skill_candidates) > 1:
        raise PackageValidationError(
            "package contains multiple SKILL.md files; upload one Skill at a time"
        )
    skill_key = skill_candidates[0]
    skill_path = PurePosixPath(skill_key)
    prefix = "" if str(skill_path.parent) == "." else f"{skill_path.parent.as_posix()}/"
    if skill_path.name != "SKILL.md":
        warnings.append("入口文件不是标准大写 SKILL.md，已按兼容模式识别。")

    try:
        skill_bytes = archive.read(files[skill_key])
        skill_md = skill_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackageValidationError("SKILL.md must be UTF-8") from exc
    except (NotImplementedError, RuntimeError) as exc:
        raise PackageValidationError("SKILL.md uses an unsupported ZIP compression method") from exc
    if skill_bytes.startswith(b"\xef\xbb\xbf"):
        warnings.append("SKILL.md 含 UTF-8 BOM，已自动规范化。")
    if not skill_md.strip() or len(skill_md) > 200_000:
        raise PackageValidationError("SKILL.md is empty or too large")

    try:
        frontmatter = parse_skill_frontmatter(skill_md, strict=True)
    except SkillFrontmatterError as exc:
        raise PackageValidationError(str(exc)) from exc
    standard_name = str(frontmatter.get("name", "")).strip()
    standard_description = str(frontmatter.get("description", "")).strip()

    manifest: dict | None = None
    for filename in ("skillgo.yaml", "manifest.skillgo.yaml", "manifest.yaml"):
        manifest_keys = [prefix + filename]
        if prefix and filename != "manifest.yaml":
            manifest_keys.append(filename)
        for manifest_key in dict.fromkeys(manifest_keys):
            if manifest_key not in files:
                continue
            try:
                candidate = yaml.safe_load(archive.read(files[manifest_key]))
            except UnicodeDecodeError as exc:
                raise PackageValidationError(f"{filename} must be UTF-8") from exc
            except yaml.YAMLError as exc:
                raise PackageValidationError(f"{filename} is invalid YAML") from exc
            except (NotImplementedError, RuntimeError) as exc:
                raise PackageValidationError(
                    f"{filename} uses an unsupported ZIP compression method"
                ) from exc
            if not isinstance(candidate, dict):
                raise PackageValidationError(f"{filename} must contain an object")
            # A generic manifest from another ecosystem must not accidentally become
            # a SkillGo deployment manifest.
            is_skillgo = filename != "manifest.yaml" or str(candidate.get("apiVersion", "")).startswith("skillgo.io/")
            if is_skillgo:
                manifest = candidate
                break
        if manifest is not None:
            break

    package_format = "skillgo" if manifest is not None else "agent-skill"
    if manifest is None:
        if not standard_name or not standard_description:
            inferred_name = standard_name or _inferred_skill_name(skill_md, skill_path)
            inferred_description = standard_description or _inferred_skill_description(
                skill_md, inferred_name
            )
            standard_name = inferred_name
            standard_description = inferred_description
            warnings.append(
                "SKILL.md 缺少标准 name/description Frontmatter；已从目录、标题和正文推断资料，请在发布前确认。"
            )
        manifest = {
            "apiVersion": "skillgo.io/v1alpha1",
            "kind": "Skill",
            "metadata": {
                "name": standard_name,
                "version": "0.1.0",
            },
            "spec": {
                "type": "instruction",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "permissions": {},
            },
            "x-skillgo": {"sourceFormat": "agent-skill"},
        }

    metadata = manifest.get("metadata") or {}
    spec = manifest.get("spec") or {}
    version_explicit = package_format == "skillgo" and bool(metadata.get("version"))
    version = str(metadata.get("version") or "0.1.0")
    if not VERSION_PATTERN.fullmatch(version):
        raise PackageValidationError("metadata.version must be a semantic version")
    raw_type = str(spec.get("type", "instruction"))
    try:
        skill_type = SkillType(raw_type)
    except ValueError as exc:
        raise PackageValidationError("spec.type must be instruction or code") from exc

    input_schema = spec.get("inputSchema") or {"type": "object"}
    output_schema = spec.get("outputSchema") or {"type": "object"}
    permissions = spec.get("permissions") or {}
    if not all(isinstance(item, dict) for item in (input_schema, output_schema, permissions)):
        raise PackageValidationError("schemas and permissions must be objects")
    try:
        fixed_execution_spec(manifest)
    except SkillExecutionSpecError as exc:
        raise PackageValidationError(str(exc)) from exc

    return ValidatedPackage(
        sha256=hashlib.sha256(data).hexdigest(),
        manifest=manifest,
        skill_md=skill_md,
        version=version,
        skill_type=skill_type,
        input_schema=input_schema,
        output_schema=output_schema,
        permissions=permissions,
        package_format=package_format,
        version_explicit=version_explicit,
        metadata_name=str(metadata.get("displayName") or standard_name or metadata.get("name") or "").strip(),
        metadata_description=standard_description,
        file_names=tuple(sorted(files)),
        warnings=tuple(warnings),
    )
