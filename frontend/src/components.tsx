import {
  Boxes,
  ChevronRight,
  CircleUserRound,
  Code2,
  Compass,
  FileArchive,
  Gauge,
  Heart,
  House,
  KeyRound,
  LayoutDashboard,
  Library,
  LogOut,
  Menu,
  Network,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  Users,
  Workflow,
  X
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useNavigate } from "./router";
import { useAuth } from "./auth";
import type { Role, Skill, VersionStatus, Visibility } from "./types";

export const icons = {
  Boxes,
  Code2,
  Compass,
  FileArchive,
  Gauge,
  Heart,
  KeyRound,
  LayoutDashboard,
  Library,
  Network,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  UploadCloud,
  Users,
  Workflow
};

export function Brand({ to = "/" }: { to?: string }) {
  return (
    <Link to={to} className="brand" aria-label={to === "/app" ? "SkillGo 工作台概览" : "SkillGo Skill 市场"}>
      <span className="brand-mark"><img src="/skillgo-logo.png" alt="" /></span>
      <span>Skill<span>Go</span></span>
    </Link>
  );
}

export function PublicHeader() {
  const { user } = useAuth();
  return (
    <header className="public-header">
      <Brand />
      <nav aria-label="主导航">
        <NavLink to="/">Skill 市场</NavLink>
        <a href="/api/docs">API 文档</a>
      </nav>
      <div className="header-actions">
        {user ? (
          <Link className="button primary compact" to="/app">进入工作台 <ChevronRight size={16} /></Link>
        ) : (
          <>
            <Link className="text-link" to="/login">登录</Link>
            <Link className="button primary compact" to="/register">创建账号</Link>
          </>
        )}
      </div>
    </header>
  );
}

export const roleLabels: Record<Role, string> = {
  super_admin: "超级管理员",
  admin: "管理员",
  user: "普通用户"
};

export const visibilityLabels: Record<Visibility, string> = {
  private: "私有",
  unlisted: "链接分享",
  internal: "实例内部",
  public: "公开社区"
};

export const skillTypeLabels = {
  instruction: "指令型",
  code: "代码型"
} as const;

const categoryLabels: Record<string, string> = {
  productivity: "效率工具",
  writing: "内容写作",
  document: "文档处理",
  development: "研发工程",
  data: "数据分析",
  other: "其他"
};

export function categoryLabel(category: string) {
  return categoryLabels[category] || category;
}

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  if (!user) return null;
  const links = [
    { to: "/app", label: "开始任务", icon: Plus, end: true },
    { to: "/app/skills", label: "我的 Skill", icon: Library, end: false },
    { to: "/app/jobs", label: "任务", icon: Workflow, end: false },
    { to: "/app/endpoints", label: "API", icon: Network, end: false }
  ];
  const adminLinks = user.role === "admin" || user.role === "super_admin"
    ? [
        { to: "/admin/reviews", label: "发布审核", icon: ShieldCheck, end: false },
        { to: "/admin/users", label: "用户管理", icon: Users, end: false }
      ]
    : [];
  const superLinks = user.role === "super_admin"
    ? [{ to: "/super/system", label: "平台设置", icon: Settings2, end: false }]
    : [];

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#skillgo-main">跳到主要内容</a>
      <button className="mobile-menu" onClick={() => setOpen(!open)} aria-controls="skillgo-sidebar" aria-expanded={open} aria-label={open ? "关闭工作台导航" : "打开工作台导航"}>
        {open ? <X /> : <Menu />}
      </button>
      {open && <button className="sidebar-backdrop" type="button" aria-label="关闭工作台导航" onClick={() => setOpen(false)} />}
      <aside id="skillgo-sidebar" className={open ? "sidebar open" : "sidebar"}>
        <Brand to="/app" />
        <Link className="sidebar-home-link" to="/" onClick={() => setOpen(false)} aria-label="发现社区 Skill"><span className="sidebar-home-icon"><Compass size={16} /></span><strong>发现 Skill</strong><ChevronRight size={14} /></Link>
        <div className="sidebar-section-label">工作区</div>
        <nav className="sidebar-nav">
          {[...links, ...adminLinks, ...superLinks].map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-profile">
          <CircleUserRound size={30} />
          <div><strong>{user.display_name}</strong><span>{roleLabels[user.role]}</span></div>
          <button aria-label="退出登录" onClick={() => { logout(); navigate("/"); }}><LogOut size={17} /></button>
        </div>
      </aside>
      <main id="skillgo-main" className="app-main" tabIndex={-1}>{children}</main>
    </div>
  );
}

const statusLabels: Record<VersionStatus, string> = {
  draft: "草稿",
  ready: "可提交",
  submitted: "待审核",
  reviewing: "审核中",
  rejected: "已驳回",
  published: "已发布",
  deprecated: "已弃用",
  yanked: "已下架"
};

export function StatusBadge({ status }: { status: VersionStatus | null }) {
  if (!status) return <span className="status muted">暂无版本</span>;
  return <span className={`status ${status}`}>{statusLabels[status]}</span>;
}

export function SkillCard({ skill }: { skill: Skill }) {
  return (
    <Link className="skill-card" to={`/skills/${skill.slug}`} aria-label={`查看 ${skill.name}`}>
      <div className="skill-card-top">
        <span className={`skill-icon tone-${skill.category}`}><Workflow /></span>
        <span className="category-pill">{categoryLabel(skill.category)}</span>
      </div>
      <div>
        <h3>{skill.name}</h3>
        <p>{skill.summary}</p>
      </div>
      <footer>
        <span>by {skill.owner_name || "社区作者"}</span>
        <span><Heart size={14} /> {skill.favorite_count}</span>
        {skill.latest_version && <span>v{skill.latest_version}</span>}
      </footer>
    </Link>
  );
}

export function PageTitle({ eyebrow, title, description, action }: { eyebrow?: string; title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="page-title">
      <div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{description && <p>{description}</p>}</div>
      {action}
    </div>
  );
}

export function Breadcrumbs({ items }: { items: Array<{ label: string; to?: string }> }) {
  return <nav className="workspace-breadcrumbs" aria-label="当前位置">{items.map((item, index) => <span key={`${item.label}-${index}`}>{index > 0 && <ChevronRight size={13} />}{item.to ? <Link to={item.to}>{item.label}</Link> : <b aria-current="page">{item.label}</b>}</span>)}</nav>;
}

export function EmptyState({ icon: Icon = Boxes, title, description, action }: { icon?: typeof Boxes; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span><Icon /></span><h3>{title}</h3><p>{description}</p>{action}</div>;
}
