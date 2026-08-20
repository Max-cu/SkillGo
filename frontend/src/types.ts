export type Role = "super_admin" | "admin" | "user";
export type Visibility = "private" | "unlisted" | "internal" | "public";
export type VersionStatus =
  | "draft"
  | "ready"
  | "submitted"
  | "reviewing"
  | "rejected"
  | "published"
  | "deprecated"
  | "yanked";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type InvocationType = "console" | "api";

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface SkillVersion {
  id: string;
  skill_id: string;
  version: string;
  status: VersionStatus;
  skill_type: "instruction" | "code";
  package_sha256: string;
  manifest: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  requested_permissions: Record<string, unknown>;
  execution_mode: "instruction_only" | "platform_tools" | "sandbox_required" | string;
  runtime_status: "available" | "awaiting_platform_tools" | "awaiting_sandbox" | string;
  runtime_runnable: boolean;
  runtime_block_reason: string | null;
  runtime_requirements: {
    runtimes?: string[];
    scripts?: string[];
    tools?: string[];
    network?: boolean;
    network_rules?: string[];
    expected_artifacts?: string[];
    [key: string]: unknown;
  };
  runtime_reasons: string[];
  review_note: string | null;
  created_at: string;
  published_at: string | null;
}

export interface Skill {
  id: string;
  owner_id: string;
  owner_name?: string;
  slug: string;
  name: string;
  summary: string;
  description: string;
  category: string;
  visibility: Visibility;
  icon: string;
  favorite_count: number;
  latest_version: string | null;
  latest_status: VersionStatus | null;
  created_at: string;
  updated_at: string;
  versions?: SkillVersion[];
}

export interface SkillPackageAnalysis {
  name: string;
  slug: string;
  summary: string;
  description: string;
  category: string;
  version: string;
  skill_type: "instruction" | "code";
  package_format: "agent-skill" | "skillgo" | string;
  source: "ai" | "package";
  model_name: string | null;
  warnings: string[];
}

export type WorkflowJobStatus =
  | "created"
  | "preparing"
  | "queued"
  | "running"
  | "waiting_user"
  | "producing_artifacts"
  | "verifying"
  | "blocked"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface WorkflowJobStep {
  id: string;
  step_key: string;
  name: string;
  position: number;
  status: "pending" | "running" | "blocked" | "succeeded" | "failed" | "skipped";
  detail: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkflowJobInputFile {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  readable: boolean;
  created_at: string;
}

export interface WorkflowArtifact {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  kind: string;
  verified: boolean;
  created_at: string;
}

export interface WorkflowJobEvent {
  id: string;
  sequence: number;
  event_type: "input" | "status" | "reasoning" | "tool" | "artifact" | "result" | "error" | string;
  status: "queued" | "running" | "succeeded" | "failed" | "blocked" | "cancelled" | string;
  title: string;
  detail: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface WorkflowJobSkill {
  skill_id: string;
  skill_version_id: string;
  skill_name: string;
  version: string;
  position: number;
}

export type WorkflowMessagePart =
  | { type: "text"; text: string }
  | {
      type: "skill_ref";
      skill_id: string;
      skill_version_id?: string;
      skill_name: string;
      version?: string;
    };

export interface WorkflowJob {
  id: string;
  user_id: string;
  skill_id: string;
  skill_version_id: string;
  skill_name: string;
  version: string;
  status: WorkflowJobStatus;
  execution_mode: string;
  trigger: string;
  instruction: string;
  message_content: WorkflowMessagePart[];
  routing_mode: "explicit" | "automatic" | "legacy" | string;
  model_name: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  steps: WorkflowJobStep[];
  input_files: WorkflowJobInputFile[];
  artifacts: WorkflowArtifact[];
  events: WorkflowJobEvent[];
  selected_skills: WorkflowJobSkill[];
}

export interface AgentMessageFile {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

export interface AgentWorkspaceMessage {
  id: string;
  role: "user" | "assistant" | string;
  kind: "text" | "workflow" | string;
  content: {
    message?: string;
    parts?: WorkflowMessagePart[];
    files?: Array<{ filename: string; size_bytes: number; content_type: string }>;
    job_id?: string;
    [key: string]: unknown;
  };
  model_name: string | null;
  token_usage: Record<string, number>;
  created_at: string;
  files: AgentMessageFile[];
  job: WorkflowJob | null;
}

export interface AgentWorkspaceConversation {
  id: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface AgentWorkspaceConversationDetail extends AgentWorkspaceConversation {
  messages: AgentWorkspaceMessage[];
}

export interface SystemSummary {
  users: number;
  admins: number;
  skills: number;
  published_versions: number;
  pending_reviews: number;
  runs: number;
  endpoints: number;
}

export interface SkillRun {
  id: string;
  skill_id: string;
  skill_version_id: string;
  skill_name: string;
  version: string;
  endpoint_id: string | null;
  endpoint_slug: string | null;
  status: RunStatus;
  invocation_type: InvocationType;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  model_name: string | null;
  token_usage: Record<string, number>;
  latency_ms: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  context_message_count: number;
}

export interface Conversation {
  id: string;
  skill_id: string;
  skill_version_id: string;
  skill_name: string;
  version: string;
  title: string;
  message_count: number;
  is_running: boolean;
  created_at: string;
  updated_at: string;
}

export interface ConversationMessage {
  id: string;
  run_id: string | null;
  role: "user" | "assistant" | string;
  content: Record<string, unknown>;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[];
}

export interface WorkspaceFile {
  id: string;
  conversation_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  source: "upload" | "generated" | string;
  readable: boolean;
  created_at: string;
}

export interface Endpoint {
  id: string;
  owner_id: string;
  skill_id: string;
  skill_version_id: string;
  skill_name: string;
  version: string;
  slug: string;
  name: string;
  is_active: boolean;
  execution_mode: string;
  invocation_mode: "sync" | "async" | string;
  api_key_prefix: string;
  created_at: string;
  updated_at: string;
}

export interface EndpointCreated extends Endpoint {
  api_key: string;
}

export interface ModelStatus {
  configured: boolean;
  base_url: string | null;
  model_name: string | null;
  json_mode: boolean;
  tls_verify: boolean;
}

export interface AvailableModels {
  configured: boolean;
  models: string[];
  default_model: string | null;
}

export interface ModelConfig {
  configured: boolean;
  base_url: string | null;
  models: string[];
  default_model: string | null;
  api_key_configured: boolean;
  timeout_seconds: number;
  temperature: number;
  json_mode: boolean;
  native_tools: boolean;
  tls_verify: boolean;
  source: "environment" | "database" | string;
}

export interface ModelConnectionTestResult {
  ok: boolean;
  model_name: string;
  latency_ms: number;
  message: string;
}

export interface ModelConnectionItem {
  id: string;
  model_name: string;
  base_url: string;
  api_key_configured: boolean;
  timeout_seconds: number;
  temperature: number;
  json_mode: boolean;
  native_tools: boolean;
  tls_verify: boolean;
  is_default: boolean;
  enabled: boolean;
  source: string;
}

export interface ModelConnectionList {
  configured: boolean;
  default_model: string | null;
  items: ModelConnectionItem[];
}
