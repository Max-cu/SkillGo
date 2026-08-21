import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Boxes,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  CloudUpload,
  Code2,
  Copy,
  Download,
  FileCheck2,
  FileText,
  Filter,
  Github,
  Heart,
  CircleHelp,
  KeyRound,
  LockKeyhole,
  MessageSquareText,
  Paperclip,
  PencilLine,
  Play,
  Plus,
  Power,
  PowerOff,
  RotateCw,
  Save,
  SendHorizontal,
  ShieldCheck,
  Sparkles,
  Trash2,
  UploadCloud,
  UserRoundCheck,
  Users,
  Workflow,
  X,
  Zap
} from "lucide-react";
import { FormEvent, useEffect, useRef, useState, type ChangeEvent, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { api, apiBlob, ApiError } from "./api";
import { useAuth } from "./auth";
import { Breadcrumbs, categoryLabel, EmptyState, PageTitle, PublicHeader, roleLabels, SkillCard, skillTypeLabels, StatusBadge, TiltSurface, visibilityLabels } from "./components";
import { Link, Navigate, useNavigate, useParams } from "./router";
import type { AvailableModels, Conversation, ConversationDetail, ConversationMessage, Endpoint, EndpointCreated, ModelConnectionItem, ModelConnectionList, ModelConnectionTestResult, Role, RunStatus, Skill, SkillPackageAnalysis, SkillRun, SkillVersion, User, Visibility, WorkflowArtifact, WorkflowJob, WorkflowJobStatus, WorkspaceFile } from "./types";

gsap.registerPlugin(useGSAP);

function useLoad<T>(path: string, initial: T) {
  const [data, setData] = useState<T>(initial);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    api<T>(path).then(setData).catch((reason: Error) => setError(reason.message)).finally(() => setLoading(false));
  }, [path]);
  return { data, setData, loading, error };
}

const maxSkillPackageBytes = 20 * 1024 * 1024;

const executionModeLabels: Record<string, string> = {
  instruction_only: "纯指令",
  platform_tools: "平台工具",
  sandbox_required: "沙箱工作流",
};

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

function workflowJobSkills(job: WorkflowJob) {
  return job.selected_skills?.length
    ? job.selected_skills
    : [{ skill_id: job.skill_id, skill_version_id: job.skill_version_id, skill_name: job.skill_name, version: job.version, position: 1 }];
}

function workflowJobSkillLabel(job: WorkflowJob) {
  const skills = workflowJobSkills(job);
  return skills.length > 1 ? `${skills.length} 个 Skill 协作` : skills[0].skill_name;
}

function endpointInvokePath(endpoint: Pick<Endpoint, "slug" | "invocation_mode">) {
  return endpoint.invocation_mode === "async"
    ? `/api/v1/workflow-endpoints/${endpoint.slug}/jobs`
    : `/api/v1/invoke/${endpoint.slug}`;
}

function endpointCurlExample(endpoint: Endpoint) {
  const url = `${window.location.origin}${endpointInvokePath(endpoint)}`;
  if (endpoint.invocation_mode === "async") {
    return `curl -X POST "${url}" \\\n+  -H "X-SkillGo-Key: $SKILLGO_API_KEY" \\\n+  -H "Idempotency-Key: request-001" \\\n+  -F "file=@./input.docx" \\\n+  -F "instruction=请完整执行并生成产物"`;
  }
  return `curl -X POST "${url}" \\\n+  -H "X-SkillGo-Key: $SKILLGO_API_KEY" \\\n+  -H "Content-Type: application/json" \\\n+  -d '{"input":{"content":"请总结这段内容"}}'`;
}

function formatPackageSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function SkillPackagePicker({ file, onChange, busy = false, busyLabel = "正在上传和校验…", compact = false, required = false }: {
  file: File | null;
  onChange: (file: File | null) => void;
  busy?: boolean;
  busyLabel?: string;
  compact?: boolean;
  required?: boolean;
}) {
  const [dragging, setDragging] = useState(false);
  const [problem, setProblem] = useState("");

  function acceptFile(selected: File | null, input?: HTMLInputElement) {
    setProblem("");
    if (!selected) {
      onChange(null);
      return;
    }
    if (!selected.name.toLowerCase().endsWith(".zip")) {
      if (input) input.value = "";
      onChange(null);
      setProblem("请选择 .zip 格式的 Skill 包");
      return;
    }
    if (selected.size > maxSkillPackageBytes) {
      if (input) input.value = "";
      onChange(null);
      setProblem("ZIP 包不能超过 20 MB");
      return;
    }
    onChange(selected);
  }

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    acceptFile(event.target.files?.[0] || null, event.target);
  }

  const className = ["upload-drop", "package-picker", compact ? "compact-drop" : "", file ? "selected" : "", dragging ? "dragging" : "", busy ? "busy" : ""].filter(Boolean).join(" ");
  return <label className={className} onDragEnter={(event) => { event.preventDefault(); setDragging(true); }} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; setDragging(true); }} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }} onDrop={(event) => { event.preventDefault(); setDragging(false); if (!busy) acceptFile(event.dataTransfer.files?.[0] || null); }}>
    <span className="package-picker-icon">{busy ? <RotateCw className="spin-icon" /> : file ? <FileCheck2 /> : <CloudUpload />}</span>
    <span className="package-picker-copy" aria-live="polite">
      <strong title={file?.name}>{file ? file.name : "选择 Skill ZIP"}</strong>
      <span>{file ? `${formatPackageSize(file.size)} · ZIP 包 · 待上传` : "最大 20 MB；兼容标准 SKILL.md 与 SkillGo 扩展包"}</span>
    </span>
    <span className="package-picker-action">{busy ? busyLabel : file ? "已选择，点击可更换" : "点击选择或拖入 ZIP"}</span>
    {problem && <span className="package-picker-error">{problem}</span>}
    <input name="package" type="file" accept=".zip,application/zip" aria-label="选择 Skill ZIP 包" required={required} disabled={busy} onChange={selectFile} />
  </label>;
}

function InteractiveSkillCore() {
  const [burstKey, setBurstKey] = useState(0);

  function move(event: ReactPointerEvent<HTMLDivElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
    const y = Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height));
    event.currentTarget.style.setProperty("--cube-rx", `${((0.5 - y) * 15).toFixed(2)}deg`);
    event.currentTarget.style.setProperty("--cube-ry", `${((x - 0.5) * 20).toFixed(2)}deg`);
  }

  function reset(event: ReactPointerEvent<HTMLDivElement>) {
    event.currentTarget.style.setProperty("--cube-rx", "-3deg");
    event.currentTarget.style.setProperty("--cube-ry", "4deg");
  }

  return <div className="hero-visual interactive-fluent-cube" onPointerMove={move} onPointerLeave={reset} onPointerCancel={reset}>
    <div className="cube-aurora cube-aurora-blue" />
    <div className="cube-aurora cube-aurora-violet" />
    <div className="cube-halo cube-halo-outer" />
    <div className="cube-halo cube-halo-inner" />
    <Link className="fluent-cube-scene" to="/app" aria-label="进入 SkillGo 工作台" title="进入工作台" onPointerEnter={() => setBurstKey((current) => current + 1)} onFocus={(event) => { if (event.currentTarget.matches(":focus-visible")) setBurstKey((current) => current + 1); }}>
      <div className={`fluent-cube-float ${burstKey ? "go-impact" : ""}`} key={`cube-${burstKey}`}>
        <div className="fluent-wire-cube">
          <div className="cube-face cube-face-front" />
          <div className="cube-face cube-face-back" />
          <div className="cube-face cube-face-right" />
          <div className="cube-face cube-face-left" />
          <div className="cube-face cube-face-top" />
          <div className="cube-face cube-face-bottom" />
          <div className="fluent-cube-core"><strong>Skill</strong><small>WORKFLOW CORE</small></div>
        </div>
      </div>
    </Link>
    <div className={`go-burst ${burstKey ? "active" : ""}`} key={`burst-${burstKey}`} aria-hidden="true">
      <span className="go-burst-word">GO</span>
      <i className="go-shock go-shock-one" />
      <i className="go-shock go-shock-two" />
      <span className="go-streaks">{Array.from({ length: 6 }, (_, index) => <i key={index} />)}</span>
      <span className="go-sparks">{Array.from({ length: 10 }, (_, index) => <i key={index} />)}</span>
    </div>
    <i className="cube-particle cube-particle-one" />
    <i className="cube-particle cube-particle-two" />
    <i className="cube-particle cube-particle-three" />
  </div>;
}

export function MarketplacePage() {
  const pageRef = useRef<HTMLDivElement>(null);
  const [category, setCategory] = useState("");
  const params = new URLSearchParams();
  if (category) params.set("category", category);
  const path = `/community/skills${params.size ? `?${params.toString()}` : ""}`;
  const { data: skills, loading } = useLoad<Skill[]>(path, []);

  useGSAP(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const entrance = gsap.timeline({ defaults: { duration: 0.48, ease: "power3.out" } });
    entrance
      .from(".public-header", { y: -12, autoAlpha: 0, duration: 0.36 }, 0)
      .from(".hero-copy h1 > span", { y: 28, autoAlpha: 0, stagger: 0.07 }, 0.08)
      .from(".hero-visual", { x: 24, scale: 0.985, autoAlpha: 0, duration: 0.68 }, 0.12)
      .from(".hero-capability-step", { y: 15, autoAlpha: 0, stagger: 0.06 }, 0.26)
      .from(".hero-description", { y: 12, autoAlpha: 0 }, 0.35);
    gsap.to(".market-ambient-orb-one", { x: 70, y: 34, scale: 1.08, duration: 13, repeat: -1, yoyo: true, ease: "sine.inOut" });
    gsap.to(".market-ambient-orb-two", { x: -54, y: -26, scale: 1.12, duration: 16, repeat: -1, yoyo: true, ease: "sine.inOut" });
  }, { scope: pageRef });

  useGSAP(() => {
    if (loading || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const cards = pageRef.current
      ? Array.from(pageRef.current.querySelectorAll<HTMLElement>(".market-section .skill-card"))
      : [];
    if (!cards.length) return;
    gsap.fromTo(cards, { y: 20, autoAlpha: 0 }, {
      y: 0,
      autoAlpha: 1,
      duration: 0.48,
      ease: "power2.out",
      stagger: 0.07,
      clearProps: "transform,opacity,visibility",
    });
  }, { scope: pageRef, dependencies: [loading, skills.length], revertOnUpdate: true });

  return (
    <div className="public-page home-marketplace" ref={pageRef}>
      <div className="market-ambient" aria-hidden="true"><i className="market-ambient-orb market-ambient-orb-one" /><i className="market-ambient-orb market-ambient-orb-two" /><i className="market-ambient-grid" /></div>
      <PublicHeader />
      <section className="hero">
        <div className="hero-copy">
          <h1><span>把好用的 Skill，</span><span><em>变成每个人</em>的能力。</span></h1>
          <div className="hero-capability-path" aria-label="SkillGo 核心能力">
            <article className="hero-capability-step"><span>01</span><div><strong>社区管理</strong><small>发布 · 审核 · 分享</small></div></article>
            <article className="hero-capability-step"><span>02</span><div><strong>独立运行</strong><small>每个任务一套沙箱</small></div></article>
            <article className="hero-capability-step"><span>03</span><div><strong>连接业务</strong><small>网页运行 · API 调用</small></div></article>
          </div>
          <p className="hero-description">发布封装好的 Skill 到社区，在独立沙箱中运行，并且支持外部 API 调用。</p>
        </div>
        <InteractiveSkillCore />
      </section>
      <section className="market-section">
        <div className="section-heading"><div><span className="eyebrow">COMMUNITY</span><h2>社区精选 Skill</h2></div><label className="filter-button"><Filter size={16} /><select aria-label="按分类筛选" value={category} onChange={(event) => setCategory(event.target.value)}><option value="">全部分类</option><option value="productivity">效率工具</option><option value="writing">内容写作</option><option value="document">文档处理</option><option value="development">研发工程</option><option value="data">数据分析</option><option value="other">其他</option></select></label></div>
        {loading ? <div className="card-grid loading-grid"><i /><i /><i /></div> : skills.length ? (
          <div className="card-grid">{skills.map((skill) => <SkillCard key={skill.id} skill={skill} />)}</div>
        ) : (
          <EmptyState icon={Boxes} title="社区正在等待第一个 Skill" description="登录后上传一个 Skill，提交审核并发布到社区。" action={<Link className="button primary" to="/register">成为首位创作者</Link>} />
        )}
      </section>
      <footer className="public-footer"><BrandFooter /><span>私有化 Skill 工作平台</span><div className="public-footer-tools"><details><summary aria-label="帮助与文档"><CircleHelp /></summary><div><a href="/api/docs"><BookOpen />API 文档</a></div></details><a href="https://github.com/Max-cu/SkillGo" target="_blank" rel="noreferrer" aria-label="在 GitHub 查看 SkillGo"><Github /></a></div></footer>
    </div>
  );
}

function BrandFooter() { return <strong className="footer-brand"><span><img src="/skillgo-logo.png" alt="" /></span> SkillGo</strong>; }

export function SkillDetailPage() {
  const { slug } = useParams();
  const { data: skill, setData, loading, error } = useLoad<Skill | null>(`/community/skills/${slug}`, null);
  const { user } = useAuth();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<"about" | "versions" | "permissions">("about");
  const [actionBusy, setActionBusy] = useState<"download" | "favorite" | "">("");
  const [actionMessage, setActionMessage] = useState("");
  if (loading) return <div className="public-page"><PublicHeader /><div className="detail-loading" /></div>;
  if (error || !skill) return <div className="public-page"><PublicHeader /><EmptyState title="没有找到这个 Skill" description="它可能仍是私有版本，或已被作者撤回。" /></div>;
  const currentSkill = skill;
  const version = currentSkill.versions?.[currentSkill.versions.length - 1];

  async function downloadPackage() {
    if (!user) { navigate("/login"); return; }
    if (!version) return;
    setActionBusy("download"); setActionMessage("");
    try {
      const blob = await apiBlob(`/skills/${currentSkill.id}/versions/${version.id}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${currentSkill.slug}-${version.version}.zip`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setActionMessage("ZIP 已开始下载");
    } catch (reason) { setActionMessage(reason instanceof Error ? reason.message : "下载失败"); }
    finally { setActionBusy(""); }
  }

  async function favorite() {
    if (!user) { navigate("/login"); return; }
    setActionBusy("favorite"); setActionMessage("");
    try {
      const result = await api<{ message: string }>(`/skills/${currentSkill.id}/favorite`, { method: "POST" });
      if (result.message === "Skill saved") setData({ ...currentSkill, favorite_count: currentSkill.favorite_count + 1 });
      setActionMessage(result.message === "Skill saved" ? "已收藏到你的账号" : "你已经收藏过这个 Skill");
    } catch (reason) { setActionMessage(reason instanceof Error ? reason.message : "收藏失败"); }
    finally { setActionBusy(""); }
  }

  const permissionEntries = Object.entries(version?.requested_permissions || {});
  return <div className="public-page"><PublicHeader /><main className="skill-detail">
    <div className="detail-main">
      <div className="detail-breadcrumb"><Link to="/">Skill 市场</Link><ChevronRight size={15} /><span>{categoryLabel(skill.category)}</span></div>
      <div className="detail-title"><span className="skill-icon large"><Workflow /></span><div><span className="category-pill">{categoryLabel(skill.category)}</span><h1>{skill.name}</h1><p>{skill.summary}</p></div></div>
      <div className="detail-tabs" role="tablist" aria-label="Skill 详情"><button className={activeTab === "about" ? "active" : ""} aria-selected={activeTab === "about"} onClick={() => setActiveTab("about")}>说明</button><button className={activeTab === "versions" ? "active" : ""} aria-selected={activeTab === "versions"} onClick={() => setActiveTab("versions")}>版本 {skill.versions?.length || 0}</button><button className={activeTab === "permissions" ? "active" : ""} aria-selected={activeTab === "permissions"} onClick={() => setActiveTab("permissions")}>权限</button></div>
      {activeTab === "about" && <section className="readme"><h2>关于这个 Skill</h2><p>{skill.description || "作者暂未添加详细说明。"}</p><h3>工作流能力</h3><p>该 Skill 的每次执行都会锁定版本和权限快照，并生成可追溯的运行记录。</p></section>}
      {activeTab === "versions" && <section className="public-version-list">{[...(skill.versions || [])].reverse().map((item) => <article key={item.id}><div><strong>v{item.version}</strong><span>{skillTypeLabels[item.skill_type]}</span></div><StatusBadge status={item.status} /><code>{item.package_sha256.slice(0, 16)}…</code><time>{new Date(item.created_at).toLocaleDateString("zh-CN")}</time></article>)}</section>}
      {activeTab === "permissions" && <section className="permission-panel"><div className="security-note"><ShieldCheck /><span>此版本声明的权限在发布前已记录并审核</span></div>{permissionEntries.length ? <dl>{permissionEntries.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{Array.isArray(value) ? value.length ? value.join("、") : "无" : JSON.stringify(value)}</dd></div>)}</dl> : <p>此版本未申请额外权限。</p>}</section>}
    </div>
    <aside className="install-card"><div className="install-version"><span>当前版本</span><strong>v{version?.version || skill.latest_version}</strong></div><button className="button primary full" disabled={Boolean(actionBusy)} onClick={downloadPackage}><Download size={17} />{actionBusy === "download" ? "正在准备下载…" : user ? "下载 Skill ZIP" : "登录后下载"}</button><button className="button secondary full favorite-action" disabled={Boolean(actionBusy)} onClick={favorite}><Heart size={17} />{actionBusy === "favorite" ? "正在收藏…" : "收藏 Skill"}</button>{actionMessage && <div className="inline-message" aria-live="polite">{actionMessage}</div>}<dl><div><dt>作者</dt><dd>{skill.owner_name}</dd></div><div><dt>类型</dt><dd>{version ? skillTypeLabels[version.skill_type] : "—"}</dd></div><div><dt>收藏</dt><dd>{skill.favorite_count}</dd></div><div><dt>状态</dt><dd><StatusBadge status={version?.status || skill.latest_status} /></dd></div></dl><div className="security-note"><ShieldCheck /><span>版本内容和权限声明可追溯，发布后不可替换</span></div></aside>
  </main></div>;
}

export function AuthPage({ mode }: { mode: "login" | "register" }) {
  const { user, login, register } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingAdmin, setPendingAdmin] = useState("");
  if (user) return <Navigate to="/app" replace />;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const form = new FormData(event.currentTarget);
    try {
      if (mode === "login") {
        await login(String(form.get("email")), String(form.get("password")));
        navigate("/app");
      } else {
        const identity = form.get("identity") === "admin" ? "admin" : "member";
        const result = await register(String(form.get("email")), String(form.get("display_name")), String(form.get("password")), identity);
        if (result.status === "pending_approval") setPendingAdmin(result.user.email);
        else navigate("/app");
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  }
  return <div className="auth-page">
    <div className="auth-aside">
      <div className="auth-entry">
        <Link to="/" className="auth-brand"><span><img src="/skillgo-logo.png" alt="" /></span>SkillGo</Link>
        <Link to="/" className="auth-return"><ArrowLeft />返回 Skill 市场</Link>
      </div>
      <div><span className="eyebrow">SKILL WORKFLOW PLATFORM</span><h1>{mode === "login" ? <>欢迎回来，<br /><span className="auth-title-nowrap">继续创造。</span></> : "把你的方法，变成可复用的 Skill。"}</h1><p>上传、分享、运行，并将完整流程发布为 API。</p></div>
      <div className="auth-proof"><ShieldCheck /><span>三级权限 · 版本审核 · 完整审计</span></div>
    </div>
    <div className="auth-form-wrap">{pendingAdmin ? <section className="registration-pending" aria-live="polite"><span><ShieldCheck /></span><div><span className="eyebrow">APPLICATION RECEIVED</span><h2>管理员申请已提交</h2><p><strong>{pendingAdmin}</strong> 已进入审核队列。超级管理员通过后，你就可以使用这个邮箱登录。</p></div><Link className="button primary full" to="/login">返回登录<ArrowRight size={17} /></Link><Link className="text-link" to="/">先看看 Skill 市场</Link></section> : <form className="auth-form" onSubmit={submit}><div><span className="eyebrow">{mode === "login" ? "SIGN IN" : "GET STARTED"}</span><h2>{mode === "login" ? "登录 SkillGo" : "创建账号"}</h2><p>{mode === "login" ? "使用你的平台账号进入工作台。" : "选择加入身份；成员立即可用，管理员需要审核。"}</p></div>{mode === "register" && <><label>显示名称<input name="display_name" minLength={2} required placeholder="你的名字或团队名" /></label><fieldset className="identity-picker"><legend>身份</legend><label><input type="radio" name="identity" value="member" defaultChecked /><span><strong>成员</strong><small>注册后直接进入工作台</small></span></label><label><input type="radio" name="identity" value="admin" /><span><strong>管理员</strong><small>提交后等待超级管理员审核</small></span></label></fieldset></>}<label>邮箱<input name="email" type="email" required placeholder="name@example.com" /></label><label>密码<input name="password" type="password" minLength={10} required placeholder="至少 10 个字符" /></label>{error && <div className="form-error">{error}</div>}<button className="button primary full" disabled={busy}>{busy ? "请稍候…" : mode === "login" ? "登录" : "创建账号"}<ArrowRight size={17} /></button><p className="form-switch">{mode === "login" ? "还没有账号？" : "已经有账号？"}<Link to={mode === "login" ? "/register" : "/login"}>{mode === "login" ? "立即注册" : "返回登录"}</Link></p></form>}</div>
  </div>;
}

export function MySkillsPage() {
  const { data: skills, loading } = useLoad<Skill[]>("/skills/mine", []);
  return <><Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "我的 Skill" }]} /><div className="workspace-section-head"><h1>我的 Skill</h1><Link className="button primary compact" to="/app/skills/new"><UploadCloud size={16} />上传 Skill</Link></div>{loading ? <div className="workspace-grid compact loading-grid"><i /><i /><i /></div> : skills.length ? <div className="workspace-grid compact">{skills.map((skill) => <TiltSurface className="workspace-skill-tilt" key={skill.id}><Link className="workspace-skill" to={`/app/skills/${skill.id}`}><div className="workspace-skill-head"><span className="skill-icon"><Workflow /></span><StatusBadge status={skill.latest_status} /></div><h3>{skill.name}</h3><p>{skill.summary}</p><footer><span>{visibilityLabels[skill.visibility]}</span><span>{skill.latest_version ? `v${skill.latest_version}` : "等待上传版本"}</span></footer></Link></TiltSurface>)}</div> : <EmptyState title="开始创建你的 Skill" description="选择一个 Skill ZIP，平台会解析内容并自动生成社区资料。" action={<Link className="button primary" to="/app/skills/new">上传 Skill</Link>} />}</>;
}

export function NewSkillPage() {
  const navigate = useNavigate();
  const analysisRequest = useRef(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [analysis, setAnalysis] = useState<SkillPackageAnalysis | null>(null);
  const [packageFile, setPackageFile] = useState<File | null>(null);
  const [draft, setDraft] = useState({
    name: "",
    slug: "",
    summary: "",
    description: "",
    category: "productivity",
    visibility: "private" as Visibility
  });

  function updateDraft(field: keyof typeof draft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  async function selectPackage(file: File | null) {
    const requestId = ++analysisRequest.current;
    setPackageFile(file);
    setAnalysis(null);
    setError("");
    if (!file) {
      setAnalysisBusy(false);
      return;
    }
    setAnalysisBusy(true);
    const body = new FormData();
    body.set("package", file);
    try {
      const result = await api<SkillPackageAnalysis>("/skills/analyze-package", { method: "POST", body });
      if (analysisRequest.current !== requestId) return;
      setAnalysis(result);
      setDraft((current) => ({
        ...current,
        name: result.name,
        slug: result.slug,
        summary: result.summary,
        description: result.description,
        category: result.category
      }));
    } catch (reason) {
      if (analysisRequest.current !== requestId) return;
      setError(reason instanceof Error ? `无法识别这个 ZIP：${reason.message}` : "无法识别这个 ZIP");
    } finally {
      if (analysisRequest.current === requestId) setAnalysisBusy(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const skill = await api<Skill>("/skills", { method: "POST", body: JSON.stringify({ ...draft, icon: "sparkles" }) });
      if (packageFile) {
        const upload = new FormData(); upload.set("package", packageFile);
        try { await api(`/skills/${skill.id}/versions`, { method: "POST", body: upload }); }
        catch (reason) {
          const uploadError = reason instanceof Error ? reason.message : "版本上传失败";
          navigate(`/app/skills/${skill.id}?upload_error=${encodeURIComponent(`Skill 已创建，但 ZIP 上传失败：${uploadError}`)}`);
          return;
        }
      }
      navigate(`/app/skills/${skill.id}`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); }
    finally { setBusy(false); }
  }
  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "我的 Skill", to: "/app/skills" }, { label: "创建 Skill" }]} />
    <PageTitle eyebrow="NEW SKILL" title="上传 Skill" description="先选择 ZIP，平台会理解 Skill 内容并自动填写资料，你只需要确认。" />
    <form className="editor-form" onSubmit={submit}>
      <section className="form-section upload-first-section">
        <div><span className="form-step">01</span><h2>选择 Skill 包</h2><p>支持标准 Agent Skill ZIP，也支持包含 SkillGo 运行配置的扩展包。</p></div>
        <div>
          <SkillPackagePicker file={packageFile} onChange={selectPackage} busy={analysisBusy || busy} busyLabel={analysisBusy ? "AI 正在理解这个 Skill…" : "正在创建并上传…"} />
          {analysis && <div className={`package-analysis ${analysis.source}`} aria-live="polite">
            <span>{analysis.source === "ai" ? <Sparkles /> : <FileCheck2 />}</span>
            <div><strong>{analysis.source === "ai" ? "AI 已完成资料预填" : "已从 Skill 包提取资料"}</strong><p>{analysis.package_format === "agent-skill" ? "标准 Agent Skill" : "SkillGo 扩展包"} · v{analysis.version}{analysis.model_name ? ` · ${analysis.model_name}` : ""}</p></div>
            <Check />
            {analysis.warnings.map((warning) => <small key={warning}>{warning}</small>)}
          </div>}
        </div>
      </section>
      <section className="form-section">
        <div><span className="form-step">02</span><h2>确认基本信息</h2><p>AI 生成的内容只是建议，发布前你可以修改任何字段。</p></div>
        <div className="form-fields">
          <label>Skill 名称<input name="name" required minLength={2} value={draft.name} onChange={(event) => updateDraft("name", event.target.value)} placeholder="选择 ZIP 后自动生成" /></label>
          <label>唯一标识<input name="slug" required pattern="[a-z0-9][a-z0-9-]*[a-z0-9]" value={draft.slug} onChange={(event) => updateDraft("slug", event.target.value)} placeholder="skill-name" /></label>
          <label className="wide">一句话简介<textarea name="summary" required minLength={10} maxLength={280} rows={2} value={draft.summary} onChange={(event) => updateDraft("summary", event.target.value)} placeholder="选择 ZIP 后由 AI 总结它解决的问题和输出。" /></label>
          <label>分类<select name="category" value={draft.category} onChange={(event) => updateDraft("category", event.target.value)}><option value="productivity">效率工具</option><option value="writing">内容写作</option><option value="document">文档处理</option><option value="development">研发工程</option><option value="data">数据分析</option><option value="other">其他</option></select></label>
          <label>可见性<select name="visibility" value={draft.visibility} onChange={(event) => updateDraft("visibility", event.target.value)}><option value="private">私有</option><option value="unlisted">链接分享</option><option value="internal">实例内部</option><option value="public">公开社区</option></select></label>
          <label className="wide">详细说明<textarea name="description" rows={5} value={draft.description} onChange={(event) => updateDraft("description", event.target.value)} placeholder="AI 会补充适用场景、执行流程和使用限制。" /></label>
        </div>
      </section>
      {error && <div className="form-error">{error}</div>}
      <div className="form-actions"><Link className="button ghost" to="/app/skills">取消</Link><button className="button primary" disabled={busy || analysisBusy}>{busy ? packageFile ? "正在创建并上传…" : "正在创建…" : packageFile ? "确认并上传" : "仅保存 Skill"}<ArrowRight size={17} /></button></div>
    </form>
  </>;
}

export function ManageSkillPage() {
  const { skillId } = useParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const { data: skill, setData, loading, error } = useLoad<Skill | null>(`/skills/${skillId}`, null);
  const [message, setMessage] = useState(() => new URLSearchParams(window.location.search).get("upload_error") || "");
  const [endpointSecret, setEndpointSecret] = useState<EndpointCreated | null>(null);
  const [packageFile, setPackageFile] = useState<File | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [visibilityTarget, setVisibilityTarget] = useState<Visibility | null>(null);
  const [visibilityBusy, setVisibilityBusy] = useState(false);
  const [visibilityError, setVisibilityError] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  if (loading) return <div className="detail-loading" />;
  if (error || !skill) return <EmptyState title="没有找到这个 Skill" description="你可能没有管理权限。" />;
  const currentSkill = skill;
  async function submitVersion(version: SkillVersion) { const updated = await api<SkillVersion>(`/skills/${currentSkill.id}/versions/${version.id}/submit`, { method: "POST" }); setData({ ...currentSkill, versions: currentSkill.versions?.map((item) => item.id === updated.id ? updated : item) }); setMessage("版本已提交审核"); }
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!packageFile) { setMessage("请先选择一个 ZIP 包"); return; }
    const formElement = event.currentTarget;
    const form = new FormData();
    form.set("package", packageFile);
    setUploadBusy(true); setMessage("");
    try {
      const version = await api<SkillVersion>(`/skills/${currentSkill.id}/versions`, { method: "POST", body: form });
      setData({ ...currentSkill, versions: [...(currentSkill.versions || []), version], latest_version: version.version, latest_status: version.status });
      setMessage("版本已上传并通过基础校验");
      setPackageFile(null);
      formElement.reset();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "上传失败"); }
    finally { setUploadBusy(false); }
  }
  async function deploy(version: SkillVersion) {
    try {
      const deployed = await api<EndpointCreated>("/endpoints", {
        method: "POST",
        body: JSON.stringify({
          version_id: version.id,
          slug: `${currentSkill.slug}-${version.version.replaceAll(".", "-")}`,
          name: `${currentSkill.name} v${version.version}`,
        }),
      });
      setEndpointSecret(deployed);
      setMessage("Endpoint 已部署。请立即保存 API Key，它不会再次完整显示。");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "部署失败");
    }
  }
  async function confirmVisibility() {
    if (!visibilityTarget) return;
    setVisibilityBusy(true); setVisibilityError(""); setMessage("");
    try {
      const updated = await api<Skill>(`/skills/${currentSkill.id}/visibility`, { method: "PATCH", body: JSON.stringify({ visibility: visibilityTarget }) });
      setData({ ...currentSkill, ...updated, versions: currentSkill.versions });
      setVisibilityTarget(null);
      setMessage(updated.visibility === "public" ? "已发布到社区，首页现在可以看到这个 Skill。" : "已从社区下架，Skill 已恢复为私有。");
    } catch (reason) {
      setVisibilityError(reason instanceof Error ? reason.message : "可见性修改失败");
    } finally { setVisibilityBusy(false); }
  }
  async function confirmDelete() {
    setDeleteBusy(true); setDeleteError("");
    try {
      await api(`/skills/${currentSkill.id}`, { method: "DELETE" });
      navigate("/app/skills");
    } catch (reason) {
      setDeleteError(reason instanceof Error ? reason.message : "删除失败");
      setDeleteBusy(false);
    }
  }
  const canDelete = user?.id === currentSkill.owner_id || user?.role === "super_admin";
  const hasPublishedVersion = Boolean(currentSkill.versions?.some((version) => version.status === "published"));
  const isCommunityPublic = currentSkill.visibility === "public";
  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "我的 Skill", to: "/app/skills" }, { label: currentSkill.name }]} />
    <PageTitle eyebrow="SKILL DETAIL" title={skill.name} description={skill.summary} action={<span className={`visibility-pill ${isCommunityPublic ? "public" : ""}`}>{isCommunityPublic ? <CloudUpload size={15} /> : <LockKeyhole size={15} />}{visibilityLabels[skill.visibility]}</span>} />
    <div className="manage-layout">
      <section className="panel">
        <div className="panel-heading"><div><h2>版本</h2><p>平台会根据包内脚本、依赖和权限识别真实运行方式。</p></div></div>
        {skill.versions?.length ? <div className="version-list">{[...skill.versions].reverse().map((version) => <article key={version.id}>
          <div><strong>v{version.version}</strong><span>{skillTypeLabels[version.skill_type]}</span><span className={`runtime-mode ${version.execution_mode}`}>{executionModeLabels[version.execution_mode] || version.execution_mode}</span><code>{version.package_sha256.slice(0, 12)}</code></div>
          <div className="version-actions"><StatusBadge status={version.status} />{(version.status === "ready" || version.status === "rejected") && <button className="button secondary compact" onClick={() => submitVersion(version)}>提交审核</button>}<Link className="button primary compact" to={`/app/skills/${currentSkill.id}/workflow?version=${version.id}`}><Play size={15} />用 Agent 运行</Link>{version.execution_mode === "instruction_only" && <Link className="button secondary compact" to={`/app/skills/${currentSkill.id}/run?version=${version.id}`}><MessageSquareText size={15} />对话调试</Link>}{version.status === "published" && version.runtime_runnable && ["instruction_only", "sandbox_required"].includes(version.execution_mode) && <button className="button secondary compact" onClick={() => deploy(version)}><Zap size={15} />发布为 API</button>}</div>
          {version.runtime_block_reason && <p className="runtime-warning"><AlertTriangle />{version.runtime_block_reason}</p>}{version.review_note && <p className="review-note">审核意见：{version.review_note}</p>}
        </article>)}</div> : <EmptyState title="还没有版本" description="上传一个符合规范的 ZIP 包开始。" />}
      </section>
      <aside className="panel upload-panel">
        <section className={`community-publish-card ${isCommunityPublic ? "published" : ""}`}><div className="community-publish-icon">{isCommunityPublic ? <Check /> : <CloudUpload />}</div><div><span className="eyebrow">COMMUNITY</span><h2>{isCommunityPublic ? "已在社区展示" : "发布到社区"}</h2><p>{isCommunityPublic ? "首页访客可以发现、查看并下载已审核版本。" : hasPublishedVersion ? "这个 Skill 已有审核通过的版本，可以立即公开展示。" : "版本审核通过后，才可以安全地发布到公开社区。"}</p></div><button className={`button full ${isCommunityPublic ? "secondary" : "primary"}`} type="button" disabled={visibilityBusy || (!isCommunityPublic && !hasPublishedVersion)} onClick={() => { setVisibilityError(""); setVisibilityTarget(isCommunityPublic ? "private" : "public"); }}>{isCommunityPublic ? <LockKeyhole size={15} /> : <CloudUpload size={15} />}{isCommunityPublic ? "从社区下架" : hasPublishedVersion ? "发布到社区" : "等待版本审核"}</button></section>
        {endpointSecret && <div className="secret-reveal"><span className="eyebrow">仅显示一次</span><h3>{endpointSecret.name}</h3><p>调用地址 <code>{endpointInvokePath(endpointSecret)}</code></p><div><code>{endpointSecret.api_key}</code><button type="button" title="复制 API Key" onClick={() => navigator.clipboard.writeText(endpointSecret.api_key)}><Copy size={16} /></button></div></div>}
        <h2>上传新版本</h2><p>标准包由平台递增版本；SkillGo 扩展包使用声明的语义化版本。</p><form onSubmit={upload}><SkillPackagePicker compact required file={packageFile} busy={uploadBusy} onChange={(file) => { setPackageFile(file); setMessage(""); }} /><button className="button primary full" type="submit" disabled={!packageFile || uploadBusy}>{uploadBusy ? "正在上传并校验…" : packageFile ? "上传并校验" : "请先选择 ZIP"}</button></form>{message && <div className="inline-message" aria-live="polite">{message}</div>}{canDelete && <div className="danger-zone"><h3>删除 Skill</h3><p>同时移除版本包、工作流任务、Endpoint 和运行记录。</p><button className="button danger full" type="button" onClick={() => setDeleteOpen(true)}><Trash2 size={16} />删除这个 Skill</button></div>}
      </aside>
    </div>
    {visibilityTarget && <div className="modal-backdrop" role="presentation"><section className={`confirm-dialog community-visibility-dialog ${visibilityTarget === "public" ? "publish" : "unpublish"}`} role="alertdialog" aria-modal="true" aria-labelledby="community-visibility-title"><span className="confirm-icon">{visibilityTarget === "public" ? <CloudUpload /> : <LockKeyhole />}</span><h2 id="community-visibility-title">{visibilityTarget === "public" ? "确认发布到社区？" : "确认从社区下架？"}</h2><p>{visibilityTarget === "public" ? "公开后，所有访客都可以在首页发现这个 Skill，并查看和下载已经审核通过的版本。未发布版本仍然不会公开。" : "下架后，首页和社区详情将立即隐藏；现有版本、任务和 API 不会被删除。"}</p>{visibilityError && <div className="form-error" aria-live="polite">{visibilityError}</div>}<div><button className="button ghost" type="button" disabled={visibilityBusy} onClick={() => { setVisibilityTarget(null); setVisibilityError(""); }}>取消</button><button className={`button ${visibilityTarget === "public" ? "primary" : "danger"}`} type="button" disabled={visibilityBusy} onClick={() => void confirmVisibility()}>{visibilityTarget === "public" ? <CloudUpload size={16} /> : <LockKeyhole size={16} />}{visibilityBusy ? "正在更新…" : visibilityTarget === "public" ? "确认公开" : "确认下架"}</button></div></section></div>}
    {deleteOpen && <div className="modal-backdrop" role="presentation"><section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-skill-title"><span className="confirm-icon"><AlertTriangle /></span><h2 id="delete-skill-title">确认删除“{currentSkill.name}”？</h2><p>这个操作无法撤销。所有版本包、已部署 API Endpoint 和历史运行记录都会一起删除。</p>{deleteError && <div className="form-error" aria-live="polite">{deleteError}</div>}<div><button className="button ghost" type="button" disabled={deleteBusy} onClick={() => { setDeleteOpen(false); setDeleteError(""); }}>取消</button><button className="button danger" type="button" disabled={deleteBusy} onClick={confirmDelete}><Trash2 size={16} />{deleteBusy ? "正在删除…" : "确认永久删除"}</button></div></section></div>}
  </>;
}

export function WorkflowJobsPage() {
  const { data: jobs, loading } = useLoad<WorkflowJob[]>("/jobs?limit=100", []);
  const { data: serviceCalls, loading: serviceCallsLoading } = useLoad<SkillRun[]>("/runs?limit=50", []);
  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "任务" }]} />
    <PageTitle eyebrow="TASK CENTER" title="任务" description="集中查看 Skill 任务、API 调用、执行状态和真实产物；底层运行细节仅在需要时展开。" action={<Link className="button primary" to="/app"><Plus size={16} />开始新任务</Link>} />
    <details className="task-center-section collapsible-section">
      <summary><div><h2>Skill 任务</h2><p>从对话发起的沙箱任务</p></div><span>{jobs.length}</span><ChevronDown /></summary>
      <div className="task-center-body">{loading ? <div className="detail-loading" /> : jobs.length ? <div className="workflow-job-list">{jobs.map((job) => <Link key={job.id} to={`/app/skills/${job.skill_id}/workflow?version=${job.skill_version_id}&job=${job.id}`}><span className={`workflow-job-icon ${job.status}`}><Workflow /></span><div><strong>{workflowJobSkillLabel(job)}</strong><p>{job.instruction.slice(0, 48) || job.input_files[0]?.filename || "Skill 任务"} · {workflowJobSkills(job).map((item) => `v${item.version}`).join(" + ")}</p></div><span className={`workflow-status ${job.status}`}>{workflowStatusLabels[job.status]}</span><time>{new Date(job.created_at).toLocaleString("zh-CN")}</time><ChevronRight /></Link>)}</div> : <EmptyState title="还没有 Skill 任务" description="在工作台对话中插入 Skill，发送后任务会出现在这里。" action={<Link className="button primary" to="/app">开始任务</Link>} />}</div>
    </details>
    <details className="task-center-section collapsible-section service-calls">
      <summary><div><h2>服务调用</h2><p>网页运行与 API Endpoint 调用</p></div><span>{serviceCalls.length}</span><ChevronDown /></summary>
      <div className="task-center-body">{serviceCallsLoading ? <div className="detail-loading compact" /> : serviceCalls.length ? <div className="runs-list">{serviceCalls.map((run) => <article className="panel run-row" key={run.id}><div className="run-row-main"><span className="skill-icon small"><Zap /></span><div><strong>{run.skill_name} <small>v{run.version}</small></strong><span>{run.invocation_type === "api" ? `API 调用${run.endpoint_slug ? ` · ${run.endpoint_slug}` : ""}` : "网页快捷运行"}</span></div></div><RunBadge status={run.status} /><div className="run-row-meta"><span>{new Date(run.created_at).toLocaleString("zh-CN")}</span><b>{run.latency_ms === null ? "—" : `${run.latency_ms}ms`}</b></div>{run.error_message && <p className="review-note">{run.error_code}：{run.error_message}</p>}</article>)}</div> : <EmptyState title="还没有服务调用" description="网页快捷运行或 API 调用后，记录会出现在这里。" />}</div>
    </details>
  </>;
}

export function LegacyWorkflowPage() {
  const { skillId } = useParams();
  const { data: skill, loading } = useLoad<Skill | null>(`/skills/${skillId}`, null);
  const { data: jobs, setData: setJobs } = useLoad<WorkflowJob[]>(`/jobs?skill_id=${encodeURIComponent(skillId || "")}`, []);
  const requestedVersion = new URLSearchParams(window.location.search).get("version");
  const requestedJob = new URLSearchParams(window.location.search).get("job");
  const requestedPrompt = new URLSearchParams(window.location.search).get("prompt") || "";
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [instruction, setInstruction] = useState(requestedPrompt);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!skill || selectedVersionId) return;
    const versions = skill.versions || [];
    const initial = versions.find((item) => item.id === requestedVersion) || versions[versions.length - 1];
    if (initial) setSelectedVersionId(initial.id);
  }, [requestedVersion, selectedVersionId, skill]);

  useEffect(() => {
    if (selectedJobId || !jobs.length) return;
    const initial = jobs.find((item) => item.id === requestedJob) || jobs[0];
    setSelectedJobId(initial.id);
  }, [jobs, requestedJob, selectedJobId]);

  const polledJob = jobs.find((item) => item.id === selectedJobId);
  useEffect(() => {
    if (!polledJob || ["succeeded", "failed", "cancelled", "blocked"].includes(polledJob.status)) return;
    let cancelled = false;
    async function refresh() {
      try {
        const updated = await api<WorkflowJob>(`/jobs/${polledJob!.id}`);
        if (!cancelled) setJobs((current) => current.map((item) => item.id === updated.id ? updated : item));
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "任务状态刷新失败");
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1400);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [polledJob?.id, polledJob?.status, setJobs]);

  if (loading) return <div className="detail-loading" />;
  if (!skill) return <EmptyState title="没有找到这个 Skill" description="你可能没有运行权限。" />;
  const versions = skill.versions || [];
  const selectedVersion = versions.find((item) => item.id === selectedVersionId) || versions[versions.length - 1];
  if (!selectedVersion) return <EmptyState title="暂无可运行版本" description="请先上传并校验一个 Skill 版本。" />;
  const selectedJob = jobs.find((item) => item.id === selectedJobId) || null;
  const requirements = selectedVersion.runtime_requirements;

  async function startFromFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || busy || !selectedVersion.runtime_runnable) return;
    setBusy(true); setError("");
    const body = new FormData();
    body.set("version_id", selectedVersion.id);
    body.set("instruction", instruction);
    body.set("file", file);
    try {
      const job = await api<WorkflowJob>("/jobs", { method: "POST", body });
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setSelectedJobId(job.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "工作流启动失败");
    } finally { setBusy(false); }
  }

  async function downloadArtifact(job: WorkflowJob, artifact: WorkflowArtifact) {
    try {
      const blob = await apiBlob(`/jobs/${job.id}/artifacts/${artifact.id}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = artifact.filename; anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "产物下载失败"); }
  }

  function changeVersion(versionId: string) {
    setSelectedVersionId(versionId);
    const related = jobs.find((item) => item.skill_version_id === versionId);
    setSelectedJobId(related?.id || "");
    setError("");
  }

  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "我的 Skill", to: "/app/skills" }, { label: skill.name, to: `/app/skills/${skill.id}` }, { label: "运行工作流" }]} />
    <PageTitle eyebrow="WORKFLOW RUNNER" title={skill.name} description="文件满足输入条件后自动创建任务，工作流会持续运行到完成、失败或确实需要你补充信息。" action={<Link className="button ghost" to={`/app/skills/${skill.id}`}><ArrowLeft size={16} />返回 Skill 详情</Link>} />
    <div className="workflow-run-layout">
      <aside className="workflow-launch panel">
        <label>Skill 版本<select value={selectedVersion.id} disabled={busy} onChange={(event) => changeVersion(event.target.value)}>{[...versions].reverse().map((version) => <option key={version.id} value={version.id}>v{version.version} · {executionModeLabels[version.execution_mode] || version.execution_mode}</option>)}</select></label>
        <div className={`runtime-card ${selectedVersion.runtime_runnable ? "available" : "blocked"}`}>
          <span>{selectedVersion.runtime_runnable ? <ShieldCheck /> : <AlertTriangle />}</span>
          <div><strong>{selectedVersion.runtime_runnable ? "当前环境可以运行" : "当前环境暂不可运行"}</strong><p>{executionModeLabels[selectedVersion.execution_mode] || selectedVersion.execution_mode}</p></div>
        </div>
        {selectedVersion.runtime_reasons.length > 0 && <ul className="runtime-reason-list">{selectedVersion.runtime_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
        {(requirements.runtimes?.length || requirements.scripts?.length || requirements.network) && <div className="runtime-requirements"><span>运行要求</span><div>{requirements.runtimes?.map((item) => <code key={item}>{item}</code>)}{requirements.network && <code>受控网络</code>}{requirements.scripts?.slice(0, 3).map((item) => <code key={item}>{item.split("/").pop()}</code>)}</div></div>}
        {selectedVersion.runtime_runnable ? <>
          <label className="workflow-instruction">补充要求（可选）<textarea rows={3} maxLength={20000} value={instruction} disabled={busy} onChange={(event) => setInstruction(event.target.value)} placeholder="例如：重点检查日期、金额和前后矛盾" /></label>
          <label className={`workflow-file-trigger ${busy ? "busy" : ""}`}>
            {busy ? <RotateCw className="spin-icon" /> : <UploadCloud />}
            <strong>{busy ? "工作流正在执行…" : "上传文件并自动开始"}</strong>
            <span>TXT、DOCX、XLSX、CSV、JSON，最大 10 MB</span>
            <input type="file" disabled={busy} accept=".txt,.md,.csv,.json,.yaml,.yml,.docx,.xlsx" onChange={startFromFile} />
          </label>
        </> : <div className="runtime-block-message"><strong>{selectedVersion.runtime_block_reason}</strong><p>平台不会再调用大模型假装执行。Linux Worker 接入后，这个版本无需重新上传。</p></div>}
        {error && <div className="form-error">{error}</div>}
      </aside>

      <section className="workflow-stage panel">
        <div className="panel-heading"><div><h2>任务进度</h2><p>只有步骤和产物真实完成，任务才会标记成功。</p></div>{selectedJob && <span className={`workflow-status ${selectedJob.status}`}>{workflowStatusLabels[selectedJob.status]}</span>}</div>
        {selectedJob ? <>
          <div className="workflow-job-meta"><span>任务 {selectedJob.id.slice(0, 8)}</span><span>v{selectedJob.version}</span><time>{new Date(selectedJob.created_at).toLocaleString("zh-CN")}</time></div>
          <div className="workflow-timeline">{selectedJob.steps.map((step) => <article className={step.status} key={step.id}><span>{step.status === "succeeded" ? <Check /> : step.status === "running" ? <RotateCw className="spin-icon" /> : step.status === "failed" || step.status === "blocked" ? <AlertTriangle /> : <i />}</span><div><strong>{step.name}</strong><p>{step.detail || (step.status === "pending" ? "等待前序步骤" : step.status)}</p></div></article>)}</div>
          {selectedJob.error_message && <div className="workflow-error"><AlertTriangle /><div><strong>{selectedJob.error_code}</strong><p>{selectedJob.error_message}</p></div></div>}
          {selectedJob.artifacts.length > 0 && <div className="workflow-artifacts"><h3>任务产物</h3>{selectedJob.artifacts.map((artifact) => <article key={artifact.id}><FileText /><div><strong>{artifact.filename}</strong><span>{formatPackageSize(artifact.size_bytes)} · {artifact.verified ? "完整性已校验" : "待校验"}</span></div><button className="button primary compact" onClick={() => void downloadArtifact(selectedJob, artifact)}><Download size={15} />下载</button></article>)}</div>}
        </> : <EmptyState title={selectedVersion.runtime_runnable ? "上传文件即可开始" : "等待运行环境"} description={selectedVersion.runtime_runnable ? "不需要再发送“看看”或“继续”，平台会自动执行到终态。" : selectedVersion.runtime_block_reason || "当前版本暂不可运行。"} />}
      </section>

      <aside className="workflow-history panel"><h2>历史任务</h2>{jobs.filter((item) => item.skill_version_id === selectedVersion.id).length ? <div>{jobs.filter((item) => item.skill_version_id === selectedVersion.id).map((job) => <button className={job.id === selectedJobId ? "active" : ""} key={job.id} onClick={() => setSelectedJobId(job.id)}><span><strong>{job.input_files[0]?.filename || "工作流任务"}</strong><small>{new Date(job.created_at).toLocaleString("zh-CN")}</small></span><i className={job.status}>{workflowStatusLabels[job.status]}</i></button>)}</div> : <p>这个版本还没有任务记录。</p>}</aside>
    </div>
  </>;
}

export function WorkflowPage() {
  const { skillId } = useParams();
  const { data: skill, loading } = useLoad<Skill | null>(`/skills/${skillId}`, null);
  const { data: jobs, setData: setJobs } = useLoad<WorkflowJob[]>(`/jobs?skill_id=${encodeURIComponent(skillId || "")}`, []);
  const { data: availableModels } = useLoad<AvailableModels>("/models/available", { configured: false, models: [], default_model: null });
  const query = new URLSearchParams(window.location.search);
  const requestedVersion = query.get("version");
  const requestedJob = query.get("job");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [creatingNewTask, setCreatingNewTask] = useState(false);
  const [messageText, setMessageText] = useState(query.get("prompt") || "");
  const [attachment, setAttachment] = useState<File | null>(null);
  const [selectedModelName, setSelectedModelName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<WorkflowJob | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messageInputRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesPanelRef = useRef<HTMLDivElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!skill || selectedVersionId) return;
    const versions = skill.versions || [];
    const initial = versions.find((item) => item.id === requestedVersion)
      || [...versions].reverse().find((item) => item.runtime_runnable)
      || versions[versions.length - 1];
    if (initial) setSelectedVersionId(initial.id);
  }, [requestedVersion, selectedVersionId, skill]);

  useEffect(() => {
    if (creatingNewTask || selectedJobId || !jobs.length) return;
    const initial = jobs.find((item) => item.id === requestedJob) || jobs[0];
    setSelectedJobId(initial.id);
  }, [creatingNewTask, jobs, requestedJob, selectedJobId]);

  useEffect(() => {
    if (!selectedModelName && availableModels.default_model) setSelectedModelName(availableModels.default_model);
  }, [availableModels.default_model, selectedModelName]);

  const selectedJob = jobs.find((item) => item.id === selectedJobId) || null;
  const jobActive = Boolean(selectedJob && !["succeeded", "failed", "cancelled", "blocked"].includes(selectedJob.status));
  useEffect(() => {
    if (!selectedJob || !jobActive) return;
    let cancelled = false;
    async function refresh() {
      try {
        const updated = await api<WorkflowJob>(`/jobs/${selectedJob!.id}`);
        if (!cancelled) setJobs((current) => current.map((item) => item.id === updated.id ? updated : item));
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "任务状态刷新失败");
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1100);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [jobActive, selectedJob?.id, setJobs]);

  useEffect(() => {
    messagesPanelRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, [selectedJob?.id]);

  useEffect(() => {
    if (jobActive) messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [jobActive, selectedJob?.events?.length]);

  if (loading) return <div className="detail-loading" />;
  if (!skill) return <EmptyState title="没有找到这个 Skill" description="你可能没有运行权限。" />;
  const versions = skill.versions || [];
  const selectedVersion = versions.find((item) => item.id === selectedVersionId) || versions[versions.length - 1];
  if (!selectedVersion) return <EmptyState title="暂无可运行版本" description="请先上传并校验一个 Skill 版本。" />;
  const versionJobs = jobs.filter((item) => item.skill_version_id === selectedVersion.id);

  function changeVersion(versionId: string) {
    setSelectedVersionId(versionId);
    setCreatingNewTask(false);
    setSelectedJobId(jobs.find((item) => item.skill_version_id === versionId)?.id || "");
    setAttachment(null); setMessageText(""); setError("");
  }

  function startNewTask() {
    if (busy || jobActive) return;
    setCreatingNewTask(true);
    setSelectedJobId("");
    setMessageText("");
    setAttachment(null);
    setError("");
    if (fileInputRef.current) fileInputRef.current.value = "";
    window.requestAnimationFrame(() => messageInputRef.current?.focus());
  }

  async function submitAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const instruction = messageText.trim() || "请根据上传文件完整执行这个 Skill，并生成最终结果。";
    if ((!messageText.trim() && !attachment) || busy || jobActive || !selectedVersion.runtime_runnable) return;
    setBusy(true); setError("");
    const body = new FormData();
    body.set("version_id", selectedVersion.id);
    body.set("instruction", instruction);
    if (selectedModelName) body.set("model_name", selectedModelName);
    if (attachment) body.set("file", attachment);
    try {
      const job = await api<WorkflowJob>("/jobs", { method: "POST", body });
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setCreatingNewTask(false);
      setSelectedJobId(job.id);
      setMessageText(""); setAttachment(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务启动失败");
    } finally { setBusy(false); }
  }

  async function cancelJob() {
    if (!selectedJob || !jobActive) return;
    try {
      await api(`/jobs/${selectedJob.id}/cancel`, { method: "POST" });
      const updated = await api<WorkflowJob>(`/jobs/${selectedJob.id}`);
      setJobs((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "取消任务失败"); }
  }

  async function downloadArtifact(job: WorkflowJob, artifact: WorkflowArtifact) {
    try {
      const blob = await apiBlob(`/jobs/${job.id}/artifacts/${artifact.id}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = artifact.filename; anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "产物下载失败"); }
  }

  async function confirmDeleteJob() {
    if (!deleteTarget || deleteBusy) return;
    setDeleteBusy(true); setDeleteError("");
    try {
      await api(`/jobs/${deleteTarget.id}`, { method: "DELETE" });
      const remaining = jobs.filter((item) => item.id !== deleteTarget.id);
      setJobs(remaining);
      if (selectedJobId === deleteTarget.id) {
        const next = remaining.find((item) => item.skill_version_id === selectedVersion.id);
        setSelectedJobId(next?.id || "");
        setCreatingNewTask(!next);
      }
      setDeleteTarget(null);
    } catch (reason) {
      setDeleteError(reason instanceof Error ? reason.message : "任务删除失败");
    } finally {
      setDeleteBusy(false);
    }
  }

  function eventIcon(type: string, status: string) {
    if (status === "failed" || status === "blocked") return <AlertTriangle />;
    if (status === "running" || status === "queued") return <RotateCw className={status === "running" ? "spin-icon" : ""} />;
    if (type === "tool") return <Code2 />;
    if (type === "artifact" || type === "input") return <FileText />;
    return <Check />;
  }

  const jobEvents = selectedJob?.events || [];
  const toolEvents = jobEvents.filter((item) => item.event_type === "tool");
  const latestToolEvent = toolEvents[toolEvents.length - 1];
  const latestTurn = Math.max(0, ...jobEvents.map((item) => typeof item.data?.turn === "number" ? item.data.turn : 0));
  const toolOperationCount = Math.max(toolEvents.length, 0, ...toolEvents.map((item) => typeof item.data?.operation === "number" ? item.data.operation : 0));
  const recoveryCount = toolEvents.filter((item) => item.status === "failed").length;
  const milestoneEvents = jobEvents.filter((item) => item.event_type === "input" || (item.event_type === "status" && (item.title.includes("队列") || item.title.includes("独立沙箱") || item.title.includes("挂载")))).slice(0, 4);
  const finalEvent = [...jobEvents].reverse().find((item) => item.event_type === "result");
  const runSummaryTitle = jobActive
    ? latestTurn > 0 ? `正在执行第 ${latestTurn} 轮` : "正在准备执行"
    : selectedJob?.status === "succeeded" ? "Skill 执行完成" : "Skill 执行已停止";
  const runSummaryDetail = jobActive
    ? `${toolOperationCount > 0 ? `已完成 ${toolOperationCount} 次工具调用` : "正在等待首个工具操作"}${recoveryCount > 0 ? ` · 已自动修正 ${recoveryCount} 次` : ""}${latestToolEvent ? ` · 当前：${latestToolEvent.title}` : ""}`
    : `${latestTurn > 0 ? `共 ${latestTurn} 轮` : "运行已结束"}${toolOperationCount > 0 ? ` · ${toolOperationCount} 次工具调用` : ""}${recoveryCount > 0 ? ` · 自动修正 ${recoveryCount} 次` : ""}`;
  const runSummaryStatus = jobActive ? "running" : selectedJob?.status === "succeeded" ? "succeeded" : "failed";
  const visibleInputFiles = selectedJob?.input_files.filter((file) => !(selectedJob.trigger === "chat_message" && file.filename === "task-request.txt")) || [];
  const selectedJobSkills = selectedJob ? workflowJobSkills(selectedJob) : [];
  const multiSkillJob = selectedJobSkills.length > 1;

  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "我的 Skill", to: "/app/skills" }, { label: skill.name, to: `/app/skills/${skill.id}` }, { label: "Agent 任务" }]} />
    <div className="workflow-agent-console">
      <aside className="workflow-agent-history">
        <div className="agent-conversations-head"><div><span className="eyebrow">TASKS</span><strong>任务记录</strong></div><button type="button" aria-label="新建任务" title="新建任务" disabled={busy || jobActive} onClick={startNewTask}><Plus /></button></div>
        <label className="agent-version">Skill 版本<select value={selectedVersion.id} disabled={busy || jobActive} onChange={(event) => changeVersion(event.target.value)}>{[...versions].reverse().map((version) => <option key={version.id} value={version.id}>v{version.version} · {executionModeLabels[version.execution_mode] || version.execution_mode}</option>)}</select></label>
        <div className="workflow-agent-task-list">{versionJobs.length ? versionJobs.map((job) => {
          const terminal = ["succeeded", "failed", "cancelled", "blocked"].includes(job.status);
          return <div className={`workflow-agent-task-row ${job.id === selectedJobId ? "active" : ""}`} key={job.id}><button type="button" className="workflow-agent-task-select" onClick={() => { setCreatingNewTask(false); setSelectedJobId(job.id); setError(""); }}><MessageSquareText /><span><strong>{job.instruction.slice(0, 34) || job.input_files[0]?.filename || "Skill 任务"}</strong><small>{new Date(job.created_at).toLocaleString("zh-CN")} · {workflowStatusLabels[job.status]}</small></span><i className={job.status} /></button><button className="workflow-agent-task-delete" type="button" title={terminal ? "删除任务记录" : "请先停止运行中的任务"} aria-label={`删除任务：${job.instruction.slice(0, 34) || "Skill 任务"}`} disabled={!terminal} onClick={() => { setDeleteError(""); setDeleteTarget(job); }}><Trash2 /></button></div>;
        }) : <p>还没有任务。直接在右侧描述需求并添加附件。</p>}</div>
        <div className="agent-context-note"><i /><span>每次任务都在独立沙箱和独立文件工作区中运行</span></div>
      </aside>

      <section className="workflow-agent-chat">
        <header className="agent-chat-head"><div className="agent-identity"><span><Sparkles /></span><div><strong>{multiSkillJob ? `${selectedJobSkills.length} 个 Skill 协作` : skill.name}</strong><small>{multiSkillJob ? selectedJobSkills.map((item) => item.skill_name).join(" + ") : `v${selectedVersion.version}`} · {selectedJob?.model_name || selectedModelName || "默认模型"}</small></div></div><div className="workflow-agent-head-actions">{selectedJob && <span className={`workflow-status ${selectedJob.status}`}>{workflowStatusLabels[selectedJob.status]}</span>}{selectedJob && !jobActive && <button className="button secondary compact" type="button" onClick={startNewTask}><Plus size={14} />新建任务</button>}{jobActive && <button className="button ghost compact" type="button" onClick={() => void cancelJob()}>停止任务</button>}<Link className="button ghost compact" to={`/app/skills/${skill.id}`}>Skill 详情</Link></div></header>

        <div className="workflow-agent-messages" aria-live="polite" ref={messagesPanelRef}>
          {!selectedJob ? <div className="agent-chat-empty"><span><Sparkles /></span><h2>让 {skill.name} 开始工作</h2><p>像和 Agent 对话一样描述需求，可在发送前直接添加附件。</p></div> : <>
            <article className="agent-message user workflow-user-message"><span className="agent-message-avatar">你</span><div><div className="agent-message-bubble">{selectedJobSkills.length > 0 && <div className="workflow-message-skills">{selectedJobSkills.map((item) => <span key={item.skill_version_id}><Zap />{item.skill_name}</span>)}</div>}{selectedJob.instruction || "请协调执行所选 Skill"}{visibleInputFiles.length > 0 && <div className="workflow-message-files">{visibleInputFiles.map((file) => <span key={file.id}><Paperclip />{file.filename}<small>{formatPackageSize(file.size_bytes)}</small></span>)}</div>}</div><time>{new Date(selectedJob.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time></div></article>

            <article className="agent-message assistant workflow-assistant-message"><span className="agent-message-avatar"><Sparkles /></span><div className="workflow-assistant-content">
              <div className="workflow-agent-run-card">
                <header><span className={jobActive ? "live" : selectedJob.status}><i />{jobActive ? multiSkillJob ? "多个 Skill 正在协作" : "Skill 正在工作" : workflowStatusLabels[selectedJob.status]}</span><small>任务 {selectedJob.id.slice(0, 8)}</small></header>
                <div className="workflow-event-stream">
                  {milestoneEvents.length ? milestoneEvents.map((item) => { const displayStatus = ["running", "queued"].includes(item.status) && jobEvents.some((event) => event.sequence > item.sequence) ? "succeeded" : item.status; return <article className={`${item.event_type} ${displayStatus}`} key={item.id}><span>{eventIcon(item.event_type, displayStatus)}</span><div><strong>{item.title}</strong>{item.detail && <p>{item.detail}</p>}<time>{new Date(item.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></div></article>; }) : selectedJob.steps.slice(0, 2).map((step) => <article className={`status ${step.status}`} key={step.id}><span>{eventIcon("status", step.status)}</span><div><strong>{step.name}</strong><p>{step.detail || "等待执行"}</p></div></article>)}
                  <article className={`summary ${runSummaryStatus}`}><span>{eventIcon("status", runSummaryStatus)}</span><div><strong>{runSummaryTitle}</strong><p>{runSummaryDetail}</p>{selectedJob.updated_at && <time>{new Date(selectedJob.updated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>}</div></article>
                </div>
                {jobActive && <div className="workflow-agent-thinking"><i /><i /><i /><span>只展示关键进度，完整工具日志可在下方展开</span></div>}
                {selectedJob.error_message && <div className="workflow-agent-error"><AlertTriangle /><div><strong>任务未完成 · {selectedJob.error_code || "TASK_FAILED"}</strong><p>已保留完整诊断信息，请展开“技术详情”查看。</p></div></div>}
                <details className="workflow-run-details">
                  <summary>技术详情 · {latestTurn || 0} 轮 / {toolOperationCount} 次工具调用{recoveryCount > 0 ? ` / ${recoveryCount} 次自动修正` : ""}</summary>
                  <div className="workflow-run-detail-body">
                    <section className="workflow-step-summary"><h4>任务阶段</h4>{selectedJob.steps.map((step) => <p key={step.id}><span className={step.status}>{step.status === "succeeded" ? <Check /> : step.status === "running" ? <RotateCw /> : <i />}</span><strong>{step.name}</strong><small>{step.detail || "等待前序步骤"}</small></p>)}</section>
                    {jobEvents.length > 0 && <section className="workflow-technical-log"><h4>完整事件日志</h4><div>{jobEvents.map((item) => { const diagnostic = typeof item.data?.diagnostic === "string" ? item.data.diagnostic : item.status === "failed" ? item.detail : ""; return <article className={item.status} key={`detail-${item.id}`}><header><strong>{item.title}</strong><time>{new Date(item.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time></header>{item.detail && (!diagnostic || item.detail !== diagnostic) && <p>{item.detail}</p>}{diagnostic && <pre>{diagnostic}</pre>}</article>; })}</div></section>}
                    {selectedJob.error_message && <section className="workflow-job-diagnostic"><h4>{selectedJob.error_code || "TASK_FAILED"}</h4><pre>{selectedJob.error_message}</pre></section>}
                  </div>
                </details>
              </div>
              {finalEvent && <div className="workflow-final-answer"><strong>已完成</strong><p>{finalEvent.detail}</p></div>}
              {selectedJob.artifacts.length > 0 && <div className="workflow-agent-artifacts"><span>生成的文件</span>{selectedJob.artifacts.map((artifact) => <button type="button" key={artifact.id} onClick={() => void downloadArtifact(selectedJob, artifact)}><FileCheck2 /><span><strong>{artifact.filename}</strong><small>{formatPackageSize(artifact.size_bytes)} · {artifact.verified ? "已校验" : "校验中"}</small></span><Download /></button>)}</div>}
            </div></article>
          </>}
          <div ref={messagesEndRef} />
        </div>

        {attachment && <div className="workflow-pending-file"><FileText /><span><strong>{attachment.name}</strong><small>{formatPackageSize(attachment.size)}</small></span><button type="button" aria-label="移除附件" onClick={() => { setAttachment(null); if (fileInputRef.current) fileInputRef.current.value = ""; }}><X /></button></div>}
        {error && <div className="agent-context-message workflow-agent-error-message">{error}</div>}
        {!selectedVersion.runtime_runnable && <div className="agent-context-message workflow-agent-error-message">{selectedVersion.runtime_block_reason || "当前运行环境不可用"}</div>}
        <form className="agent-composer workflow-agent-composer" onSubmit={submitAgent}>
          <label className="agent-model-picker"><span>运行模型</span><select aria-label="选择运行模型" value={selectedModelName} disabled={busy || jobActive || !availableModels.configured} onChange={(event) => setSelectedModelName(event.target.value)}>{availableModels.models.map((modelName) => <option key={modelName} value={modelName}>{modelName}</option>)}</select></label>
          <textarea ref={messageInputRef} aria-label="给 SkillGo Agent 发送消息" rows={3} maxLength={20000} value={messageText} disabled={busy || jobActive || !selectedVersion.runtime_runnable} onChange={(event) => { setMessageText(event.target.value); setError(""); }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={jobActive ? "当前任务正在独立沙箱中运行…" : `告诉 ${skill.name} 你想完成什么…`} />
          <div><button className="agent-attach" type="button" title="添加附件" aria-label="添加附件" disabled={busy || jobActive || !selectedVersion.runtime_runnable} onClick={() => fileInputRef.current?.click()}><Paperclip /></button><input ref={fileInputRef} type="file" hidden accept=".txt,.md,.csv,.json,.yaml,.yml,.log,.html,.htm,.xml,.docx,.xlsx,.pdf,.png,.jpg,.jpeg" onChange={(event) => { const file = event.target.files?.[0] || null; if (file && file.size > 10 * 1024 * 1024) { event.target.value = ""; setAttachment(null); setError("附件不能超过 10 MB"); return; } setAttachment(file); setError(""); }} /><span className="agent-composer-hint">支持 TXT、DOCX、XLSX、PDF、图片等文件 · 最大 10 MB</span><button type="submit" aria-label="发送并开始任务" disabled={(!messageText.trim() && !attachment) || busy || jobActive || !selectedVersion.runtime_runnable}>{busy ? <RotateCw className="spin-icon" /> : <SendHorizontal />}</button></div>
        </form>
      </section>
    </div>
    {deleteTarget && <div className="modal-backdrop" role="presentation"><section className="confirm-dialog workflow-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-workflow-job-title"><span className="confirm-icon"><Trash2 /></span><h2 id="delete-workflow-job-title">删除这条任务记录？</h2><p>任务“{deleteTarget.instruction.slice(0, 60) || deleteTarget.input_files[0]?.filename || "Skill 任务"}”的输入文件、运行事件和生成产物会一起永久删除。API 客户端之后也无法再查询这条任务。</p>{deleteError && <div className="form-error" role="alert">{deleteError}</div>}<div><button className="button ghost" type="button" disabled={deleteBusy} onClick={() => { setDeleteTarget(null); setDeleteError(""); }}>取消</button><button className="button danger" type="button" disabled={deleteBusy} onClick={() => void confirmDeleteJob()}><Trash2 size={16} />{deleteBusy ? "正在删除…" : "确认删除"}</button></div></section></div>}
  </>;
}

const runStatusLabels: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

function RunBadge({ status }: { status: RunStatus }) {
  return <span className={`run-status ${status}`}>{runStatusLabels[status]}</span>;
}

function chatMessageText(content: Record<string, unknown>) {
  const preferredKeys = ["message", "answer", "result", "summary", "content", "text", "joke"];
  for (const key of preferredKeys) {
    const value = content[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  const textValues = Object.values(content).filter((value): value is string => typeof value === "string" && Boolean(value.trim()));
  if (textValues.length === 1) return textValues[0];
  return JSON.stringify(content, null, 2);
}

function transientChatMessage(role: "user" | "assistant", text: string): ConversationMessage {
  return {
    id: `local-${role}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    run_id: null,
    role,
    content: { message: text },
    created_at: new Date().toISOString(),
  };
}

export function RunSkillPage() {
  const { skillId } = useParams();
  const { data: skill, loading } = useLoad<Skill | null>(`/skills/${skillId}`, null);
  const { data: conversations, setData: setConversations } = useLoad<Conversation[]>(`/conversations?skill_id=${encodeURIComponent(skillId || "")}`, []);
  const { data: availableModels } = useLoad<AvailableModels>("/models/available", { configured: false, models: [], default_model: null });
  const requestedVersion = new URLSearchParams(window.location.search).get("version");
  const requestedConversation = new URLSearchParams(window.location.search).get("conversation");
  const requestedPrompt = new URLSearchParams(window.location.search).get("prompt") || "";
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [selectedConversationId, setSelectedConversationId] = useState("");
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messageText, setMessageText] = useState(requestedPrompt);
  const [selectedModelName, setSelectedModelName] = useState("");
  const [pendingMessage, setPendingMessage] = useState("");
  const [contextBusy, setContextBusy] = useState(false);
  const [contextMessage, setContextMessage] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[]>([]);
  const [fileBusy, setFileBusy] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!skill || selectedVersionId) return;
    const available = (skill.versions || []).filter((version) => version.execution_mode === "instruction_only");
    const initial = available.find((version) => version.id === requestedVersion) || available[available.length - 1];
    if (!initial) return;
    setSelectedVersionId(initial.id);
  }, [requestedVersion, selectedVersionId, skill]);

  useEffect(() => {
    if (!selectedVersionId || selectedConversationId) return;
    const latest = conversations.find((item) => item.id === requestedConversation && item.skill_version_id === selectedVersionId)
      || conversations.find((item) => item.skill_version_id === selectedVersionId);
    if (latest) setSelectedConversationId(latest.id);
  }, [conversations, requestedConversation, selectedConversationId, selectedVersionId]);

  useEffect(() => {
    if (!selectedModelName && availableModels.default_model) setSelectedModelName(availableModels.default_model);
  }, [availableModels.default_model, selectedModelName]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedConversationId) {
      setMessages([]);
      setMessagesLoading(false);
      return;
    }
    setMessagesLoading(true);
    api<ConversationDetail>(`/conversations/${selectedConversationId}`)
      .then((detail) => { if (!cancelled) setMessages(detail.messages); })
      .catch((reason: Error) => { if (!cancelled) setContextMessage(reason.message); })
      .finally(() => { if (!cancelled) setMessagesLoading(false); });
    return () => { cancelled = true; };
  }, [selectedConversationId]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedConversationId) {
      setWorkspaceFiles([]);
      return;
    }
    api<WorkspaceFile[]>(`/conversations/${selectedConversationId}/files`)
      .then((files) => { if (!cancelled) setWorkspaceFiles(files); })
      .catch((reason: Error) => { if (!cancelled) setContextMessage(reason.message); });
    return () => { cancelled = true; };
  }, [selectedConversationId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [busy, messages, pendingMessage]);

  if (loading) return <div className="detail-loading" />;
  if (!skill) return <EmptyState title="没有找到这个 Skill" description="你可能没有运行权限。" />;
  const versions = (skill.versions || []).filter((version) => version.execution_mode === "instruction_only");
  const selectedVersion = versions.find((version) => version.id === selectedVersionId) || versions[versions.length - 1];
  if (!selectedVersion) return <EmptyState title="这个 Skill 不能使用对话调试" description="它需要工作流运行器、平台工具或 Linux 沙箱。" action={<Link className="button primary" to={`/app/skills/${skill.id}/workflow`}>查看工作流运行条件</Link>} />;
  const versionConversations = conversations
    .filter((item) => item.skill_version_id === selectedVersion.id)
    .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime());
  const selectedConversation = versionConversations.find((item) => item.id === selectedConversationId) || null;

  function changeVersion(versionId: string) {
    const version = versions.find((item) => item.id === versionId);
    if (!version) return;
    setSelectedVersionId(version.id);
    setSelectedConversationId("");
    setMessages([]); setWorkspaceFiles([]); setMessageText(""); setPendingMessage("");
    setRenaming(false); setRenameDraft("");
    setError(""); setContextMessage("");
  }

  async function createConversation(): Promise<Conversation | null> {
    setContextBusy(true); setContextMessage("");
    try {
      const created = await api<Conversation>("/conversations", { method: "POST", body: JSON.stringify({ version_id: selectedVersion.id }) });
      setConversations((current) => [created, ...current]);
      setSelectedConversationId(created.id);
      setMessages([]); setWorkspaceFiles([]); setError(""); setRenaming(false);
      setContextMessage("新会话已创建");
      return created;
    } catch (reason) {
      setContextMessage(reason instanceof Error ? reason.message : "创建会话失败");
      return null;
    }
    finally { setContextBusy(false); }
  }

  function beginRename() {
    if (!selectedConversation) return;
    setRenameDraft(selectedConversation.title);
    setRenaming(true);
    setContextMessage("");
  }

  async function renameConversation() {
    if (!selectedConversation) return;
    const title = renameDraft.trim();
    if (!title) { setContextMessage("会话名称不能为空"); return; }
    if (title === selectedConversation.title) { setRenaming(false); return; }
    setContextBusy(true); setContextMessage("");
    try {
      const updated = await api<Conversation>(`/conversations/${selectedConversation.id}`, { method: "PATCH", body: JSON.stringify({ title }) });
      setConversations((current) => current.map((item) => item.id === updated.id ? updated : item));
      setRenaming(false); setRenameDraft("");
      setContextMessage(`已重命名为“${updated.title}”`);
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "重命名失败"); }
    finally { setContextBusy(false); }
  }

  async function clearConversation() {
    if (!selectedConversation || !window.confirm("确定清空这个会话的全部上下文吗？历史 Run 记录仍会保留。")) return;
    setContextBusy(true); setContextMessage("");
    try {
      await api(`/conversations/${selectedConversation.id}/messages`, { method: "DELETE" });
      setConversations((current) => current.map((item) => item.id === selectedConversation.id ? { ...item, message_count: 0 } : item));
      setMessages([]); setError("");
      setContextMessage("上下文已清空，下一次运行将从空白开始");
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "清空失败"); }
    finally { setContextBusy(false); }
  }

  async function deleteConversation() {
    if (!selectedConversation || !window.confirm(`确定删除会话“${selectedConversation.title}”吗？历史 Run 记录仍会保留。`)) return;
    setContextBusy(true); setContextMessage("");
    try {
      await api(`/conversations/${selectedConversation.id}`, { method: "DELETE" });
      setConversations((current) => current.filter((item) => item.id !== selectedConversation.id));
      setSelectedConversationId("");
      setMessages([]); setWorkspaceFiles([]); setError("");
      setRenaming(false); setRenameDraft("");
      setContextMessage("会话已删除");
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "删除会话失败"); }
    finally { setContextBusy(false); }
  }

  async function uploadWorkspaceFile(event: ChangeEvent<HTMLInputElement>) {
    const selectedFile = event.target.files?.[0];
    event.target.value = "";
    if (!selectedFile || busy || fileBusy) return;
    setFileBusy(true); setContextMessage(""); setError("");
    try {
      const activeConversation = selectedConversation || await createConversation();
      if (!activeConversation) throw new Error("无法创建文件工作区");
      const body = new FormData();
      body.append("file", selectedFile);
      const uploaded = await api<WorkspaceFile>(`/conversations/${activeConversation.id}/files`, { method: "POST", body });
      setWorkspaceFiles((current) => [...current, uploaded]);
      setContextMessage(uploaded.readable ? `已上传 ${uploaded.filename}，Skill 可以读取其内容` : `已上传 ${uploaded.filename}；该格式仅支持保存和下载`);
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "文件上传失败"); }
    finally { setFileBusy(false); }
  }

  async function downloadWorkspaceFile(file: WorkspaceFile) {
    try {
      const blob = await apiBlob(`/conversations/${file.conversation_id}/files/${file.id}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = file.filename; anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "文件下载失败"); }
  }

  async function deleteWorkspaceFile(file: WorkspaceFile) {
    if (!window.confirm(`确定删除文件“${file.filename}”吗？`)) return;
    setFileBusy(true); setContextMessage("");
    try {
      await api(`/conversations/${file.conversation_id}/files/${file.id}`, { method: "DELETE" });
      setWorkspaceFiles((current) => current.filter((item) => item.id !== file.id));
      setContextMessage(`已删除 ${file.filename}`);
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "文件删除失败"); }
    finally { setFileBusy(false); }
  }

  async function saveMessageAsArtifact(message: ConversationMessage) {
    if (!selectedConversation || !skill) return;
    const content = chatMessageText(message.content);
    setFileBusy(true); setContextMessage("");
    try {
      const artifact = await api<WorkspaceFile>(`/conversations/${selectedConversation.id}/artifacts`, { method: "POST", body: JSON.stringify({ filename: `${skill.slug}-result-${Date.now()}.txt`, content }) });
      setWorkspaceFiles((current) => [...current, artifact]);
      setContextMessage(`已保存产物 ${artifact.filename}`);
    } catch (reason) { setContextMessage(reason instanceof Error ? reason.message : "保存产物失败"); }
    finally { setFileBusy(false); }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = messageText.trim();
    if (!text || busy || contextBusy) return;
    setBusy(true); setError(""); setContextMessage("");
    setPendingMessage(text); setMessageText("");
    try {
      const activeConversation = selectedConversation || await createConversation();
      if (!activeConversation) throw new Error("无法创建会话，请稍后重试");
      const run = await api<SkillRun>("/runs", { method: "POST", body: JSON.stringify({ version_id: selectedVersion.id, conversation_id: activeConversation.id, message: text, model_name: selectedModelName || undefined }) });
      if (run.status === "succeeded" && run.output) {
        setConversations((current) => current.map((item) => item.id === activeConversation.id ? { ...item, message_count: item.message_count + 2, is_running: false, updated_at: new Date().toISOString() } : item));
        try {
          const detail = await api<ConversationDetail>(`/conversations/${activeConversation.id}`);
          setMessages(detail.messages);
        } catch {
          setMessages((current) => [...current, transientChatMessage("user", text), { ...transientChatMessage("assistant", chatMessageText(run.output || {})), run_id: run.id }]);
        }
      } else {
        const failure = run.error_message || "这次运行没有成功，请稍后重试。";
        setMessages((current) => [...current, transientChatMessage("user", text), transientChatMessage("assistant", failure)]);
        setError(failure);
      }
    } catch (reason) {
      const failure = reason instanceof Error ? reason.message : "发送失败";
      setMessages((current) => [...current, transientChatMessage("user", text), transientChatMessage("assistant", failure)]);
      setError(failure);
    } finally { setPendingMessage(""); setBusy(false); }
  }
  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "我的 Skill", to: "/app/skills" }, { label: skill.name, to: `/app/skills/${skill.id}` }, { label: "对话调试" }]} />
    <PageTitle eyebrow="INSTRUCTION DEBUG" title={`${skill.name} · 对话调试`} description="仅用于调试纯指令 Skill。需要脚本、工具或正式产物时，请使用“运行工作流”。" action={<Link className="button ghost" to={`/app/skills/${skill.id}`}><ArrowLeft size={16} />返回 Skill 详情</Link>} />
    <div className="agent-console">
      <aside className="agent-conversations">
        <div className="agent-conversations-head"><div><span className="eyebrow">CONVERSATIONS</span><strong>会话</strong></div><button type="button" aria-label="新建会话" title="新建会话" disabled={busy || contextBusy} onClick={() => void createConversation()}><Plus /></button></div>
        <label className="agent-version">Skill 版本<select value={selectedVersion.id} disabled={busy || contextBusy} onChange={(event) => changeVersion(event.target.value)}>{[...versions].reverse().map((version) => <option key={version.id} value={version.id}>v{version.version}</option>)}</select></label>
        <div className="agent-conversation-list">
          {versionConversations.length ? versionConversations.map((conversation) => <button type="button" className={conversation.id === selectedConversationId ? "active" : ""} key={conversation.id} onClick={() => { setSelectedConversationId(conversation.id); setRenaming(false); setRenameDraft(""); setContextMessage(""); setError(""); }}><MessageSquareText /><span><strong>{conversation.title}</strong><small>{conversation.message_count ? `${Math.floor(conversation.message_count / 2)} 轮对话` : "新会话"}</small></span></button>) : <p className="agent-no-conversations">还没有会话。直接在右侧发送消息即可开始。</p>}
        </div>
        <div className="agent-context-note"><i /><span>不同用户的会话和历史互相隔离</span></div>
      </aside>

      <section className="agent-chat">
        <header className="agent-chat-head">
          <div className="agent-identity"><span><Sparkles /></span><div>{renaming && selectedConversation ? <div className="agent-rename"><input aria-label="编辑会话名称" value={renameDraft} maxLength={160} autoFocus disabled={contextBusy} onChange={(event) => setRenameDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void renameConversation(); } if (event.key === "Escape") { setRenaming(false); setRenameDraft(""); } }} /><button type="button" title="保存" onClick={renameConversation}><Check /></button><button type="button" title="取消" onClick={() => { setRenaming(false); setRenameDraft(""); }}><X /></button></div> : <><strong>{selectedConversation?.title || "新对话"}</strong><small>{skill.name} · v{selectedVersion.version} · 最多携带最近 10 轮上下文</small></>}</div></div>
          {selectedConversation && !renaming && <div className="agent-chat-actions"><button type="button" title="重命名" aria-label="重命名会话" disabled={busy || contextBusy} onClick={beginRename}><PencilLine /></button><button type="button" title="清空消息" aria-label="清空消息" disabled={busy || contextBusy || !selectedConversation.message_count} onClick={clearConversation}><RotateCw /></button><button className="danger" type="button" title="删除会话" aria-label="删除会话" disabled={busy || contextBusy} onClick={deleteConversation}><Trash2 /></button></div>}
        </header>

        <div className="agent-messages" aria-live="polite">
          {messagesLoading ? <div className="agent-messages-loading"><i /><i /><i /></div> : !messages.length && !pendingMessage ? <div className="agent-chat-empty"><span><Sparkles /></span><h2>开始与 {skill.name} 对话</h2><p>像使用普通 Agent 一样描述你的需求，不需要填写 JSON。</p><button type="button" onClick={() => setMessageText("先介绍一下你能帮我完成什么任务")}>先介绍一下你能做什么</button></div> : messages.map((message) => <article className={`agent-message ${message.role === "assistant" ? "assistant" : "user"}`} key={message.id}><span className="agent-message-avatar">{message.role === "assistant" ? <Sparkles /> : "你"}</span><div><div className="agent-message-bubble">{chatMessageText(message.content)}</div><div className="agent-message-meta"><time>{new Date(message.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time>{message.role === "assistant" && selectedConversation && <button type="button" disabled={fileBusy || busy} onClick={() => void saveMessageAsArtifact(message)}><Save />保存为文件</button>}</div></div></article>)}
          {pendingMessage && <article className="agent-message user pending"><span className="agent-message-avatar">你</span><div><div className="agent-message-bubble">{pendingMessage}</div><time>刚刚</time></div></article>}
          {busy && <article className="agent-message assistant thinking"><span className="agent-message-avatar"><Sparkles /></span><div><div className="agent-message-bubble"><i /><i /><i /></div><time>Skill 正在处理</time></div></article>}
          <div ref={messagesEndRef} />
        </div>

        {workspaceFiles.length > 0 && <div className="agent-files"><span><Paperclip />当前会话文件</span>{workspaceFiles.map((file) => <article key={file.id}><FileText /><span><strong title={file.filename}>{file.filename}</strong><small>{formatPackageSize(file.size_bytes)} · {file.source === "generated" ? "Skill 产物" : file.readable ? "可供 Skill 读取" : "仅存储"}</small></span><button type="button" title="下载" aria-label={`下载 ${file.filename}`} onClick={() => void downloadWorkspaceFile(file)}><Download /></button><button className="danger" type="button" title="删除" aria-label={`删除 ${file.filename}`} disabled={fileBusy || busy} onClick={() => void deleteWorkspaceFile(file)}><X /></button></article>)}</div>}
        {contextMessage && <div className="agent-context-message" aria-live="polite">{contextMessage}</div>}
        <form className="agent-composer" onSubmit={submit}>
          <label className="agent-model-picker"><span>对话模型</span><select aria-label="选择对话模型" value={selectedModelName} disabled={busy || contextBusy || !availableModels.configured} onChange={(event) => setSelectedModelName(event.target.value)}>{availableModels.models.map((modelName) => <option key={modelName} value={modelName}>{modelName}</option>)}</select></label>
          <textarea aria-label="给 Skill 发送消息" rows={3} maxLength={20000} value={messageText} disabled={busy || contextBusy || selectedConversation?.is_running} onChange={(event) => { setMessageText(event.target.value); setError(""); }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={`给 ${skill.name} 发送消息…`} />
          <div><button className="agent-attach" type="button" title="上传会话文件" aria-label="上传会话文件" disabled={busy || fileBusy || contextBusy} onClick={() => fileInputRef.current?.click()}>{fileBusy ? <RotateCw className="spin-icon" /> : <Paperclip />}</button><input ref={fileInputRef} type="file" hidden onChange={uploadWorkspaceFile} accept=".txt,.md,.csv,.json,.yaml,.yml,.log,.html,.htm,.xml,.docx,.xlsx,.pdf,.png,.jpg,.jpeg" /><span className="agent-composer-hint">TXT、DOCX、XLSX 可读取；PDF 和图片暂仅保存 · Enter 发送</span>{error && <small>{error}</small>}<button type="submit" aria-label="发送消息" disabled={!messageText.trim() || busy || contextBusy || selectedConversation?.is_running}><SendHorizontal /></button></div>
        </form>
      </section>
    </div>
  </>;
}

function fallbackCopyText(value: string) {
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("Browser rejected the copy command");
}

export function EndpointsPage() {
  const { data: endpoints, setData, loading } = useLoad<Endpoint[]>("/endpoints", []);
  const [secret, setSecret] = useState<EndpointCreated | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<{ key: string; tone: "success" | "error"; message: string } | null>(null);
  const copyTimerRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
  }, []);

  async function copyText(key: string, label: string, value: string) {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (!copied) {
      try {
        fallbackCopyText(value);
        copied = true;
      } catch {
        copied = false;
      }
    }
    setCopyFeedback({
      key,
      tone: copied ? "success" : "error",
      message: copied ? `${label}已复制到剪贴板` : `无法自动复制${label}，请选中内容后手动复制`,
    });
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopyFeedback(null), copied ? 2200 : 4200);
  }

  async function toggle(endpoint: Endpoint) {
    const updated = await api<Endpoint>(`/endpoints/${endpoint.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !endpoint.is_active }) });
    setData(endpoints.map((item) => item.id === updated.id ? updated : item));
  }
  async function rotate(endpoint: Endpoint) {
    const updated = await api<EndpointCreated>(`/endpoints/${endpoint.id}/rotate-key`, { method: "POST" });
    setSecret(updated);
    setData(endpoints.map((item) => item.id === updated.id ? updated : item));
  }
  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "API Endpoint" }]} />
    <div className="workspace-section-head"><h1>API Endpoint</h1></div>
    {secret && <section className="secret-banner"><div><span className="eyebrow">新密钥 · 仅显示一次</span><strong>{secret.name}</strong><code>{secret.api_key}</code></div><button className={`button secondary ${copyFeedback?.key === "secret" && copyFeedback.tone === "success" ? "copied" : ""}`} onClick={() => void copyText("secret", "API Key", secret.api_key)}>{copyFeedback?.key === "secret" && copyFeedback.tone === "success" ? <Check size={16} /> : <Copy size={16} />}{copyFeedback?.key === "secret" && copyFeedback.tone === "success" ? "已复制" : "复制密钥"}</button></section>}
    {loading ? <div className="panel detail-loading" /> : endpoints.length ? <div className="endpoint-grid">{endpoints.map((endpoint) => {
      const path = endpointInvokePath(endpoint);
      const curl = endpointCurlExample(endpoint).replaceAll("\n+", "\n");
      const urlCopyKey = `url-${endpoint.id}`;
      const curlCopyKey = `curl-${endpoint.id}`;
      const urlCopied = copyFeedback?.key === urlCopyKey && copyFeedback.tone === "success";
      const curlCopied = copyFeedback?.key === curlCopyKey && copyFeedback.tone === "success";
      return <TiltSurface className="endpoint-card-tilt" key={endpoint.id}><article className="panel endpoint-card">
        <header><div><h3>{endpoint.name}</h3><p>{endpoint.skill_name} · v{endpoint.version}</p></div><span className={endpoint.is_active ? "endpoint-live" : "endpoint-off"}>{endpoint.is_active ? "运行中" : "已停用"}</span></header>
        <div className="endpoint-url"><code>POST {path}</code><button className={urlCopied ? "copied" : ""} type="button" title={urlCopied ? "调用地址已复制" : "复制调用地址"} aria-label={urlCopied ? "调用地址已复制" : "复制调用地址"} onClick={() => void copyText(urlCopyKey, "调用地址", `${window.location.origin}${path}`)}>{urlCopied ? <Check size={14} /> : <Copy size={14} />}</button></div>
        <details className="endpoint-guide"><summary><Code2 size={14} />查看 cURL 调用示例</summary><div><pre><code>{curl}</code></pre><button className={`button ghost compact ${curlCopied ? "copied" : ""}`} type="button" onClick={() => void copyText(curlCopyKey, "cURL 示例", curl)}>{curlCopied ? <Check size={14} /> : <Copy size={14} />}{curlCopied ? "已复制" : "复制示例"}</button></div></details>
        <footer><button className="button ghost compact" onClick={() => rotate(endpoint)}><RotateCw size={14} />轮换密钥</button><button className={`button compact ${endpoint.is_active ? "danger" : "secondary"}`} onClick={() => toggle(endpoint)}>{endpoint.is_active ? <PowerOff size={14} /> : <Power size={14} />}{endpoint.is_active ? "停用" : "启用"}</button></footer>
      </article></TiltSurface>;
    })}</div> : <EmptyState icon={Zap} title="还没有 Endpoint" description="进入一个已发布且当前可运行的 Skill 版本，点击“发布为 API”。" />}
    {copyFeedback && <div className={`copy-toast ${copyFeedback.tone}`} role={copyFeedback.tone === "error" ? "alert" : "status"} aria-live="polite">{copyFeedback.tone === "success" ? <Check size={17} /> : <AlertTriangle size={17} />}<span>{copyFeedback.message}</span></div>}
  </>;
}

export function AdminReviewsPage() {
  const { data: versions, setData, loading } = useLoad<SkillVersion[]>("/admin/reviews", []);
  const [reviewTarget, setReviewTarget] = useState<SkillVersion | null>(null);
  const [decision, setDecision] = useState<"approve" | "reject">("approve");
  const [note, setNote] = useState("");
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState("");

  function openReview(version: SkillVersion, nextDecision: "approve" | "reject") {
    setReviewTarget(version); setDecision(nextDecision); setNote(""); setReviewError("");
  }

  async function confirmReview() {
    if (!reviewTarget || reviewBusy) return;
    if (decision === "reject" && !note.trim()) { setReviewError("驳回时必须填写原因"); return; }
    setReviewBusy(true); setReviewError("");
    try {
      await api(`/admin/reviews/${reviewTarget.id}/${decision}`, { method: "POST", body: JSON.stringify({ note: note.trim() }) });
      setData(versions.filter((item) => item.id !== reviewTarget.id));
      setReviewTarget(null); setNote("");
    } catch (reason) { setReviewError(reason instanceof Error ? reason.message : "审核操作失败"); }
    finally { setReviewBusy(false); }
  }

  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "发布审核" }]} />
    <PageTitle eyebrow="MODERATION" title="发布审核" description="检查版本内容、权限申请、类型和包哈希，再决定是否进入社区。" />
    {loading ? <div className="panel detail-loading" /> : versions.length ? <div className="review-grid">{versions.map((version) => <article className="review-card" key={version.id}><div className="review-card-head"><span className="skill-icon"><FileCheck2 /></span><div><span className="eyebrow">版本</span><h3>v{version.version}</h3></div><StatusBadge status={version.status} /></div><dl><div><dt>类型</dt><dd>{skillTypeLabels[version.skill_type]}</dd></div><div><dt>包摘要</dt><dd><code>{version.package_sha256.slice(0, 16)}…</code></dd></div><div><dt>权限项</dt><dd>{Object.keys(version.requested_permissions).length}</dd></div></dl><div className="review-actions"><button className="button danger" onClick={() => openReview(version, "reject")}>驳回</button><button className="button primary" onClick={() => openReview(version, "approve")}><UserRoundCheck size={17} />批准发布</button></div></article>)}</div> : <EmptyState icon={ShieldCheck} title="审核队列为空" description="所有提交的 Skill 版本都已经处理。" />}
    {reviewTarget && <div className="modal-backdrop" role="presentation"><section className={`confirm-dialog review-dialog ${decision}`} role="dialog" aria-modal="true" aria-labelledby="review-dialog-title"><span className="confirm-icon">{decision === "approve" ? <UserRoundCheck /> : <AlertTriangle />}</span><h2 id="review-dialog-title">{decision === "approve" ? `批准发布 v${reviewTarget.version}` : `驳回版本 v${reviewTarget.version}`}</h2><p>{decision === "approve" ? "批准后该版本会进入公开社区，版本内容将保持不可变。" : "请写明具体原因，作者会在版本管理页看到这条审核意见。"}</p><label>{decision === "approve" ? "审核备注（可选）" : "驳回原因"}<textarea value={note} rows={4} maxLength={4000} autoFocus placeholder={decision === "approve" ? "例如：权限范围与用途说明一致" : "说明需要修改的内容或权限问题"} onChange={(event) => { setNote(event.target.value); setReviewError(""); }} /></label>{reviewError && <div className="form-error" aria-live="polite">{reviewError}</div>}<div><button className="button ghost" type="button" disabled={reviewBusy} onClick={() => setReviewTarget(null)}>取消</button><button className={`button ${decision === "approve" ? "primary" : "danger"}`} type="button" disabled={reviewBusy} onClick={confirmReview}>{reviewBusy ? "正在提交…" : decision === "approve" ? "确认批准" : "确认驳回"}</button></div></section></div>}
  </>;
}

export function AdminUsersPage() {
  const { user: actor } = useAuth();
  const { data: users, setData } = useLoad<User[]>("/admin/users", []);
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([]);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [busyUserId, setBusyUserId] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [userMessage, setUserMessage] = useState("");
  const memberUsers = users.filter((user) => user.role === "user");
  const administratorUsers = users.filter((user) => user.role !== "user");

  async function toggle(user: User) {
    setBusyUserId(user.id); setUserMessage("");
    try { const updated = await api<User>(`/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !user.is_active }) }); setData(users.map((item) => item.id === user.id ? updated : item)); }
    catch (reason) { setUserMessage(reason instanceof Error ? reason.message : "账号状态更新失败"); }
    finally { setBusyUserId(""); }
  }
  async function role(user: User, role: Role) {
    setBusyUserId(user.id); setUserMessage("");
    try { const updated = await api<User>(`/super-admin/users/${user.id}/role`, { method: "PATCH", body: JSON.stringify({ role }) }); setData(users.map((item) => item.id === user.id ? updated : item)); }
    catch (reason) { setUserMessage(reason instanceof Error ? reason.message : "平台角色更新失败"); }
    finally { setBusyUserId(""); }
  }
  async function approveAdmin(user: User) {
    setBusyUserId(user.id); setUserMessage("");
    try {
      const updated = await api<User>(`/super-admin/users/${user.id}/approve-admin`, { method: "POST" });
      setData(users.map((item) => item.id === user.id ? updated : item));
      setUserMessage(`已批准 ${user.display_name} 的管理员申请。`);
    } catch (reason) { setUserMessage(reason instanceof Error ? reason.message : "管理员申请审批失败"); }
    finally { setBusyUserId(""); }
  }
  function selectUser(userId: string, checked: boolean) { setSelectedUserIds((current) => checked ? [...new Set([...current, userId])] : current.filter((id) => id !== userId)); }
  async function deleteSelectedUsers() {
    if (!selectedUserIds.length || deleteBusy) return;
    setDeleteBusy(true); setUserMessage("");
    try {
      await api<{ message: string }>("/super-admin/users/delete", { method: "POST", body: JSON.stringify({ user_ids: selectedUserIds }) });
      setData(users.filter((user) => !selectedUserIds.includes(user.id)));
      setUserMessage(`已永久删除 ${selectedUserIds.length} 个账号及其个人数据。`);
      setSelectedUserIds([]); setDeleteOpen(false);
    } catch (reason) { setUserMessage(reason instanceof Error ? reason.message : "账号删除失败"); setDeleteOpen(false); }
    finally { setDeleteBusy(false); }
  }
  function renderManagedUser(user: User) {
    const awaitingApproval = user.role === "admin" && !user.is_active;
    const isSuperAdmin = user.role === "super_admin";
    const canToggle = user.id !== actor?.id && (user.role === "user" || actor?.role === "super_admin");
    return <article className={`user-management-row${isSuperAdmin ? " super-admin" : ""}${selectedUserIds.includes(user.id) ? " selected" : ""}`} key={user.id}>
      <span className="user-select-slot">{actor?.role === "super_admin" && <input type="checkbox" aria-label={`选择账号 ${user.display_name}`} disabled={user.id === actor.id} checked={selectedUserIds.includes(user.id)} onChange={(event) => selectUser(user.id, event.target.checked)} />}</span>
      <span className="user-cell"><i>{user.display_name.slice(0, 1).toUpperCase()}</i><span><strong>{user.display_name}</strong><small>{user.email}</small></span></span>
      <span className="user-role-slot">{actor?.role === "super_admin" && user.id !== actor.id && !isSuperAdmin ? <select disabled={busyUserId === user.id} value={user.role} aria-label={`修改 ${user.display_name} 的角色`} onChange={(event) => void role(user, event.target.value as Role)}><option value="user">成员</option><option value="admin">管理员</option></select> : <b className={`role-pill${isSuperAdmin ? " super-admin" : ""}`}>{roleLabels[user.role]}</b>}</span>
      <span className={`user-account-status${awaitingApproval ? " pending-account" : ""}`}><i className={user.is_active ? "dot active" : "dot"} />{awaitingApproval ? "待审核" : user.is_active ? "正常" : "已停用"}</span>
      <span className="user-joined-at">{new Date(user.created_at).toLocaleDateString("zh-CN")}</span>
      <span className="user-row-actions">{awaitingApproval ? actor?.role === "super_admin" ? <button className="text-button" disabled={busyUserId === user.id} onClick={() => void approveAdmin(user)}>{busyUserId === user.id ? "处理中…" : "批准"}</button> : <small className="approval-waiting">等待超级管理员</small> : canToggle ? <button className="text-button" disabled={busyUserId === user.id} onClick={() => void toggle(user)}>{busyUserId === user.id ? "处理中…" : user.is_active ? "停用" : "启用"}</button> : null}</span>
    </article>;
  }
  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "用户管理" }]} />
    <PageTitle eyebrow="ADMINISTRATION" title="用户管理" description="成员注册后直接启用；管理员申请由唯一的超级管理员审核。" action={actor?.role === "super_admin" ? <button className="button danger" type="button" disabled={!selectedUserIds.length} onClick={() => setDeleteOpen(true)}><Trash2 size={16} />删除所选账号{selectedUserIds.length ? `（${selectedUserIds.length}）` : ""}</button> : undefined} />
    {userMessage && <div className="user-admin-message" role="status">{userMessage}</div>}
    <div className="user-role-groups">
      <details className="user-role-group"><summary><span className="user-group-icon"><Users /></span><span><strong>成员</strong><small>普通账号，注册后可直接使用平台</small></span><b>{memberUsers.length}</b><ChevronDown /></summary><div className="user-group-list">{memberUsers.map(renderManagedUser)}</div></details>
      <details className="user-role-group administrators"><summary><span className="user-group-icon"><ShieldCheck /></span><span><strong>管理员</strong><small>包含管理员与唯一的超级管理员</small></span><b>{administratorUsers.length}</b><ChevronDown /></summary><div className="user-group-list">{administratorUsers.map(renderManagedUser)}</div></details>
    </div>
    {deleteOpen && <div className="modal-backdrop" role="presentation"><section className="confirm-dialog user-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-users-title"><span className="confirm-icon"><AlertTriangle /></span><h2 id="delete-users-title">永久删除 {selectedUserIds.length} 个账号？</h2><p>账号、会话、附件、历史任务和产物会永久删除，无法恢复。仍拥有 Skill 或正在运行任务的账号会被系统阻止删除。</p><div className="selected-user-summary">{users.filter((user) => selectedUserIds.includes(user.id)).map((user) => <span key={user.id}>{user.display_name}<small>{user.email}</small></span>)}</div><div><button className="button ghost" type="button" disabled={deleteBusy} onClick={() => setDeleteOpen(false)}>取消</button><button className="button danger" type="button" disabled={deleteBusy} onClick={() => void deleteSelectedUsers()}><Trash2 size={16} />{deleteBusy ? "正在删除…" : "确认永久删除"}</button></div></section></div>}
  </>;
}

export function ModelSettingsPage() {
  const emptyCatalog: ModelConnectionList = { configured: false, default_model: null, items: [] };
  const { data: catalog, setData: setCatalog, loading } = useLoad<ModelConnectionList>("/super-admin/models", emptyCatalog);
  const [editingModel, setEditingModel] = useState<ModelConnectionItem | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [modelName, setModelName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [timeoutSeconds, setTimeoutSeconds] = useState(120);
  const [temperature, setTemperature] = useState(0.2);
  const [jsonMode, setJsonMode] = useState(true);
  const [nativeTools, setNativeTools] = useState(true);
  const [tlsVerify, setTlsVerify] = useState(true);
  const [isDefault, setIsDefault] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [busyAction, setBusyAction] = useState<"save" | "test" | "">("");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"success" | "error" | "">("");
  const [testResult, setTestResult] = useState<ModelConnectionTestResult | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);


  function resetEditor(item: ModelConnectionItem | null = null) {
    setEditingModel(item);
    setModelName(item?.model_name || "");
    setBaseUrl(item?.base_url || "");
    setApiKey("");
    setTimeoutSeconds(item?.timeout_seconds ?? 120);
    setTemperature(item?.temperature ?? 0.2);
    setJsonMode(item?.json_mode ?? true);
    setNativeTools(item?.native_tools ?? true);
    setTlsVerify(item?.tls_verify ?? true);
    setIsDefault(item?.is_default ?? catalog.items.length === 0);
    setEnabled(item?.enabled ?? true);
    setMessage(""); setMessageTone("");
    setTestResult(null);
    setEditorOpen(true);
  }

  function closeEditor() {
    if (busyAction) return;
    setEditorOpen(false);
    setEditingModel(null);
    setMessage(""); setMessageTone("");
    setTestResult(null);
  }

  useEffect(() => {
    if (!editorOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyAction) closeEditor();
    };
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [busyAction, editorOpen]);

  function payload() {
    return {
      base_url: baseUrl.trim(),
      api_key: apiKey.trim() || null,
      clear_api_key: false,
      model_name: modelName.trim(),
      models: [modelName.trim()],
      default_model: modelName.trim(),
      timeout_seconds: timeoutSeconds,
      temperature,
      json_mode: jsonMode,
      native_tools: nativeTools,
      tls_verify: tlsVerify,
      is_default: isDefault,
      enabled,
      model_id: editingModel?.id || null,
    };
  }

  async function testConnection(item?: ModelConnectionItem) {
    setBusyAction("test"); setMessage(""); setTestResult(null);
    try {
      const candidate = item ? { base_url: item.base_url, api_key: null, clear_api_key: false, models: [item.model_name], default_model: item.model_name, timeout_seconds: item.timeout_seconds, temperature: item.temperature, json_mode: item.json_mode, native_tools: item.native_tools, tls_verify: item.tls_verify, model_id: item.id } : payload();
      const result = await api<ModelConnectionTestResult>("/super-admin/model/test", { method: "POST", body: JSON.stringify(candidate) });
      setTestResult(result);
      setMessageTone("success");
      setMessage(`连接正常，${result.model_name} 响应耗时 ${result.latency_ms} ms`);
    } catch (reason) { setMessageTone("error"); setMessage(reason instanceof Error ? reason.message : "模型连接测试失败"); }
    finally { setBusyAction(""); }
  }

  async function saveConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusyAction("save"); setMessage(""); setMessageTone(""); setTestResult(null);
    try {
      const path = editingModel ? `/super-admin/models/${editingModel.id}` : "/super-admin/models";
      const updated = await api<ModelConnectionItem>(path, { method: editingModel ? "PUT" : "POST", body: JSON.stringify(payload()) });
      const items = editingModel ? catalog.items.map((item) => item.id === updated.id ? updated : isDefault ? { ...item, is_default: false } : item) : [...catalog.items.map((item) => isDefault ? { ...item, is_default: false } : item), updated];
      setCatalog({ configured: items.some((item) => item.enabled), default_model: items.find((item) => item.is_default)?.model_name || null, items });
      setMessageTone("success");
      setMessage(editingModel ? "模型配置已更新。" : "模型已新增并可以在对话和 Skill 任务中选择。");
      setEditingModel(null); setApiKey(""); setEditorOpen(false);
    } catch (reason) { setMessageTone("error"); setMessage(reason instanceof Error ? reason.message : "模型配置保存失败"); }
    finally { setBusyAction(""); }
  }

  async function makeDefault(item: ModelConnectionItem) {
    setBusyAction("save"); setMessage(""); setMessageTone("");
    try {
      const updated = await api<ModelConnectionItem>(`/super-admin/models/${item.id}/default`, { method: "POST" });
      setCatalog({ configured: true, default_model: updated.model_name, items: catalog.items.map((current) => ({ ...current, is_default: current.id === updated.id })) });
      setMessageTone("success"); setMessage(`${updated.model_name} 已设为平台默认模型。`);
    } catch (reason) { setMessageTone("error"); setMessage(reason instanceof Error ? reason.message : "设置默认模型失败"); }
    finally { setBusyAction(""); }
  }

  async function removeModel(item: ModelConnectionItem) {
    if (!window.confirm(`确认删除模型“${item.model_name}”？`)) return;
    setBusyAction("save"); setMessage("");
    try {
      await api<void>(`/super-admin/models/${item.id}`, { method: "DELETE" });
      const items = catalog.items.filter((current) => current.id !== item.id);
      setCatalog({ configured: items.some((current) => current.enabled), default_model: items.find((current) => current.is_default)?.model_name || items.find((current) => current.enabled)?.model_name || null, items });
      if (editingModel?.id === item.id) { setEditorOpen(false); setEditingModel(null); }
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "删除模型失败"); }
    finally { setBusyAction(""); }
  }

  return <>
    <Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: "平台设置" }]} />
    <div className="workspace-section-head"><div><h1>模型</h1><p>{catalog.items.length} 个模型连接</p></div><button className="button primary compact" type="button" onClick={() => resetEditor(null)}><Plus />新增模型</button></div>
    {loading ? <div className="detail-loading compact" /> : catalog.items.length ? <section className="model-compact-list">{catalog.items.map((item) => <article key={item.id}>
      <div className="model-compact-identity"><span><Workflow /></span><div><strong>{item.model_name}</strong><small>{item.base_url}</small></div></div>
      <b className={item.enabled ? item.is_default ? "default" : "available" : "disabled"}>{item.is_default ? "默认" : item.enabled ? "可用" : "停用"}</b>
      <div className="model-row-actions"><button type="button" onClick={() => void testConnection(item)}><Zap />测试</button><button type="button" onClick={() => resetEditor(item)}><PencilLine />编辑</button>{!item.is_default && item.enabled && <button type="button" onClick={() => void makeDefault(item)}><Check />设为默认</button>}<button className="danger" type="button" onClick={() => void removeModel(item)}><Trash2 />删除</button></div>
    </article>)}</section> : <EmptyState icon={AlertTriangle} title="还没有模型" description="新增模型连接后，即可用于对话、Skill 任务和 API 调用。" action={<button className="button primary" type="button" onClick={() => resetEditor(null)}>新增模型</button>} />}

    {message && !editorOpen && <div className={`model-runtime-message ${messageTone}`} aria-live="polite">{messageTone === "success" ? <Check /> : <AlertTriangle />}<span>{message}</span></div>}

    {editorOpen && createPortal(<div className="model-editor-layer">
      <button className="model-editor-backdrop" type="button" aria-label="关闭模型配置" onClick={closeEditor} />
      <aside className="model-editor-drawer" role="dialog" aria-modal="true" aria-label={editingModel ? `编辑模型 ${editingModel.model_name}` : "新增模型"}>
        <header className="model-editor-drawer-head"><div><span className="eyebrow">{editingModel ? "EDIT MODEL" : "ADD MODEL"}</span><strong>{editingModel ? `编辑 ${editingModel.model_name}` : "新增模型"}</strong><small>独立配置连接地址、密钥和运行参数</small></div><button className="model-editor-close" type="button" aria-label="关闭配置编辑器" onClick={closeEditor}><X /></button></header>
        <div className="model-editor-drawer-body">
          <form className="model-settings-panel" onSubmit={saveConfig}>
            <div className="model-settings-intro"><h2>连接信息</h2><p>保存后，模型会立即出现在对话与 Skill 任务的模型选择器中。</p></div>
            <div className="model-settings-form">
              <label>模型名称<input autoFocus required value={modelName} placeholder="例如 deepseek-v4-flash" onChange={(event) => setModelName(event.target.value)} /><small>需要与模型服务实际支持的 model 参数一致。</small></label>
              <label>Base URL<input type="url" required value={baseUrl} placeholder="https://api.example.com/v1" onChange={(event) => setBaseUrl(event.target.value)} /></label>
              <label className="model-field-wide">API Key<input type="password" value={apiKey} autoComplete="new-password" placeholder={editingModel?.api_key_configured ? "已保存；留空保持不变" : "输入该模型的服务密钥"} onChange={(event) => setApiKey(event.target.value)} /><small>仅保存在服务端，页面不会回显原文。</small></label>
              <label>超时时间（秒）<input type="number" min={5} max={600} value={timeoutSeconds} onChange={(event) => setTimeoutSeconds(Number(event.target.value))} /></label>
              <label>Temperature<input type="number" min={0} max={2} step={0.1} value={temperature} onChange={(event) => setTemperature(Number(event.target.value))} /></label>
            </div>
            <div className="model-option-row">
              <label><input type="checkbox" checked={nativeTools} onChange={(event) => setNativeTools(event.target.checked)} /><span>工具调用</span></label>
              <label><input type="checkbox" checked={isDefault} onChange={(event) => setIsDefault(event.target.checked)} /><span>设为平台默认</span></label>
            </div>
            {message && <div className={`model-runtime-message model-editor-message ${messageTone}`} aria-live="polite">{messageTone === "success" ? <Check /> : <AlertTriangle />}<span>{message}</span></div>}
            <div className="model-settings-actions"><span>{editingModel ? "保存后只更新当前模型" : "保存后加入现有模型列表"}</span><div><button className="button secondary" type="button" disabled={Boolean(busyAction) || !baseUrl || !modelName} onClick={() => void testConnection()}>{busyAction === "test" ? <RotateCw className="spin-icon" /> : <Zap />}测试连接</button><button className="button primary" type="submit" disabled={Boolean(busyAction) || !baseUrl || !modelName}>{busyAction === "save" ? <RotateCw className="spin-icon" /> : <Save />}{editingModel ? "保存修改" : "新增模型"}</button></div></div>
          </form>
          <aside className="model-edit-guide"><p><ShieldCheck />API Key 只在服务端保存；点击“测试连接”可在保存前确认配置是否可用。</p></aside>
        </div>
      </aside>
    </div>, document.body)}
  </>;
}

export function ComingSoonPage({ kind }: { kind: "runs" | "endpoints" | "credentials" }) {
  const copy = { runs: ["运行记录", "该模块尚未开放。"], endpoints: ["API Endpoint", "该模块尚未开放。"], credentials: ["连接与密钥", "该模块将在工具网关接入后开放，目前不会向 Skill 暴露外部凭据。"] }[kind];
  return <><Breadcrumbs items={[{ label: "工作台", to: "/app" }, { label: copy[0] }]} /><PageTitle eyebrow="规划中" title={copy[0]} description={copy[1]} /><EmptyState icon={kind === "endpoints" ? Code2 : kind === "credentials" ? KeyRound : Workflow} title="当前版本暂不可用" description="为避免误导，这个模块不会作为现有业务能力展示；完成权限和安全设计后再开放。" /></>;
}
