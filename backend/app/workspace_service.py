from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import WorkspaceFile
from .schemas import WorkspaceFileRead


class WorkspaceFileError(ValueError):
    pass


TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".html",
    ".htm",
    ".xml",
}
BLOCKED_SUFFIXES = {
    ".exe",
    ".dll",
    ".msi",
    ".com",
    ".scr",
    ".bat",
    ".cmd",
    ".ps1",
    ".sh",
    ".py",
    ".js",
    ".jar",
    ".lnk",
}


def safe_workspace_filename(raw_name: str | None) -> str:
    raw_name = (raw_name or "").strip()
    normalized = raw_name.replace("\\", "/")
    if not normalized or normalized in {".", ".."} or "/" in normalized:
        raise WorkspaceFileError("File name must not contain a path")
    filename = PurePosixPath(normalized).name
    if len(filename) > 180 or any(ord(char) < 32 for char in filename):
        raise WorkspaceFileError("File name is invalid or too long")
    if PurePosixPath(filename).suffix.lower() in BLOCKED_SUFFIXES:
        raise WorkspaceFileError("Executable and script files are not allowed")
    return filename


def safe_content_type(raw: str | None) -> str:
    value = (raw or "application/octet-stream").strip()
    if not value or len(value) > 160 or "\r" in value or "\n" in value:
        return "application/octet-stream"
    return value


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_text(data: bytes) -> str | None:
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


def _validated_archive(data: bytes) -> ZipFile:
    try:
        archive = ZipFile(BytesIO(data))
    except BadZipFile as exc:
        raise WorkspaceFileError("Office document is not a valid ZIP package") from exc
    infos = archive.infolist()
    if len(infos) > 2000 or sum(item.file_size for item in infos) > 50 * 1024 * 1024:
        archive.close()
        raise WorkspaceFileError("Office document expands beyond the safe limit")
    return archive


def _extract_docx(data: bytes) -> str:
    with _validated_archive(data) as archive:
        try:
            document = archive.read("word/document.xml")
        except KeyError as exc:
            raise WorkspaceFileError("DOCX document is missing word/document.xml") from exc
    root = ElementTree.fromstring(document)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_xlsx(data: bytes) -> str:
    with _validated_archive(data) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")) for item in shared_root]
        sheet_names = sorted(
            name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        )[:20]
        rendered_sheets: list[str] = []
        for sheet_name in sheet_names:
            root = ElementTree.fromstring(archive.read(sheet_name))
            rows: list[str] = []
            for row in (node for node in root.iter() if node.tag.endswith("}row")):
                values: list[str] = []
                for cell in (node for node in row if node.tag.endswith("}c")):
                    cell_type = cell.attrib.get("t")
                    value_node = next((node for node in cell.iter() if node.tag.endswith("}v")), None)
                    inline_nodes = [node.text or "" for node in cell.iter() if node.tag.endswith("}t")]
                    value = "".join(inline_nodes) if inline_nodes else (value_node.text if value_node is not None and value_node.text else "")
                    if cell_type == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    values.append(value)
                if any(values):
                    rows.append("\t".join(values))
            rendered_sheets.append(f"[{PurePosixPath(sheet_name).stem}]\n" + "\n".join(rows))
    return "\n\n".join(rendered_sheets)


def extract_workspace_text(filename: str, data: bytes) -> str | None:
    suffix = PurePosixPath(filename).suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            extracted = _decode_text(data)
        elif suffix == ".docx":
            extracted = _extract_docx(data)
        elif suffix == ".xlsx":
            extracted = _extract_xlsx(data)
        else:
            return None
    except ElementTree.ParseError as exc:
        raise WorkspaceFileError("Office document contains invalid XML") from exc
    if extracted is None:
        return None
    limit = max(1000, settings.workspace_extract_max_chars)
    return extracted[:limit]


def workspace_file_read(file: WorkspaceFile) -> WorkspaceFileRead:
    return WorkspaceFileRead(
        id=file.id,
        conversation_id=file.conversation_id,
        filename=file.filename,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        source=file.source,
        readable=file.extracted_text is not None,
        created_at=file.created_at,
    )


def workspace_context(db: Session, conversation_id: str) -> list[dict[str, str]]:
    limit = max(0, settings.workspace_context_max_chars)
    if not limit:
        return []
    candidates = list(
        db.scalars(
            select(WorkspaceFile)
            .where(
                WorkspaceFile.conversation_id == conversation_id,
                WorkspaceFile.extracted_text.is_not(None),
            )
            .order_by(WorkspaceFile.created_at.desc())
            .limit(max(1, settings.workspace_max_files))
        ).all()
    )
    selected: list[dict[str, str]] = []
    used = 0
    for file in candidates:
        content = file.extracted_text or ""
        available = limit - used - len(file.filename) - 32
        if available <= 0:
            break
        excerpt = content[:available]
        selected.append({"filename": file.filename, "content": excerpt})
        used += len(excerpt) + len(file.filename) + 32
    selected.reverse()
    return selected
