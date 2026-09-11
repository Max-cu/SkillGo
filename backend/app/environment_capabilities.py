"""Platform-owned capability catalogue. Skill declarations never select packages or grant network."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .skill_metadata import parse_skill_frontmatter
from .sandbox_runtime import SandboxRuntimeError

CATALOG_VERSION = 1
# Each capability is satisfied by any complete provider group.
CAPABILITIES = {
    "pdf.read": [["pymupdf"], ["pypdf"], ["pdfplumber"]],
    "pdf.render": [["pymupdf"], ["pdftoppm"]],
    "pdf.write": [["pymupdf"], ["reportlab"]],
    "pdf.annotate": [["pymupdf"], ["pypdf", "reportlab"]],
    "office.docx": [["python-docx"]],
    "office.xlsx": [["openpyxl"]],
    "office.pptx": [["python-pptx"]],
    "office.convert": [["libreoffice"]],
    "image.basic": [["Pillow"]],
    "data.tabular": [["pandas"]],
    "media.convert": [["ffmpeg"]],
    "fonts.cjk": [["fonts.cjk"]],
}


def capability_requirements(skill_md: str, manifest: dict) -> dict:
    frontmatter = parse_skill_frontmatter(skill_md)
    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    declared = []
    for source in (frontmatter, spec):
        value = source.get("capabilities", [])
        if not isinstance(value, list) or len(value) > 32 or any(not isinstance(x, str) or not x or len(x) > 80 for x in value):
            raise ValueError("capabilities must be an array of at most 32 non-empty capability names")
        declared.extend(value)
    # Inference is advisory only: examples/negated prose must not block a task.
    lower = skill_md.casefold()
    hints = set()
    for needle, capabilities in {
        "pdf": ["pdf.read", "pdf.render"], "docx": ["office.docx"],
        "xlsx": ["office.xlsx"], "pptx": ["office.pptx"],
        "中文": ["fonts.cjk"],
    }.items():
        if needle in lower:
            hints.update(capabilities)
    return {"declared_capabilities": sorted(set(declared)), "inferred_capabilities": sorted(hints - set(declared))}


def resolve_capabilities(providers: dict) -> list[str]:
    return sorted(name for name, groups in CAPABILITIES.items()
                  if any(all(providers.get(key, {}).get("available") is True for key in group) for group in groups))


async def preflight_environment(sandbox, skill_contexts: list[dict]) -> dict:
    # -I excludes workspace/PYTHONPATH packages; only the platform image is probed.
    script = Path(__file__).with_name("environment_probe.py").read_text(encoding="utf-8")
    result = await sandbox.command(["python3", "-I", "-c", script], cwd="/workspace", timeout_seconds=90, allow_large_arguments=True)
    try:
        payload = json.loads(result.stdout)
        if (result.exit_code or not isinstance(payload.get("providers"), dict)
                or payload.get("schema_version") != 1
                or any(not isinstance(value, dict) or type(value.get('available')) is not bool
                       for value in payload['providers'].values())):
            raise ValueError("Invalid environment probe")
    except (ValueError, TypeError, AttributeError) as exc:
        raise SandboxRuntimeError("SANDBOX_ENVIRONMENT_PROBE_FAILED", "Could not verify the platform runtime environment") from exc
    payload["catalog_version"] = CATALOG_VERSION
    container = getattr(sandbox, "container", None)
    payload["image_id"] = (getattr(container, "attrs", {}) or {}).get("Image")
    payload["inventory_digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    payload["capabilities"] = resolve_capabilities(payload["providers"])
    payload["network_enabled"] = bool(sandbox.network_enabled)
    payload["environment_upgrade_supported"] = False
    missing = sorted({name for ctx in skill_contexts for name in ctx.get("runtime_requirements", {}).get("declared_capabilities", [])} - set(payload["capabilities"]))
    if missing:
        raise SandboxRuntimeError("SANDBOX_DEPENDENCY_MISSING", "Declared capabilities unavailable: " + ", ".join(missing) + ". Ask the platform administrator to prepare a compatible runtime; task network permission is unchanged.")
    return payload
