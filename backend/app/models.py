from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Role(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    USER = "user"


class Visibility(str, enum.Enum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    INTERNAL = "internal"
    PUBLIC = "public"


class VersionStatus(str, enum.Enum):
    DRAFT = "draft"
    READY = "ready"
    SUBMITTED = "submitted"
    REVIEWING = "reviewing"
    REJECTED = "rejected"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    YANKED = "yanked"


class SkillType(str, enum.Enum):
    INSTRUCTION = "instruction"
    CODE = "code"


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvocationType(str, enum.Enum):
    CONSOLE = "console"
    API = "api"


class JobStatus(str, enum.Enum):
    CREATED = "created"
    PREPARING = "preparing"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PRODUCING_ARTIFACTS = "producing_artifacts"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(Role, native_enum=False), default=Role.USER, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    skills: Mapped[list["Skill"]] = relationship(back_populates="owner")


class Skill(TimestampMixin, Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(String(280))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="other", index=True)
    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, native_enum=False), default=Visibility.PRIVATE, index=True
    )
    icon: Mapped[str] = mapped_column(String(16), default="sparkles")

    owner: Mapped[User] = relationship(back_populates="skills")
    versions: Mapped[list["SkillVersion"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan", order_by="SkillVersion.created_at"
    )
    favorites: Mapped[list["Favorite"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )


class SkillVersion(TimestampMixin, Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    reviewed_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    version: Mapped[str] = mapped_column(String(40))
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, native_enum=False), default=VersionStatus.DRAFT, index=True
    )
    skill_type: Mapped[SkillType] = mapped_column(
        Enum(SkillType, native_enum=False), default=SkillType.INSTRUCTION
    )
    package_sha256: Mapped[str] = mapped_column(String(64), index=True)
    package_path: Mapped[str] = mapped_column(String(500))
    manifest: Mapped[dict] = mapped_column(JSON)
    skill_md: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    skill: Mapped[Skill] = relationship(back_populates="versions")

    @property
    def runtime_profile(self) -> dict:
        from .runtime_profile import version_runtime_profile

        return version_runtime_profile(self)

    @property
    def execution_mode(self) -> str:
        return str(self.runtime_profile["execution_mode"])

    @property
    def runtime_status(self) -> str:
        return str(self.runtime_profile["runtime_status"])

    @property
    def runtime_runnable(self) -> bool:
        return bool(self.runtime_profile["runnable"])

    @property
    def runtime_block_reason(self) -> str | None:
        value = self.runtime_profile.get("block_reason")
        return str(value) if value else None

    @property
    def runtime_requirements(self) -> dict:
        return dict(self.runtime_profile.get("requirements") or {})

    @property
    def runtime_reasons(self) -> list[str]:
        return [str(item) for item in self.runtime_profile.get("reasons") or []]


class Favorite(TimestampMixin, Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "skill_id", name="uq_favorite"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    skill: Mapped[Skill] = relationship(back_populates="favorites")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ModelProviderConfig(TimestampMixin, Base):
    __tablename__ = "model_provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    base_url: Mapped[str] = mapped_column(String(500))
    api_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    models: Mapped[list] = mapped_column(JSON, default=list)
    default_model: Mapped[str] = mapped_column(String(160))
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    temperature_milli: Mapped[int] = mapped_column(Integer, default=200)
    json_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    native_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)


class ModelConnectionConfig(TimestampMixin, Base):
    """One independently configurable model exposed by the platform."""

    __tablename__ = "model_connection_configs"
    __table_args__ = (UniqueConstraint("model_name", name="uq_model_connection_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_name: Mapped[str] = mapped_column(String(160), index=True)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    temperature_milli: Mapped[int] = mapped_column(Integer, default=200)
    json_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    native_tools: Mapped[bool] = mapped_column(Boolean, default=True)
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class AgentConversation(TimestampMixin, Base):
    """A user's general SkillGo workspace conversation.

    These conversations are intentionally independent from ``Conversation``,
    which is the legacy one-Skill chat runtime.  A workspace conversation can
    contain ordinary model replies and explicit single- or multi-Skill jobs.
    """

    __tablename__ = "agent_conversations"
    __table_args__ = (
        Index("idx_agent_conversations_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))

    messages: Mapped[list["AgentMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentMessage.created_at",
    )
    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentRun.created_at",
    )

    @property
    def message_count(self) -> int:
        return len(self.messages)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("idx_agent_messages_conversation_created", "conversation_id", "created_at"),
        UniqueConstraint("job_id", name="uq_agent_message_job"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(24), default="text", index=True)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    model_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[AgentConversation] = relationship(back_populates="messages")
    job: Mapped["WorkflowJob | None"] = relationship()
    files: Mapped[list["AgentMessageFile"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="AgentMessageFile.created_at",
    )


class AgentMessageFile(Base):
    __tablename__ = "agent_message_files"
    __table_args__ = (
        Index("idx_agent_message_files_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(600))
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    message: Mapped[AgentMessage] = relationship(back_populates="files")


class Endpoint(TimestampMixin, Base):
    __tablename__ = "endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    skill_version_id: Mapped[str] = mapped_column(ForeignKey("skill_versions.id"), index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16))
    api_key_hash: Mapped[str] = mapped_column(String(64))

    skill: Mapped[Skill] = relationship()
    skill_version: Mapped[SkillVersion] = relationship()


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("idx_conversations_user_skill_updated", "user_id", "skill_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    skill_version_id: Mapped[str] = mapped_column(ForeignKey("skill_versions.id"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    is_running: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    run_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    skill: Mapped[Skill] = relationship()
    skill_version: Mapped[SkillVersion] = relationship()
    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.created_at",
    )
    files: Mapped[list["WorkspaceFile"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="WorkspaceFile.created_at",
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("idx_runs_user_created", "user_id", "created_at"),
        Index("idx_runs_endpoint_created", "endpoint_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("endpoints.id"), nullable=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    skill_version_id: Mapped[str] = mapped_column(ForeignKey("skill_versions.id"), index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False), default=RunStatus.QUEUED, index=True
    )
    invocation_type: Mapped[InvocationType] = mapped_column(
        Enum(InvocationType, native_enum=False), default=InvocationType.CONSOLE
    )
    input_data: Mapped[dict] = mapped_column(JSON)
    output_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    token_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    skill: Mapped[Skill] = relationship()
    skill_version: Mapped[SkillVersion] = relationship()
    endpoint: Mapped[Endpoint | None] = relationship()


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("idx_conversation_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class WorkspaceFile(Base):
    __tablename__ = "workspace_files"
    __table_args__ = (
        Index("idx_workspace_files_user_conversation_created", "user_id", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(600))
    source: Mapped[str] = mapped_column(String(20), default="upload", index=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="files")


class WorkflowJob(TimestampMixin, Base):
    __tablename__ = "workflow_jobs"
    __table_args__ = (
        Index("idx_workflow_jobs_user_created", "user_id", "created_at"),
        Index("idx_workflow_jobs_skill_created", "skill_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    skill_version_id: Mapped[str] = mapped_column(ForeignKey("skill_versions.id"), index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.CREATED, index=True
    )
    execution_mode: Mapped[str] = mapped_column(String(40))
    trigger: Mapped[str] = mapped_column(String(30), default="file_upload")
    instruction: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    skill: Mapped[Skill] = relationship()
    skill_version: Mapped[SkillVersion] = relationship()
    steps: Mapped[list["JobStep"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobStep.position"
    )
    input_files: Mapped[list["JobInputFile"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobInputFile.created_at"
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Artifact.created_at"
    )
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.sequence"
    )
    skill_bindings: Mapped[list["WorkflowJobSkill"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="WorkflowJobSkill.position"
    )
    model_selection: Mapped["WorkflowJobModel | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    prompt_message: Mapped["WorkflowJobPrompt | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    agent_run: Mapped["AgentRun | None"] = relationship(
        back_populates="workflow_job", cascade="all, delete-orphan", uselist=False
    )

    @property
    def skill_name(self) -> str:
        return self.skill.name

    @property
    def version(self) -> str:
        return self.skill_version.version

    @property
    def model_name(self) -> str | None:
        return self.model_selection.model_name if self.model_selection else None

    @property
    def message_content(self) -> list[dict]:
        if self.prompt_message:
            return list(self.prompt_message.content or [])
        return [{"type": "text", "text": self.instruction}] if self.instruction else []

    @property
    def routing_mode(self) -> str:
        return self.prompt_message.routing_mode if self.prompt_message else "legacy"

    @property
    def selected_skills(self) -> list[dict]:
        if self.skill_bindings:
            return [
                {
                    "skill_id": item.skill_id,
                    "skill_version_id": item.skill_version_id,
                    "skill_name": item.skill.name,
                    "version": item.skill_version.version,
                    "position": item.position,
                }
                for item in self.skill_bindings
            ]
        # Jobs created before multi-Skill support keep working without a data migration.
        return [
            {
                "skill_id": self.skill_id,
                "skill_version_id": self.skill_version_id,
                "skill_name": self.skill.name,
                "version": self.skill_version.version,
                "position": 1,
            }
        ]


class WorkflowJobSkill(Base):
    """Ordered Skill versions mounted into one workflow job.

    Keeping this as a separate table lets existing deployments gain multi-Skill
    execution through create_all without altering populated workflow_jobs rows.
    """

    __tablename__ = "workflow_job_skills"
    __table_args__ = (
        UniqueConstraint("job_id", "position", name="uq_workflow_job_skill_position"),
        UniqueConstraint("job_id", "skill_id", name="uq_workflow_job_skill_identity"),
        Index("idx_workflow_job_skills_version", "skill_version_id"),
    )

    job_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    skill_version_id: Mapped[str] = mapped_column(
        ForeignKey("skill_versions.id"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)

    job: Mapped[WorkflowJob] = relationship(back_populates="skill_bindings")
    skill: Mapped[Skill] = relationship()
    skill_version: Mapped[SkillVersion] = relationship()


class WorkflowJobModel(Base):
    __tablename__ = "workflow_job_models"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    model_name: Mapped[str] = mapped_column(String(160), index=True)
    job: Mapped[WorkflowJob] = relationship(back_populates="model_selection")


class WorkflowJobPrompt(Base):
    """Durable structured user message used to route and order Skills.

    A separate table keeps existing deployments migration-safe: ``create_all``
    can add it without altering populated ``workflow_jobs`` rows.
    """

    __tablename__ = "workflow_job_prompts"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[list] = mapped_column(JSON, default=list)
    routing_mode: Mapped[str] = mapped_column(String(24), default="explicit", index=True)
    job: Mapped[WorkflowJob] = relationship(back_populates="prompt_message")


class WorkflowEndpointRequest(Base):
    """Bind an externally-created workflow job to exactly one Endpoint.

    The binding is also the durable idempotency record.  Keeping it in a
    separate table lets existing installations gain the feature through
    ``create_all`` without altering the already-populated workflow_jobs table.
    """

    __tablename__ = "workflow_endpoint_requests"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_workflow_endpoint_request_job"),
        UniqueConstraint(
            "endpoint_id",
            "idempotency_key_hash",
            name="uq_workflow_endpoint_idempotency",
        ),
        Index("idx_workflow_endpoint_requests_endpoint_created", "endpoint_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id"), index=True)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class JobStep(Base):
    __tablename__ = "job_steps"
    __table_args__ = (UniqueConstraint("job_id", "step_key", name="uq_job_step_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[JobStepStatus] = mapped_column(
        Enum(JobStepStatus, native_enum=False), default=JobStepStatus.PENDING, index=True
    )
    detail: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[WorkflowJob] = relationship(back_populates="steps")


class JobEvent(Base):
    """Append-only, user-visible execution events for the Agent timeline.

    Events deliberately contain operational summaries only. Hidden model
    reasoning, uploaded file contents, and command output are not persisted.
    """

    __tablename__ = "job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),
        Index("idx_job_events_job_sequence", "job_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    title: Mapped[str] = mapped_column(String(180))
    detail: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    job: Mapped[WorkflowJob] = relationship(back_populates="events")


class AgentRun(TimestampMixin, Base):
    """Durable lifecycle record shared by Skill jobs and ordinary chat turns.

    Detailed events are deliberately kept in a separate table so retention can
    prune them without deleting the small final run summary or user content.
    A workflow job owns one logical run with one or more isolated attempts;
    every ordinary conversation turn owns a separate run.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("workflow_job_id", name="uq_agent_run_workflow_job"),
        Index("idx_agent_runs_user_created", "user_id", "created_at"),
        Index("idx_agent_runs_conversation_created", "conversation_id", "created_at"),
        Index("idx_agent_runs_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_type: Mapped[str] = mapped_column(String(32), index=True)
    workflow_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    request_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, native_enum=False), default=RunStatus.QUEUED, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(180), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)

    workflow_job: Mapped["WorkflowJob | None"] = relationship(back_populates="agent_run")
    conversation: Mapped["AgentConversation | None"] = relationship(back_populates="runs")
    events: Mapped[list["AgentRunEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentRunEvent.sequence"
    )


class AgentRunEvent(Base):
    """Short-lived structured execution detail; never stores token streams or file bodies."""

    __tablename__ = "agent_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_event_sequence"),
        Index("idx_agent_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    run: Mapped[AgentRun] = relationship(back_populates="events")


class JobInputFile(Base):
    __tablename__ = "job_input_files"
    __table_args__ = (Index("idx_job_input_files_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(600))
    readable: Mapped[bool] = mapped_column(Boolean, default=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    job: Mapped[WorkflowJob] = relationship(back_populates="input_files")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("idx_artifacts_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(180))
    content_type: Mapped[str] = mapped_column(String(160))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(600))
    kind: Mapped[str] = mapped_column(String(40), default="result")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    job: Mapped[WorkflowJob] = relationship(back_populates="artifacts")
