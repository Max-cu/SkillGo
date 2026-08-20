from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..conversation_service import can_run_version, conversation_detail, conversation_read
from ..database import get_db
from ..deps import current_user
from ..models import Conversation, ConversationMessage, SkillType, SkillVersion, User, WorkspaceFile, utcnow
from ..schemas import (
    ConversationCreate,
    ConversationDetail,
    ConversationRead,
    ConversationUpdate,
    Message,
)
from ..services import add_audit
from ..storage import storage


router = APIRouter(tags=["conversations"])


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


def _next_conversation_title(db: Session, user: User, skill_id: str) -> str:
    existing = set(
        db.scalars(
            select(Conversation.title).where(
                Conversation.user_id == user.id,
                Conversation.skill_id == skill_id,
            )
        ).all()
    )
    number = 1
    while f"会话 {number}" in existing:
        number += 1
    return f"会话 {number}"


@router.post(
    "/conversations",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    payload: ConversationCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ConversationRead:
    version = db.get(SkillVersion, payload.version_id)
    if version is None or not can_run_version(version, user):
        raise HTTPException(status_code=404, detail="Skill version not found")
    if version.skill_type != SkillType.INSTRUCTION:
        raise HTTPException(status_code=422, detail="Only instruction Skills support conversations")
    title = payload.title or _next_conversation_title(db, user, version.skill_id)
    conversation = Conversation(
        user_id=user.id,
        skill_id=version.skill_id,
        skill_version_id=version.id,
        title=title,
    )
    db.add(conversation)
    db.flush()
    add_audit(
        db,
        actor=user,
        action="conversation.create",
        resource_type="conversation",
        resource_id=conversation.id,
        details={"skill_id": version.skill_id, "version_id": version.id},
    )
    db.commit()
    db.refresh(conversation)
    return conversation_read(db, conversation)


@router.get("/conversations", response_model=list[ConversationRead])
def list_conversations(
    skill_id: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[ConversationRead]:
    statement = select(Conversation).where(Conversation.user_id == user.id)
    if skill_id:
        statement = statement.where(Conversation.skill_id == skill_id)
    conversations = db.scalars(statement.order_by(Conversation.updated_at.desc())).all()
    return [conversation_read(db, item) for item in conversations]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ConversationDetail:
    return conversation_detail(db, _owned_conversation(db, conversation_id, user))


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
def update_conversation(
    conversation_id: str,
    payload: ConversationUpdate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ConversationRead:
    conversation = _owned_conversation(db, conversation_id, user)
    previous_title = conversation.title
    conversation.title = payload.title
    conversation.updated_at = utcnow()
    add_audit(
        db,
        actor=user,
        action="conversation.rename",
        resource_type="conversation",
        resource_id=conversation.id,
        details={"previous_title": previous_title, "title": conversation.title},
    )
    db.commit()
    db.refresh(conversation)
    return conversation_read(db, conversation)


@router.delete("/conversations/{conversation_id}/messages", response_model=Message)
def clear_conversation(
    conversation_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Message:
    conversation = _owned_conversation(db, conversation_id, user)
    if conversation.is_running:
        raise HTTPException(status_code=409, detail="Conversation is currently running")
    db.execute(
        delete(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id
        )
    )
    conversation.updated_at = utcnow()
    add_audit(
        db,
        actor=user,
        action="conversation.clear",
        resource_type="conversation",
        resource_id=conversation.id,
    )
    db.commit()
    return Message(message="Conversation context cleared")


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    conversation = _owned_conversation(db, conversation_id, user)
    if conversation.is_running:
        raise HTTPException(status_code=409, detail="Conversation is currently running")
    workspace_paths = list(
        db.scalars(
            select(WorkspaceFile.storage_path).where(
                WorkspaceFile.conversation_id == conversation.id
            )
        )
    )
    db.execute(
        delete(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id
        )
    )
    db.execute(
        delete(WorkspaceFile).where(WorkspaceFile.conversation_id == conversation.id)
    )
    db.execute(delete(Conversation).where(Conversation.id == conversation.id))
    add_audit(
        db,
        actor=user,
        action="conversation.delete",
        resource_type="conversation",
        resource_id=conversation.id,
        details={"skill_id": conversation.skill_id},
    )
    db.commit()
    for storage_path in workspace_paths:
        try:
            storage.delete(storage_path)
        except OSError:
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
