import { AlertTriangle, ArrowDown, ArrowRight, Check, ChevronDown, ChevronRight, Download, FileCheck2, FileText, FolderClock, MessageSquareText, Paperclip, PencilLine, Plus, RotateCw, Trash2, Workflow, X, Zap } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, apiBlob, apiNdjson } from "./api";
import { useAuth } from "./auth";
import { SkillPromptEditor, type SkillPromptEditorHandle } from "./SkillPromptEditor";
import { Link } from "./router";
import type { AgentMessageFile, AgentWorkspaceConversation, AgentWorkspaceConversationDetail, AvailableModels, Skill, SkillVersion, WorkflowArtifact, WorkflowJob, WorkflowJobEvent, WorkflowJobStatus, WorkflowMessagePart } from "./types";

gsap.registerPlugin(useGSAP);

function useLoad<T>(path: string, initial: T) {
  const [data, setData] = useState<T>(initial);
  useEffect(() => {
    api<T>(path).then(setData).catch(() => undefined);
  }, [path]);
  return data;
}

const activeStatuses = new Set<WorkflowJobStatus>(["created", "preparing", "queued", "running", "producing_artifacts", "verifying"]);

const workflowStatusLabels: Record<WorkflowJobStatus, string> = {
  created: "已创建",
  preparing: "准备中",
  queued: "排队中",
  running: "执行中",
  waiting_user: "等待补充信息",
  producing_artifacts: "生成产物",
  verifying: "校验产物",
  blocked: "等待运行环境",
  succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

function greeting(hour = new Date().getHours()) {
  if (hour < 5) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 13) return "中午好";
  if (hour < 18) return "下午好";
  if (hour < 23) return "晚上好";
  return "夜深了";
}

function formatSize(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function formatDuration(milliseconds: number) {
  const safe = Math.max(0, Math.round(milliseconds));
  if (safe < 1000) return `${safe} 毫秒`;
  const seconds = Math.round(safe / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
}

function elapsedBetween(start: string | null, finish: string | null, now: number) {
  if (!start) return 0;
  const startTime = new Date(start).getTime();
  const finishTime = finish ? new Date(finish).getTime() : now;
  return Math.max(0, finishTime - startTime);
}

function eventDuration(event: WorkflowJobEvent) {
  return typeof event.data?.duration_ms === "number" ? formatDuration(event.data.duration_ms) : "";
}

function useLiveNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) {
      setNow(Date.now());
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function hasPromptContent(parts: WorkflowMessagePart[]) {
  return parts.some((part) => part.type === "skill_ref" || part.text.trim());
}

function promptText(parts: WorkflowMessagePart[]) {
  return parts.filter((part): part is Extract<WorkflowMessagePart, { type: "text" }> => part.type === "text")
    .map((part) => part.text)
    .join("")
    .trim();
}

function runnableVersion(skill: Skill): SkillVersion | undefined {
  const versions = [...(skill.versions || [])].reverse();
  return versions.find((item) => item.runtime_runnable)
    || versions.find((item) => item.execution_mode === "instruction_only");
}

function StructuredPrompt({ parts, fallback }: { parts?: WorkflowMessagePart[]; fallback: string }) {
  if (!parts?.length) return <>{fallback}</>;
  return <>{parts.map((part, index) => part.type === "text"
    ? <span key={`text-${index}`}>{part.text}</span>
    : <span className="agent-message-skill" key={`${part.skill_id}-${index}`}><Zap />{part.skill_name}</span>)}</>;
}

function MarkdownContent({ children, className }: { children: string; className: string }) {
  return <div className={`${className} agent-markdown`}><ReactMarkdown
    remarkPlugins={[remarkGfm]}
    components={{
      a: ({ children: linkChildren, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener">{linkChildren}</a>,
    }}
  >{children}</ReactMarkdown></div>;
}

function TraceEvent({ event, forceComplete = false }: { event: WorkflowJobEvent; forceComplete?: boolean }) {
  const duration = eventDuration(event);
  const skillName = typeof event.data?.skill_name === "string" ? event.data.skill_name : "";
  const diagnostic = typeof event.data?.diagnostic === "string" ? event.data.diagnostic : "";
  const path = typeof event.data?.path === "string" ? event.data.path : "";
  const running = !forceComplete && (event.status === "running" || event.status === "queued");
  const failed = event.status === "failed";
  const technicalPath = path && path !== event.detail ? path : "";
  const hasDetail = Boolean(diagnostic || technicalPath);
  return <details className={`agent-trace-event ${failed ? "failed" : running ? "running" : "succeeded"}`} open={running}>
    <summary>
      <span className="agent-trace-node">{running ? <i className="agent-inline-spinner" /> : failed ? <AlertTriangle /> : <Check />}</span>
      <span className="agent-trace-copy"><strong>{event.title}</strong><small>{event.detail}</small></span>
      {skillName && <span className="agent-trace-skill">{skillName}</span>}
      {duration && <time>{duration}</time>}
      {hasDetail && <ChevronDown />}
    </summary>
    {hasDetail && <div className="agent-trace-detail">
      {technicalPath && <code>{technicalPath}</code>}
      {diagnostic && <pre>{diagnostic}</pre>}
    </div>}
  </details>;
}

function WorkflowReply({ job, onDownload, onRetry, onEdit }: { job: WorkflowJob; onDownload: (job: WorkflowJob, artifact: WorkflowArtifact) => void; onRetry: (job: WorkflowJob) => void; onEdit: (job: WorkflowJob) => void }) {
  const running = activeStatuses.has(job.status);
  const now = useLiveNow(running);
  const [traceOpen, setTraceOpen] = useState(running);
  useEffect(() => setTraceOpen(running), [running]);
  const finalEvent = [...job.events].reverse().find((item) => item.event_type === "result");
  const toolEvents = job.events.filter((item) => item.event_type === "tool");
  const failedTools = toolEvents.filter((item) => item.status === "failed");
  const latestTurn = Math.max(0, ...job.events.map((item) => typeof item.data?.turn === "number" ? item.data.turn : 0));
  const totalDuration = elapsedBetween(job.started_at || job.created_at, job.finished_at, now);
  const visibleEvents = job.events.filter((item) => !["artifact", "result", "error"].includes(item.event_type));
  const firstTurnIndex = visibleEvents.findIndex((item) => Number(item.data?.turn) > 0);
  const lastTurnIndex = visibleEvents.reduce((last, item, index) => Number(item.data?.turn) > 0 ? index : last, -1);
  const preludeEvents = firstTurnIndex < 0 ? visibleEvents : visibleEvents.slice(0, firstTurnIndex);
  const closingEvents = lastTurnIndex < 0 ? [] : visibleEvents.slice(lastTurnIndex + 1);
  const turns = Array.from(new Set(
    visibleEvents.map((item) => Number(item.data?.turn) || 0).filter((turn) => turn > 0),
  )).map((turn) => ({
    turn,
    events: visibleEvents.filter((event) => Number(event.data?.turn) === turn && ["reasoning", "tool"].includes(event.event_type)),
  }));
  const latestEvent = [...job.events].reverse().find((item) => !["result", "artifact"].includes(item.event_type));
  const stageRows = job.steps.filter((step) => step.started_at).map((step) => ({
    ...step,
    duration: elapsedBetween(step.started_at, step.finished_at, now),
  }));

  return <div className={`agent-run ${running ? "is-running" : `is-${job.status}`}`}>
    <button type="button" className="agent-run-summary" aria-expanded={traceOpen} onClick={() => setTraceOpen((open) => !open)}>
      <span className="agent-run-state">{running ? <i className="agent-inline-spinner" /> : job.status === "succeeded" ? <Check /> : <AlertTriangle />}</span>
      <span>
        <strong>{running ? latestTurn ? `正在处理 · 第 ${latestTurn} 轮` : "正在启动独立沙箱" : job.status === "succeeded" ? "处理完成" : workflowStatusLabels[job.status]}</strong>
        <small>{running ? latestEvent?.title || "等待 Worker 接收任务" : `${formatDuration(totalDuration)} · ${latestTurn} 轮 · ${toolEvents.length} 次工具调用`}</small>
      </span>
      <time>{running ? formatDuration(totalDuration) : `任务 ${job.id.slice(0, 8)}`}</time>
      <ChevronDown />
    </button>

    {traceOpen && <div className="agent-run-trace">
      {preludeEvents.map((event) => <TraceEvent event={event} forceComplete={!running} key={event.id} />)}
      {turns.length > 0 && <details className="agent-trace-process" open={running}>
        <summary>
          <span className="agent-trace-process-node">{running ? <i className="agent-inline-spinner" /> : <Check />}</span>
          <span><strong>{running ? `正在执行第 ${latestTurn} 轮` : "执行过程"}</strong><small>{running ? `${toolEvents.length} 项操作已调用` : `${turns.length} 轮 · ${toolEvents.length} 项操作`}</small></span>
          <ChevronDown />
        </summary>
        <div>
          {running && latestTurn > 1 && <div className="agent-trace-prior"><Check /><span>前 {latestTurn - 1} 轮已完成</span><small>{toolEvents.filter((event) => Number(event.data?.turn) < latestTurn).length} 项操作</small></div>}
          {(running ? turns.filter((item) => item.turn === latestTurn) : turns).map((item) => <details className="agent-trace-turn" key={`turn-${item.turn}`} open={running && item.turn === latestTurn}>
            <summary><span>第 {item.turn} 轮</span><small>{item.events.filter((event) => event.event_type === "tool").length} 项操作</small><ChevronDown /></summary>
            <div>{item.events.map((event) => <TraceEvent event={event} forceComplete={!running} key={event.id} />)}</div>
          </details>)}
        </div>
      </details>}
      {closingEvents.map((event) => <TraceEvent event={event} forceComplete={!running} key={event.id} />)}
      {running && latestTurn === 0 && <div className="agent-trace-waiting"><i /><span>Worker 正在为本次任务准备一次性隔离环境</span></div>}
      {stageRows.length > 0 && <details className="agent-stage-details"><summary>阶段耗时<ChevronDown /></summary><div>{stageRows.map((step) => <p key={step.id}><span>{step.name}</span><small>{formatDuration(step.duration)}</small></p>)}</div></details>}
    </div>}

    {job.error_message && <div className="agent-run-error"><AlertTriangle /><span><strong>{job.error_code || "TASK_FAILED"}</strong><small>{job.error_message}</small></span></div>}
    {finalEvent && <MarkdownContent className="agent-run-answer">{finalEvent.detail}</MarkdownContent>}
    {["failed", "blocked", "cancelled"].includes(job.status) && <div className="agent-run-recovery"><button type="button" onClick={() => onRetry(job)}><RotateCw />原样重试</button><button type="button" onClick={() => onEdit(job)}><PencilLine />修改后再运行</button></div>}
    {job.artifacts.length > 0 && <div className="agent-run-artifacts"><span>生成的文件</span>{job.artifacts.map((artifact) => <button type="button" key={artifact.id} onClick={() => onDownload(job, artifact)}><FileCheck2 /><span><strong>{artifact.filename}</strong><small>{formatSize(artifact.size_bytes)} · {artifact.verified ? "已校验" : "校验中"}</small></span><Download /></button>)}</div>}
    {!running && <footer className="agent-run-meta">
      <span>{job.model_name || "默认模型"}</span><i />
      <span>{formatDuration(totalDuration)}</span><i />
      <span>{latestTurn} 轮 · {toolEvents.length} 次工具调用</span>
      {failedTools.length > 0 && job.status === "succeeded" && <><i /><span>已自动纠正 {failedTools.length} 次异常</span></>}
    </footer>}
  </div>;
}

type WorkspaceStreamEvent =
  | { type: "delta"; text: string }
  | { type: "done"; model_name?: string; latency_ms?: number }
  | { type: "persisted"; conversation_id: string; message_id: string; latency_ms?: number }
  | { type: "error"; code: string; message: string };

interface StreamingTurn {
  conversationId: string;
  prompt: string;
  attachments: Array<{ name: string; size: number }>;
  answer: string;
  startedAt: number;
  modelName: string;
  latencyMs?: number;
}

export function DashboardPage() {
  const { user } = useAuth();
  const ownedSkills = useLoad<Skill[]>("/skills/mine", []);
  const communitySkills = useLoad<Skill[]>("/community/skills", []);
  const availableModels = useLoad<AvailableModels>("/models/available", { configured: false, models: [], default_model: null });
  const [conversations, setConversations] = useState<AgentWorkspaceConversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<AgentWorkspaceConversationDetail | null>(null);
  const [messageParts, setMessageParts] = useState<WorkflowMessagePart[]>([]);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [selectedHistoryFiles, setSelectedHistoryFiles] = useState<AgentMessageFile[]>([]);
  const [fileMenuOpen, setFileMenuOpen] = useState(false);
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [conversationMenuOpen, setConversationMenuOpen] = useState(false);
  const [conversationQuery, setConversationQuery] = useState("");
  const [editingConversationId, setEditingConversationId] = useState<string | null>(null);
  const [editingConversationTitle, setEditingConversationTitle] = useState("");
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null);
  const [conversationActionId, setConversationActionId] = useState<string | null>(null);
  const [selectedModelName, setSelectedModelName] = useState("");
  const [launching, setLaunching] = useState(false);
  const [streamingTurn, setStreamingTurn] = useState<StreamingTurn | null>(null);
  const [loadingConversationId, setLoadingConversationId] = useState<string | null>(null);
  const [launchError, setLaunchError] = useState("");
  const [showScrollToLatest, setShowScrollToLatest] = useState(false);
  const formRef = useRef<HTMLFormElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const skillMenuRef = useRef<HTMLDivElement | null>(null);
  const fileMenuRef = useRef<HTMLDivElement | null>(null);
  const modelMenuRef = useRef<HTMLDivElement | null>(null);
  const conversationMenuRef = useRef<HTMLDivElement | null>(null);
  const conversationDrawerRef = useRef<HTMLElement | null>(null);
  const editorRef = useRef<SkillPromptEditorHandle | null>(null);
  const messagesScrollerRef = useRef<HTMLDivElement | null>(null);
  const followLatestRef = useRef(true);
  const workspaceRef = useRef<HTMLDivElement | null>(null);

  useGSAP(() => {
    if (activeConversation || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const entrance = gsap.timeline({ defaults: { duration: 0.48, ease: "power3.out" } });
    entrance
      .from(".agent-start-heading", { y: 14, autoAlpha: 0 }, 0)
      .from(".agent-composer-heading", { y: 8, autoAlpha: 0 }, 0.1)
      .from(".agent-start-composer", { y: 16, scale: 0.994, autoAlpha: 0, duration: 0.56 }, 0.15);
  }, { scope: workspaceRef, dependencies: [Boolean(activeConversation)], revertOnUpdate: true });

  useEffect(() => {
    api<AgentWorkspaceConversation[]>("/agent/conversations").then(setConversations).catch(() => undefined);
  }, []);

  const availableSkills = useMemo(
    () => [
      ...ownedSkills,
      ...communitySkills.filter((candidate) => !ownedSkills.some((owned) => owned.id === candidate.id)),
    ].filter((skill) => Boolean(skill.latest_version)),
    [communitySkills, ownedSkills],
  );
  const selectedSkillIds = messageParts
    .filter((part): part is Extract<WorkflowMessagePart, { type: "skill_ref" }> => part.type === "skill_ref")
    .map((part) => part.skill_id);
  const runningJobIds = (activeConversation?.messages || [])
    .map((message) => message.job)
    .filter((job): job is WorkflowJob => Boolean(job) && activeStatuses.has(job!.status))
    .map((job) => job.id);
  const pollingKey = runningJobIds.join(",");
  const conversationBusy = runningJobIds.length > 0;
  const historyFiles = useMemo(() => {
    const bySha = new Map<string, AgentMessageFile>();
    (activeConversation?.messages || []).forEach((message) => message.files.forEach((file) => bySha.set(file.sha256, file)));
    return [...bySha.values()].reverse();
  }, [activeConversation?.messages]);

  function syncConversation(detail: AgentWorkspaceConversationDetail) {
    setActiveConversation(detail);
    setConversations((current) => [detail, ...current.filter((item) => item.id !== detail.id)]);
  }

  useEffect(() => {
    if (!selectedModelName && availableModels.default_model) setSelectedModelName(availableModels.default_model);
  }, [availableModels.default_model, selectedModelName]);

  useEffect(() => {
    const availableIds = new Set(availableSkills.map((skill) => skill.id));
    selectedSkillIds.filter((id) => !availableIds.has(id)).forEach((id) => editorRef.current?.removeSkill(id));
  }, [availableSkills, selectedSkillIds]);

  useEffect(() => {
    if (!skillMenuOpen && !modelMenuOpen && !conversationMenuOpen && !fileMenuOpen) return;
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!skillMenuRef.current?.contains(target)) setSkillMenuOpen(false);
      if (!modelMenuRef.current?.contains(target)) setModelMenuOpen(false);
      if (!conversationMenuRef.current?.contains(target) && !conversationDrawerRef.current?.contains(target)) setConversationMenuOpen(false);
      if (!fileMenuRef.current?.contains(target)) setFileMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [conversationMenuOpen, fileMenuOpen, modelMenuOpen, skillMenuOpen]);

  useEffect(() => {
    if (!conversationMenuOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setConversationMenuOpen(false);
    };
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [conversationMenuOpen]);

  useEffect(() => {
    if (!activeConversation || !pollingKey) return;
    let disposed = false;
    const refresh = async () => {
      try {
        const detail = await api<AgentWorkspaceConversationDetail>(`/agent/conversations/${activeConversation.id}`);
        if (!disposed) syncConversation(detail);
      } catch {
        // Keep the last visible state and retry while the job remains active.
      }
    };
    const timer = window.setInterval(() => void refresh(), 1600);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [activeConversation?.id, pollingKey]);

  function scrollMessagesToLatest(behavior: ScrollBehavior = "auto") {
    const scroller = messagesScrollerRef.current;
    if (!scroller) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scroller.scrollTo({
      top: scroller.scrollHeight,
      behavior: reducedMotion ? "auto" : behavior,
    });
  }

  function followLatestMessages(behavior: ScrollBehavior = "smooth") {
    followLatestRef.current = true;
    setShowScrollToLatest(false);
    window.requestAnimationFrame(() => scrollMessagesToLatest(behavior));
  }

  function handleMessagesScroll() {
    const scroller = messagesScrollerRef.current;
    if (!scroller) return;
    const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    const isNearBottom = distanceFromBottom < 96;
    followLatestRef.current = isNearBottom;
    setShowScrollToLatest(!isNearBottom);
  }

  useEffect(() => {
    if (!activeConversation) return;
    followLatestRef.current = true;
    setShowScrollToLatest(false);
    const frame = window.requestAnimationFrame(() => scrollMessagesToLatest("auto"));
    return () => window.cancelAnimationFrame(frame);
  }, [activeConversation?.id]);

  useEffect(() => {
    if (!activeConversation || !followLatestRef.current) return;
    const frame = window.requestAnimationFrame(() => scrollMessagesToLatest("auto"));
    return () => window.cancelAnimationFrame(frame);
  }, [activeConversation?.message_count, activeConversation?.messages, launching, streamingTurn?.answer, streamingTurn?.conversationId]);

  function toggleSkill(skill: Skill) {
    if (selectedSkillIds.includes(skill.id)) {
      editorRef.current?.removeSkill(skill.id);
      return;
    }
    if (selectedSkillIds.length >= 5) {
      setLaunchError("一次任务最多可以挂载 5 个 Skill");
      return;
    }
    editorRef.current?.insertSkill(skill);
    setLaunchError("");
  }

  async function ensureConversation(): Promise<string> {
    if (activeConversation) return activeConversation.id;
    const created = await api<AgentWorkspaceConversation>("/agent/conversations", {
      method: "POST",
      body: JSON.stringify({}),
    });
    syncConversation({ ...created, messages: [] });
    return created.id;
  }

  async function openConversation(conversationId: string) {
    if (loadingConversationId || conversationId === activeConversation?.id) return;
    setLoadingConversationId(conversationId);
    setLaunchError("");
    try {
      syncConversation(await api<AgentWorkspaceConversationDetail>(`/agent/conversations/${conversationId}`));
      setConversationMenuOpen(false);
      editorRef.current?.clear();
      setMessageParts([]);
      setAttachments([]);
      setSelectedHistoryFiles([]);
    } catch (reason) {
      setLaunchError(reason instanceof Error ? reason.message : "无法打开会话");
    } finally {
      setLoadingConversationId(null);
    }
  }

  function startNewConversation() {
    setActiveConversation(null);
    setConversationMenuOpen(false);
    setEditingConversationId(null);
    setDeletingConversationId(null);
    editorRef.current?.clear();
    setMessageParts([]);
    setAttachments([]);
    setSelectedHistoryFiles([]);
    setLaunchError("");
    if (fileRef.current) fileRef.current.value = "";
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  function beginRenameConversation(conversation: AgentWorkspaceConversation) {
    setEditingConversationId(conversation.id);
    setEditingConversationTitle(conversation.title);
    setDeletingConversationId(null);
  }

  async function renameConversation(conversationId: string) {
    const title = editingConversationTitle.trim();
    if (!title || conversationActionId) return;
    setConversationActionId(conversationId);
    setLaunchError("");
    try {
      const updated = await api<AgentWorkspaceConversation>(`/agent/conversations/${conversationId}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      setActiveConversation((current) => current?.id === updated.id ? { ...current, title: updated.title, updated_at: updated.updated_at } : current);
      setEditingConversationId(null);
      setEditingConversationTitle("");
    } catch (reason) {
      setLaunchError(reason instanceof Error ? reason.message : "会话名称修改失败");
    } finally {
      setConversationActionId(null);
    }
  }

  async function deleteConversation(conversationId: string) {
    if (conversationActionId) return;
    setConversationActionId(conversationId);
    setLaunchError("");
    try {
      await api<void>(`/agent/conversations/${conversationId}`, { method: "DELETE" });
      setConversations((current) => current.filter((item) => item.id !== conversationId));
      if (activeConversation?.id === conversationId) startNewConversation();
      setDeletingConversationId(null);
    } catch (reason) {
      setLaunchError(reason instanceof Error ? reason.message : "会话删除失败");
    } finally {
      setConversationActionId(null);
    }
  }

  function clearComposer() {
    editorRef.current?.clear();
    setMessageParts([]);
    setAttachments([]);
    setSelectedHistoryFiles([]);
    setSkillMenuOpen(false);
    setFileMenuOpen(false);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function launchAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if ((!hasPromptContent(messageParts) && !attachments.length && !selectedHistoryFiles.length) || launching || conversationBusy) return;
    const textSize = messageParts.reduce((size, part) => size + (part.type === "text" ? part.text.length : 0), 0);
    if (textSize > 20_000) {
      setLaunchError("任务描述不能超过 20,000 个字符");
      return;
    }
    const submittedParts = messageParts.map((part) => ({ ...part })) as WorkflowMessagePart[];
    const submittedLocalFiles = [...attachments];
    const submittedHistoryFiles = [...selectedHistoryFiles];
    let clearedBeforeRequest = false;
    let receivedStreamEvent = false;
    const focusedElement = document.activeElement;
    if (focusedElement instanceof HTMLElement && formRef.current?.contains(focusedElement)) focusedElement.blur();
    followLatestMessages("auto");
    setLaunching(true);
    setLaunchError("");
    let submittedConversationId = "";
    try {
      const conversationId = await ensureConversation();
      submittedConversationId = conversationId;
      if (!selectedSkillIds.length) {
        const submittedPrompt = promptText(messageParts) || "请阅读并说明这些附件的主要内容。";
        const submittedAttachments = [
          ...attachments.map((file) => ({ name: file.name, size: file.size })),
          ...selectedHistoryFiles.map((file) => ({ name: file.filename, size: file.size_bytes })),
        ];
        const body = new FormData();
        body.set("message", promptText(messageParts));
        if (selectedModelName) body.set("model_name", selectedModelName);
        attachments.forEach((file) => body.append("files", file));
        if (selectedHistoryFiles.length) body.set("existing_file_ids", JSON.stringify(selectedHistoryFiles.map((file) => file.id)));
        setStreamingTurn({
          conversationId,
          prompt: submittedPrompt,
          attachments: submittedAttachments,
          answer: "",
          startedAt: Date.now(),
          modelName: selectedModelName,
        });
        clearComposer();
        clearedBeforeRequest = true;
        let streamError = "";
        await apiNdjson<WorkspaceStreamEvent>(`/agent/conversations/${conversationId}/messages/stream`, { method: "POST", body }, (streamEvent) => {
          receivedStreamEvent = true;
          if (streamEvent.type === "delta") {
            setStreamingTurn((current) => current ? { ...current, answer: current.answer + streamEvent.text } : current);
          } else if (streamEvent.type === "done") {
            setStreamingTurn((current) => current ? {
              ...current,
              modelName: streamEvent.model_name || current.modelName,
              latencyMs: streamEvent.latency_ms,
            } : current);
          } else if (streamEvent.type === "error") {
            streamError = streamEvent.message;
          }
        });
        if (streamError) throw new Error(streamError);
        syncConversation(await api<AgentWorkspaceConversationDetail>(`/agent/conversations/${conversationId}`));
        setStreamingTurn(null);
      } else {
        const selectedDetails = await Promise.all(selectedSkillIds.map(async (skillId) => {
          const detail = await api<Skill>(`/skills/${skillId}`);
          const version = runnableVersion(detail);
          if (!version) throw new Error(`${detail.name} 暂时没有可运行版本`);
          return { detail, version };
        }));
        const versionsBySkill = new Map(selectedDetails.map((item) => [item.detail.id, item.version]));
        const normalizedParts = messageParts.map((part): WorkflowMessagePart => {
          if (part.type === "text") return part;
          const version = versionsBySkill.get(part.skill_id);
          if (!version) throw new Error(`${part.skill_name} 暂时没有可运行版本`);
          return { ...part, skill_version_id: version.id, version: version.version };
        });
        const body = new FormData();
        body.set("agent_conversation_id", conversationId);
        body.set("message_content", JSON.stringify(normalizedParts));
        body.set("instruction", promptText(normalizedParts));
        body.set("version_id", selectedDetails[0].version.id);
        body.set("version_ids", JSON.stringify(selectedDetails.map((item) => item.version.id)));
        if (selectedModelName) body.set("model_name", selectedModelName);
        attachments.forEach((file) => body.append("files", file));
        if (selectedHistoryFiles.length) body.set("existing_file_ids", JSON.stringify(selectedHistoryFiles.map((file) => file.id)));
        await api<WorkflowJob>("/jobs", { method: "POST", body });
        syncConversation(await api<AgentWorkspaceConversationDetail>(`/agent/conversations/${conversationId}`));
        clearComposer();
      }
    } catch (reason) {
      setLaunchError(reason instanceof Error ? reason.message : "无法发送消息");
      if (clearedBeforeRequest && !receivedStreamEvent) {
        editorRef.current?.setParts(submittedParts);
        setAttachments(submittedLocalFiles);
        setSelectedHistoryFiles(submittedHistoryFiles);
      }
      if (submittedConversationId) {
        try {
          syncConversation(await api<AgentWorkspaceConversationDetail>(`/agent/conversations/${submittedConversationId}`));
          setStreamingTurn(null);
        } catch {
          // Keep the optimistic message visible when the refresh is unavailable.
        }
      }
    } finally {
      setLaunching(false);
    }
  }

  async function downloadArtifact(job: WorkflowJob, artifact: WorkflowArtifact) {
    try {
      const blob = await apiBlob(`/jobs/${job.id}/artifacts/${artifact.id}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setLaunchError(reason instanceof Error ? reason.message : "产物下载失败");
    }
  }

  async function retryJob(job: WorkflowJob) {
    if (launching || conversationBusy || !activeConversation) return;
    setLaunching(true);
    setLaunchError("");
    try {
      await api<WorkflowJob>(`/jobs/${job.id}/retry`, { method: "POST" });
      syncConversation(await api<AgentWorkspaceConversationDetail>(`/agent/conversations/${activeConversation.id}`));
    } catch (reason) {
      setLaunchError(reason instanceof Error ? reason.message : "任务重试失败");
    } finally {
      setLaunching(false);
    }
  }

  function editFailedJob(job: WorkflowJob) {
    editorRef.current?.setParts(job.message_content.length ? job.message_content : [{ type: "text", text: job.instruction }]);
    const reusable = historyFiles.filter((file) => job.input_files.some((input) => input.sha256 === file.sha256));
    setSelectedHistoryFiles(reusable.slice(0, 5));
    setAttachments([]);
    setLaunchError(reusable.length < job.input_files.filter((file) => file.filename !== "task-request.txt").length
      ? "部分原附件已不在当前会话中，请重新添加后发送"
      : "已恢复原任务，可修改文字、Skill 或附件后重新发送");
    window.setTimeout(() => editorRef.current?.focus(), 0);
  }

  function addLocalFiles(files: File[]) {
    const oversized = files.find((file) => file.size > 10 * 1024 * 1024);
    if (oversized) {
      setLaunchError(`《${oversized.name}》超过 10 MB`);
      return;
    }
    const existingNames = new Set([
      ...attachments.map((file) => file.name.toLocaleLowerCase()),
      ...selectedHistoryFiles.map((file) => file.filename.toLocaleLowerCase()),
    ]);
    const unique = files.filter((file) => !existingNames.has(file.name.toLocaleLowerCase()));
    if (attachments.length + selectedHistoryFiles.length + unique.length > 5) {
      setLaunchError("一次最多添加 5 个附件");
      return;
    }
    setAttachments((current) => [...current, ...unique]);
    setLaunchError(unique.length === files.length ? "" : "已忽略同名附件");
  }

  function toggleHistoryFile(file: AgentMessageFile) {
    if (selectedHistoryFiles.some((item) => item.id === file.id)) {
      setSelectedHistoryFiles((current) => current.filter((item) => item.id !== file.id));
      return;
    }
    if (attachments.some((item) => item.name.toLocaleLowerCase() === file.filename.toLocaleLowerCase())) {
      setLaunchError("不能同时添加两个同名附件");
      return;
    }
    if (attachments.length + selectedHistoryFiles.length >= 5) {
      setLaunchError("一次最多添加 5 个附件");
      return;
    }
    setSelectedHistoryFiles((current) => [...current, file]);
    setLaunchError("");
  }

  const canSend = hasPromptContent(messageParts) || attachments.length > 0 || selectedHistoryFiles.length > 0;
  const composerDisabled = launching || conversationBusy;
  const conversationMessages = activeConversation?.messages || [];
  const visibleConversations = conversations
    .filter((conversation) => conversation.title.toLocaleLowerCase().includes(conversationQuery.trim().toLocaleLowerCase()))
    .slice(0, 30);

  const conversationHistory: ReactNode = <div className="agent-conversation-history-wrap" ref={conversationMenuRef}>
    <button className="agent-conversation-history-trigger" type="button" aria-haspopup="dialog" aria-expanded={conversationMenuOpen} onClick={() => { setConversationMenuOpen((open) => !open); setSkillMenuOpen(false); setModelMenuOpen(false); }}><MessageSquareText />历史对话{conversations.length > 0 && <small>{conversations.length}</small>}</button>
    {conversationMenuOpen && createPortal(<div className="agent-drawer-layer">
      <button className="agent-drawer-backdrop" type="button" aria-label="关闭历史对话" onClick={() => setConversationMenuOpen(false)} />
      <aside className="agent-conversation-history-menu agent-side-drawer" ref={conversationDrawerRef} role="dialog" aria-modal="true" aria-label="历史对话">
        <header><span><strong>历史对话</strong><small>{conversations.length} 个会话</small></span><button type="button" aria-label="关闭历史对话" onClick={() => setConversationMenuOpen(false)}><X /></button></header>
        <label className="agent-conversation-history-search"><input autoFocus type="search" value={conversationQuery} placeholder="搜索会话名称" onChange={(event) => setConversationQuery(event.target.value)} />{conversationQuery && <button type="button" aria-label="清空搜索" onClick={() => setConversationQuery("")}><X /></button>}</label>
        <div>{visibleConversations.length ? visibleConversations.map((conversation) => <article className={`${conversation.id === activeConversation?.id ? "active" : ""}${deletingConversationId === conversation.id ? " deleting" : ""}`} key={conversation.id}>
          {editingConversationId === conversation.id ? <form className="agent-conversation-rename" onSubmit={(event) => { event.preventDefault(); void renameConversation(conversation.id); }}><label><span>会话名称</span><input autoFocus maxLength={160} value={editingConversationTitle} onChange={(event) => setEditingConversationTitle(event.target.value)} /></label><div><button className="save" type="submit" disabled={!editingConversationTitle.trim() || conversationActionId === conversation.id}>{conversationActionId === conversation.id ? <RotateCw className="spin-icon" /> : <Check />}保存</button><button type="button" onClick={() => setEditingConversationId(null)}><X />取消</button></div></form> : <>
            <button className="agent-conversation-open" type="button" disabled={Boolean(loadingConversationId || conversationActionId)} onClick={() => void openConversation(conversation.id)}><MessageSquareText /><span><strong>{conversation.title}</strong><small>{Math.ceil(conversation.message_count / 2)} 轮 · {new Date(conversation.updated_at).toLocaleString("zh-CN")}</small></span>{loadingConversationId === conversation.id ? <RotateCw className="spin-icon" /> : <ChevronRight />}</button>
            <div className="agent-conversation-item-actions">{deletingConversationId === conversation.id ? <><button className="confirm-delete" type="button" disabled={conversationActionId === conversation.id} onClick={() => void deleteConversation(conversation.id)}>{conversationActionId === conversation.id ? <RotateCw className="spin-icon" /> : <Trash2 />}确认删除</button><button type="button" onClick={() => setDeletingConversationId(null)}><X />取消</button></> : <><button type="button" onClick={() => beginRenameConversation(conversation)}><PencilLine />重命名</button><button className="delete" type="button" onClick={() => { setDeletingConversationId(conversation.id); setEditingConversationId(null); }}><Trash2 />删除</button></>}</div>
          </>}
        </article>) : <p>{conversationQuery ? "没有匹配的会话。" : "还没有历史对话。"}</p>}</div>
      </aside>
    </div>, document.body)}
  </div>;

  const composer: ReactNode = <form ref={formRef} className={`agent-start-composer${activeConversation ? " in-dialogue" : ""}${composerDisabled ? " is-busy" : ""}`} aria-busy={composerDisabled} onSubmit={launchAgent}>
    {(attachments.length > 0 || selectedHistoryFiles.length > 0) && <div className="agent-start-files">
      {attachments.map((file) => <div className="agent-start-file" key={`local-${file.name}`}><FileText /><span><strong>{file.name}</strong><small>{formatSize(file.size)}</small></span><button type="button" aria-label={`移除附件 ${file.name}`} onClick={() => setAttachments((current) => current.filter((item) => item !== file))}><X /></button></div>)}
      {selectedHistoryFiles.map((file) => <div className="agent-start-file reused" key={`history-${file.id}`}><FolderClock /><span><strong>{file.filename}</strong><small>会话文件 · {formatSize(file.size_bytes)}</small></span><button type="button" aria-label={`移除会话文件 ${file.filename}`} onClick={() => setSelectedHistoryFiles((current) => current.filter((item) => item.id !== file.id))}><X /></button></div>)}
    </div>}
    <SkillPromptEditor
      ref={editorRef}
      disabled={composerDisabled}
      onChange={(parts) => {
        setMessageParts(parts);
        if (parts.reduce((size, part) => size + (part.type === "text" ? part.text.length : 0), 0) <= 20_000) setLaunchError("");
      }}
      onSubmitRequest={() => formRef.current?.requestSubmit()}
    />
    <footer>
      <button className="agent-start-attach" type="button" title="添加附件" aria-label="添加附件" disabled={composerDisabled} onClick={() => fileRef.current?.click()}><Paperclip /></button>
      <input ref={fileRef} type="file" multiple hidden accept=".txt,.md,.csv,.json,.yaml,.yml,.log,.html,.htm,.xml,.docx,.xlsx,.pdf,.png,.jpg,.jpeg" onChange={(event) => { addLocalFiles(Array.from(event.target.files || [])); event.target.value = ""; }} />
      {activeConversation && historyFiles.length > 0 && <div className="agent-file-reuse-wrap" ref={fileMenuRef}>
        <button className={`agent-start-add${fileMenuOpen ? " active" : ""}`} type="button" title="使用本会话文件" aria-label="使用本会话文件" aria-expanded={fileMenuOpen} disabled={composerDisabled} onClick={() => { setFileMenuOpen((open) => !open); setSkillMenuOpen(false); setModelMenuOpen(false); }}><FolderClock /></button>
        {fileMenuOpen && <div className="agent-file-popover"><header><strong>本会话文件</strong><small>无需重新上传</small></header><div>{historyFiles.map((file) => { const selected = selectedHistoryFiles.some((item) => item.id === file.id); return <button type="button" className={selected ? "selected" : ""} key={file.id} onClick={() => toggleHistoryFile(file)}><FileText /><span><strong>{file.filename}</strong><small>{formatSize(file.size_bytes)}</small></span>{selected && <Check />}</button>; })}</div></div>}
      </div>}
      <div className="agent-skill-add-wrap" ref={skillMenuRef}>
        <button className={`agent-start-add${skillMenuOpen ? " active" : ""}`} type="button" title="插入 Skill" aria-label="插入 Skill" aria-expanded={skillMenuOpen} disabled={composerDisabled} onMouseDown={(event) => event.preventDefault()} onClick={() => { setSkillMenuOpen((open) => !open); setModelMenuOpen(false); setFileMenuOpen(false); }}><Plus /></button>
        {skillMenuOpen && <div className="agent-skill-popover">
          <header><span><strong>插入 Skill</strong><small>插入到光标位置 · 最多 5 个</small></span><Link to="/app/skills" onClick={() => setSkillMenuOpen(false)}>管理</Link></header>
          {availableSkills.length ? <div className="agent-skill-options">{availableSkills.map((skill) => { const selected = selectedSkillIds.includes(skill.id); return <button key={skill.id} type="button" aria-pressed={selected} className={selected ? "selected" : ""} onMouseDown={(event) => event.preventDefault()} onClick={() => toggleSkill(skill)}><span className="skill-icon small"><Workflow /></span><span><strong>{skill.name}</strong><small>{skill.summary}</small></span>{selected && <Check />}</button>; })}</div> : <div className="agent-skill-empty"><span>还没有可用的 Skill</span><Link to="/app/skills/new">上传第一个 Skill</Link></div>}
        </div>}
      </div>
      {selectedSkillIds.length > 0 && <span className="agent-start-route-mode">明确执行 · {selectedSkillIds.length} 个 Skill</span>}
      <div className="agent-start-model-wrap" ref={modelMenuRef}>
        <button type="button" className="agent-start-model" disabled={composerDisabled || !availableModels.configured} aria-expanded={modelMenuOpen} onClick={() => { setModelMenuOpen((open) => !open); setSkillMenuOpen(false); setFileMenuOpen(false); }}><span>{selectedModelName || "默认模型"}</span><ChevronDown /></button>
        {modelMenuOpen && <div className="agent-model-popover">{availableModels.models.map((model) => <button type="button" key={model} className={model === selectedModelName ? "selected" : ""} onClick={() => { setSelectedModelName(model); setModelMenuOpen(false); }}>{model}<Check /></button>)}</div>}
      </div>
      <button className="agent-start-send" type="submit" aria-label="发送消息" disabled={!canSend || composerDisabled}>{launching ? <RotateCw className="spin-icon" /> : <ArrowRight />}</button>
    </footer>
  </form>;

  return <div className="agent-start-page" ref={workspaceRef}>
    <header className="agent-start-topbar">
      <div className="agent-workbench-title"><i /><strong>任务工作台</strong><span>对话、Skill 与文件在这里协作</span></div>
      <div className="agent-start-topbar-actions">{!activeConversation && conversationHistory}</div>
    </header>

    <section className={`agent-start-main${activeConversation ? " has-conversation" : ""}`}>
      {!activeConversation ? <>
        <div className="agent-start-heading"><p>{greeting()}，{user?.display_name}</p></div>
        {composer}
      </> : <div className="agent-workspace-dialogue">
        <header>
          <div><div><strong>{activeConversation.title}</strong><small>普通消息直接回复 · Skill 任务在当前对话中运行</small></div></div>
          <div className="agent-conversation-actions">
            {conversationHistory}
            <button type="button" onClick={startNewConversation}><Plus />新对话</button>
          </div>
        </header>
        <div className="agent-workspace-messages-wrap">
          <div className="agent-workspace-messages" ref={messagesScrollerRef} aria-live="polite" onScroll={handleMessagesScroll}>
            {conversationMessages.map((message) => message.role === "user" ? <article className="agent-workspace-message user" key={message.id}>
              <div className="agent-workspace-bubble"><StructuredPrompt parts={message.content.parts} fallback={String(message.content.message || "")} />
                {(message.files.length > 0 || (message.content.files?.length || 0) > 0) && <div className="agent-workspace-files">{message.files.length > 0 ? message.files.map((file) => <span key={file.id}><Paperclip />{file.filename}<small>{formatSize(file.size_bytes)}</small></span>) : message.content.files?.map((file, index) => <span key={`${file.filename}-${index}`}><Paperclip />{file.filename}<small>{formatSize(file.size_bytes)}</small></span>)}</div>}
              </div><time>{formatTime(message.created_at)}</time>
            </article> : <article className="agent-workspace-message assistant" key={message.id}><div>{message.kind === "workflow" && message.job ? <WorkflowReply job={message.job} onDownload={(job, artifact) => void downloadArtifact(job, artifact)} onRetry={(job) => void retryJob(job)} onEdit={editFailedJob} /> : <><MarkdownContent className="agent-workspace-answer">{String(message.content.message || "")}</MarkdownContent><time>{formatTime(message.created_at)}{message.model_name ? ` · ${message.model_name}` : ""}{typeof message.content.latency_ms === "number" ? ` · ${formatDuration(message.content.latency_ms)}` : ""}</time></>}</div></article>)}
            {streamingTurn?.conversationId === activeConversation.id && <>
              <article className="agent-workspace-message user streaming-user">
                <div className="agent-workspace-bubble">{streamingTurn.prompt}{streamingTurn.attachments.length > 0 && <div className="agent-workspace-files">{streamingTurn.attachments.map((file, index) => <span key={`${file.name}-${index}`}><Paperclip />{file.name}<small>{formatSize(file.size)}</small></span>)}</div>}</div>
                <time>{new Date(streamingTurn.startedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time>
              </article>
              <article className="agent-workspace-message assistant streaming-assistant"><div>{streamingTurn.answer ? <MarkdownContent className="agent-workspace-answer">{streamingTurn.answer}</MarkdownContent> : <div className="agent-workspace-answer"><span className="agent-stream-waiting"><i /><span>正在思考</span></span></div>}{streamingTurn.answer && <time>{streamingTurn.modelName || "默认模型"}{streamingTurn.latencyMs ? ` · ${formatDuration(streamingTurn.latencyMs)}` : " · 正在生成"}</time>}</div></article>
            </>}
            {launching && !streamingTurn && <article className="agent-workspace-message assistant pending"><div className="agent-workspace-answer"><RotateCw className="spin-icon" />正在准备任务…</div></article>}
          </div>
          {showScrollToLatest && <button className="agent-scroll-latest" type="button" onClick={() => followLatestMessages("smooth")}><ArrowDown />{launching ? "查看最新回复" : "回到最新消息"}</button>}
        </div>
        {composer}
      </div>}
      {launchError && <div className="agent-start-error">{launchError}</div>}
    </section>

  </div>;
}
