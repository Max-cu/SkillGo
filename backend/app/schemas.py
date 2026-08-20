from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .models import InvocationType, JobStatus, JobStepStatus, Role, RunStatus, SkillType, VersionStatus, Visibility


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=128)
    identity: Literal["member", "admin"] = "member"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class RegistrationResponse(BaseModel):
    status: Literal["active", "pending_approval"]
    message: str
    access_token: str | None = None
    token_type: str = "bearer"
    user: UserRead


class SkillCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    name: str = Field(min_length=2, max_length=120)
    summary: str = Field(min_length=10, max_length=280)
    description: str = Field(default="", max_length=20_000)
    category: str = Field(default="other", max_length=80)
    visibility: Visibility = Visibility.PRIVATE
    icon: str = Field(default="sparkles", max_length=16)


class SkillVisibilityUpdate(BaseModel):
    visibility: Visibility


class SkillPackageAnalysis(BaseModel):
    name: str
    slug: str
    summary: str
    description: str
    category: str
    version: str
    skill_type: SkillType
    package_format: str
    source: str
    model_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    slug: str
    name: str
    summary: str
    description: str
    category: str
    visibility: Visibility
    icon: str
    created_at: datetime
    updated_at: datetime
    owner_name: str | None = None
    favorite_count: int = 0
    latest_version: str | None = None
    latest_status: VersionStatus | None = None


class VersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    skill_id: str
    version: str
    status: VersionStatus
    skill_type: SkillType
    package_sha256: str
    manifest: dict
    input_schema: dict
    output_schema: dict
    requested_permissions: dict
    execution_mode: str
    runtime_status: str
    runtime_runnable: bool
    runtime_block_reason: str | None
    runtime_requirements: dict
    runtime_reasons: list[str]
    review_note: str | None
    created_at: datetime
    published_at: datetime | None


class SkillDetail(SkillRead):
    versions: list[VersionRead]


class JobStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    step_key: str
    name: str
    position: int
    status: JobStepStatus
    detail: str
    started_at: datetime | None
    finished_at: datetime | None


class JobInputFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    readable: bool
    created_at: datetime


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    kind: str
    verified: bool
    created_at: datetime


class JobEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    event_type: str
    status: str
    title: str
    detail: str
    data: dict = Field(default_factory=dict)
    created_at: datetime


class WorkflowJobSkillRead(BaseModel):
    skill_id: str
    skill_version_id: str
    skill_name: str
    version: str
    position: int


class WorkflowJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    skill_id: str
    skill_version_id: str
    skill_name: str
    version: str
    status: JobStatus
    execution_mode: str
    trigger: str
    instruction: str
    message_content: list[dict] = Field(default_factory=list)
    routing_mode: str = "legacy"
    model_name: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[JobStepRead] = Field(default_factory=list)
    input_files: list[JobInputFileRead] = Field(default_factory=list)
    artifacts: list[ArtifactRead] = Field(default_factory=list)
    events: list[JobEventRead] = Field(default_factory=list)
    selected_skills: list[WorkflowJobSkillRead] = Field(default_factory=list)


class AgentConversationCreate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Conversation title cannot be empty")
        return value


class AgentConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Conversation title cannot be empty")
        return value


class AgentMessageFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class AgentMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    kind: str
    content: dict
    model_name: str | None
    token_usage: dict = Field(default_factory=dict)
    created_at: datetime
    files: list[AgentMessageFileRead] = Field(default_factory=list)
    job: WorkflowJobRead | None = None


class AgentConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class AgentConversationDetail(AgentConversationRead):
    messages: list[AgentMessageRead] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    note: str = Field(default="", max_length=4000)


class UserAdminPatch(BaseModel):
    is_active: bool


class UserRolePatch(BaseModel):
    role: Role

    @field_validator("role")
    @classmethod
    def role_allowed(cls, value: Role) -> Role:
        if value == Role.SUPER_ADMIN:
            raise ValueError("The super administrator role cannot be assigned")
        return value


class UserDeleteRequest(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("user_ids")
    @classmethod
    def unique_user_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class Message(BaseModel):
    message: str


class SystemSummary(BaseModel):
    users: int
    admins: int
    skills: int
    published_versions: int
    pending_reviews: int
    runs: int = 0
    endpoints: int = 0


class ConversationCreate(BaseModel):
    version_id: str
    title: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Conversation title cannot be empty")
        return value


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Conversation title cannot be empty")
        return value


class ConversationMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str | None
    role: str
    content: dict
    created_at: datetime


class ConversationRead(BaseModel):
    id: str
    skill_id: str
    skill_version_id: str
    skill_name: str
    version: str
    title: str
    message_count: int
    is_running: bool
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[ConversationMessageRead]


class WorkspaceFileRead(BaseModel):
    id: str
    conversation_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    source: str
    readable: bool
    created_at: datetime


class WorkspaceArtifactCreate(BaseModel):
    filename: str = Field(default="skill-output.txt", min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=500000)


class RunCreate(BaseModel):
    version_id: str
    input: dict | None = None
    message: str | None = Field(default=None, min_length=1, max_length=20000)
    conversation_id: str | None = None
    model_name: str | None = Field(default=None, max_length=160)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty")
        return value

    @model_validator(mode="after")
    def require_one_input_mode(self) -> "RunCreate":
        if (self.input is None) == (self.message is None):
            raise ValueError("Provide exactly one of input or message")
        return self


class RunRead(BaseModel):
    id: str
    skill_id: str
    skill_version_id: str
    skill_name: str
    version: str
    endpoint_id: str | None
    endpoint_slug: str | None
    status: RunStatus
    invocation_type: InvocationType
    input: dict
    output: dict | None
    error_code: str | None
    error_message: str | None
    model_name: str | None
    token_usage: dict
    latency_ms: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    context_message_count: int = 0


class EndpointCreate(BaseModel):
    version_id: str
    slug: str = Field(min_length=3, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    name: str = Field(min_length=2, max_length=160)


class EndpointPatch(BaseModel):
    is_active: bool


class EndpointRead(BaseModel):
    id: str
    owner_id: str
    skill_id: str
    skill_version_id: str
    skill_name: str
    version: str
    slug: str
    name: str
    is_active: bool
    execution_mode: str
    invocation_mode: str
    api_key_prefix: str
    created_at: datetime
    updated_at: datetime


class EndpointCreated(EndpointRead):
    api_key: str


class InvokeRequest(BaseModel):
    input: dict


class InvokeResponse(BaseModel):
    run_id: str
    status: RunStatus
    output: dict
    model_name: str | None
    latency_ms: int | None


class ModelStatus(BaseModel):
    configured: bool
    base_url: str | None
    model_name: str | None
    json_mode: bool
    tls_verify: bool


class AvailableModels(BaseModel):
    configured: bool
    models: list[str] = Field(default_factory=list)
    default_model: str | None


class ModelConfigRead(AvailableModels):
    base_url: str | None
    api_key_configured: bool
    timeout_seconds: int
    temperature: float
    json_mode: bool
    native_tools: bool
    tls_verify: bool
    source: str


class ModelConfigUpdate(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    clear_api_key: bool = False
    models: list[str] = Field(min_length=1, max_length=20)
    default_model: str = Field(min_length=1, max_length=160)
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    temperature: float = Field(default=0.2, ge=0, le=2)
    json_mode: bool = True
    native_tools: bool = True
    tls_verify: bool = True


class ModelConnectionTestRequest(ModelConfigUpdate):
    model_id: str | None = Field(default=None, max_length=36)


class ModelConnectionTestResult(BaseModel):
    ok: bool
    model_name: str
    latency_ms: int
    message: str


class ModelConnectionItem(BaseModel):
    id: str
    model_name: str
    base_url: str
    api_key_configured: bool
    timeout_seconds: int
    temperature: float
    json_mode: bool
    native_tools: bool
    tls_verify: bool
    is_default: bool
    enabled: bool
    source: str = "database"


class ModelConnectionList(BaseModel):
    configured: bool
    default_model: str | None
    items: list[ModelConnectionItem] = Field(default_factory=list)


class ModelConnectionCreate(BaseModel):
    model_name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    temperature: float = Field(default=0.2, ge=0, le=2)
    json_mode: bool = True
    native_tools: bool = True
    tls_verify: bool = True
    is_default: bool = False
    enabled: bool = True


class ModelConnectionUpdate(ModelConnectionCreate):
    clear_api_key: bool = False
