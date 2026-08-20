from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .config import settings
from .models import Conversation, ConversationMessage, Role, SkillVersion, User, VersionStatus, utcnow
from .schemas import ConversationDetail, ConversationMessageRead, ConversationRead


def can_run_version(version: SkillVersion, user: User) -> bool:
    return (
        version.skill.owner_id == user.id
        or user.role in (Role.ADMIN, Role.SUPER_ADMIN)
        or version.status == VersionStatus.PUBLISHED
    )


def conversation_read(db: Session, conversation: Conversation) -> ConversationRead:
    message_count = db.scalar(
        select(func.count()).select_from(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.id
        )
    ) or 0
    return ConversationRead(
        id=conversation.id,
        skill_id=conversation.skill_id,
        skill_version_id=conversation.skill_version_id,
        skill_name=conversation.skill.name,
        version=conversation.skill_version.version,
        title=conversation.title,
        message_count=message_count,
        is_running=conversation.is_running,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def conversation_detail(db: Session, conversation: Conversation) -> ConversationDetail:
    base = conversation_read(db, conversation)
    messages = list(
        db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.created_at.asc(), ConversationMessage.id.asc())
        ).all()
    )
    return ConversationDetail(
        **base.model_dump(),
        messages=[ConversationMessageRead.model_validate(item) for item in messages],
    )


def conversation_history(db: Session, conversation_id: str) -> list[dict]:
    max_messages = max(0, min(settings.context_max_messages, 100))
    max_chars = max(0, settings.context_max_chars)
    if not max_messages or not max_chars:
        return []

    newest = list(
        db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .limit(max_messages)
        ).all()
    )
    selected: list[dict] = []
    used_chars = 0
    for message in newest:
        rendered = json.dumps(message.content, ensure_ascii=False, separators=(",", ":"))
        size = len(rendered) + len(message.role)
        if used_chars + size > max_chars:
            if not selected:
                excerpt_size = max(0, max_chars - 80)
                selected.append(
                    {
                        "role": message.role,
                        "content": {"_truncated": True, "excerpt": rendered[:excerpt_size]},
                    }
                )
            break
        selected.append({"role": message.role, "content": message.content})
        used_chars += size
    selected.reverse()
    return selected


def claim_conversation(db: Session, conversation: Conversation, user: User) -> bool:
    now = utcnow()
    stale_before = now - timedelta(seconds=max(30, settings.conversation_lock_seconds))
    result = db.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation.id,
            Conversation.user_id == user.id,
            or_(
                Conversation.is_running.is_(False),
                Conversation.run_started_at.is_(None),
                Conversation.run_started_at < stale_before,
            ),
        )
        .values(is_running=True, run_started_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def release_conversation(db: Session, conversation_id: str) -> None:
    db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(is_running=False, run_started_at=None, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


def record_exchange(
    db: Session,
    *,
    conversation_id: str,
    run_id: str,
    input_data: dict,
    output_data: dict,
) -> None:
    created_at = utcnow()
    db.add_all(
        [
            ConversationMessage(
                conversation_id=conversation_id,
                run_id=run_id,
                role="user",
                content=input_data,
                created_at=created_at,
            ),
            ConversationMessage(
                conversation_id=conversation_id,
                run_id=run_id,
                role="assistant",
                content=output_data,
                created_at=created_at + timedelta(microseconds=1),
            ),
        ]
    )
