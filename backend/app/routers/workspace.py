from __future__ import annotations

from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import current_user
from ..models import Conversation, User, WorkspaceFile
from ..schemas import Message, WorkspaceArtifactCreate, WorkspaceFileRead
from ..services import add_audit
from ..storage import storage
from ..workspace_service import (
    WorkspaceFileError,
    extract_workspace_text,
    file_sha256,
    safe_content_type,
    safe_workspace_filename,
    workspace_file_read,
)


router = APIRouter(tags=["workspace"])


def _owned_conversation(db: Session, conversation_id: str, user: User) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _owned_file(
    db: Session,
    conversation_id: str,
    file_id: str,
    user: User,
) -> WorkspaceFile:
    file = db.scalar(
        select(WorkspaceFile).where(
            WorkspaceFile.id == file_id,
            WorkspaceFile.conversation_id == conversation_id,
            WorkspaceFile.user_id == user.id,
        )
    )
    if file is None:
        raise HTTPException(status_code=404, detail="Workspace file not found")
    return file


def _check_file_capacity(db: Session, conversation_id: str) -> None:
    count = db.scalar(
        select(func.count()).select_from(WorkspaceFile).where(
            WorkspaceFile.conversation_id == conversation_id
        )
    ) or 0
    if count >= max(1, settings.workspace_max_files):
        raise HTTPException(status_code=409, detail="Conversation workspace file limit reached")


def _store_file(
    db: Session,
    *,
    conversation: Conversation,
    user: User,
    filename: str,
    content_type: str,
    data: bytes,
    source: str,
    extracted_text: str | None,
) -> WorkspaceFile:
    file = WorkspaceFile(
        user_id=user.id,
        conversation_id=conversation.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=file_sha256(data),
        storage_path="pending",
        source=source,
        extracted_text=extracted_text,
    )
    db.add(file)
    db.flush()
    key = f"workspaces/{user.id}/{conversation.id}/{file.id}/{filename}"
    try:
        file.storage_path = storage.put(key, data)
        add_audit(
            db,
            actor=user,
            action=f"workspace.file.{source}",
            resource_type="workspace_file",
            resource_id=file.id,
            details={
                "conversation_id": conversation.id,
                "filename": filename,
                "size_bytes": len(data),
                "readable": extracted_text is not None,
            },
        )
        db.commit()
        db.refresh(file)
        return file
    except Exception:
        db.rollback()
        try:
            storage.delete(key)
        except OSError:
            pass
        raise


@router.get(
    "/conversations/{conversation_id}/files",
    response_model=list[WorkspaceFileRead],
)
def list_workspace_files(
    conversation_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[WorkspaceFileRead]:
    _owned_conversation(db, conversation_id, user)
    files = db.scalars(
        select(WorkspaceFile)
        .where(
            WorkspaceFile.conversation_id == conversation_id,
            WorkspaceFile.user_id == user.id,
        )
        .order_by(WorkspaceFile.created_at.asc())
    ).all()
    return [workspace_file_read(item) for item in files]


@router.post(
    "/conversations/{conversation_id}/files",
    response_model=WorkspaceFileRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_workspace_file(
    conversation_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> WorkspaceFileRead:
    conversation = _owned_conversation(db, conversation_id, user)
    if conversation.is_running:
        raise HTTPException(status_code=409, detail="Cannot change files while the conversation is running")
    _check_file_capacity(db, conversation.id)
    try:
        filename = safe_workspace_filename(file.filename)
        data = await file.read(settings.workspace_max_file_bytes + 1)
        if not data:
            raise WorkspaceFileError("File is empty")
        if len(data) > settings.workspace_max_file_bytes:
            raise WorkspaceFileError("File exceeds the workspace upload limit")
        extracted_text = extract_workspace_text(filename, data)
    except WorkspaceFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored: WorkspaceFile | None = None
    try:
        stored = _store_file(
            db,
            conversation=conversation,
            user=user,
            filename=filename,
            content_type=safe_content_type(file.content_type),
            data=data,
            source="upload",
            extracted_text=extracted_text,
        )
        return workspace_file_read(stored)
    except Exception:
        db.rollback()
        if stored and stored.storage_path != "pending":
            storage.delete(stored.storage_path)
        raise


@router.post(
    "/conversations/{conversation_id}/artifacts",
    response_model=WorkspaceFileRead,
    status_code=status.HTTP_201_CREATED,
)
def create_text_artifact(
    conversation_id: str,
    payload: WorkspaceArtifactCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> WorkspaceFileRead:
    conversation = _owned_conversation(db, conversation_id, user)
    if conversation.is_running:
        raise HTTPException(status_code=409, detail="Cannot create artifacts while the conversation is running")
    _check_file_capacity(db, conversation.id)
    try:
        filename = safe_workspace_filename(payload.filename)
    except WorkspaceFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "." not in filename:
        filename += ".txt"
    data = payload.content.encode("utf-8")
    if len(data) > settings.workspace_max_file_bytes:
        raise HTTPException(status_code=422, detail="Artifact exceeds the workspace file limit")
    stored = _store_file(
        db,
        conversation=conversation,
        user=user,
        filename=filename,
        content_type="text/plain; charset=utf-8",
        data=data,
        source="generated",
        extracted_text=payload.content[: max(1000, settings.workspace_extract_max_chars)],
    )
    return workspace_file_read(stored)


@router.get("/conversations/{conversation_id}/files/{file_id}/download")
def download_workspace_file(
    conversation_id: str,
    file_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    file = _owned_file(db, conversation_id, file_id, user)
    if file.purged_at is not None:
        raise HTTPException(status_code=410, detail="文件已超过 15 天保留期")
    data = storage.read(file.storage_path)
    add_audit(
        db,
        actor=user,
        action="workspace.file.download",
        resource_type="workspace_file",
        resource_id=file.id,
        details={"conversation_id": conversation_id, "filename": file.filename},
    )
    db.commit()
    encoded_name = quote(file.filename)
    return StreamingResponse(
        BytesIO(data),
        media_type=file.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@router.delete(
    "/conversations/{conversation_id}/files/{file_id}",
    response_model=Message,
)
def delete_workspace_file(
    conversation_id: str,
    file_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Message:
    conversation = _owned_conversation(db, conversation_id, user)
    if conversation.is_running:
        raise HTTPException(status_code=409, detail="Cannot change files while the conversation is running")
    file = _owned_file(db, conversation_id, file_id, user)
    storage_path = file.storage_path
    filename = file.filename
    db.delete(file)
    add_audit(
        db,
        actor=user,
        action="workspace.file.delete",
        resource_type="workspace_file",
        resource_id=file.id,
        details={"conversation_id": conversation.id, "filename": filename},
    )
    db.commit()
    storage.delete(storage_path)
    return Message(message="Workspace file deleted")
