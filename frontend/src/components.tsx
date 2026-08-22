import {
  Boxes,
  ChevronLeft,
  ChevronRight,
  CircleUserRound,
  Code2,
  Compass,
  FileArchive,
  Gauge,
  Heart,
  HardDrive,
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
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useLocation } from "wouter";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { Link, NavLink, useNavigate } from "./router";
import { useAuth } from "./auth";
import type { Role, Skill, VersionStatus, Visibility } from "./types";

gsap.registerPlugin(useGSAP);

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
  const navigate = useNavigate();
  function enterWorkspace(event: ReactMouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    navigate("/app");
    window.setTimeout(() => {
      if (window.location.pathname !== "/app") window.location.assign("/app");
    }, 0);
  }
  return (
    <header className="public-header">
      <Brand />
      <div className="header-actions">
        {user ? (
          <a className="button primary compact public-workspace-entry" href="/app" onClick={enterWorkspace}>进入工作台 <ChevronRight size={16} /></a>
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

export function TiltSurface({ children, className = "" }: { children: ReactNode; className?: string }) {
  const surfaceRef = useRef<HTMLDivElement>(null);
  const rotateX = useRef<((value: number) => void) | null>(null);
  const rotateY = useRef<((value: number) => void) | null>(null);

  useGSAP(() => {
    const surface = surfaceRef.current;
    if (!surface || window.matchMedia("(prefers-reduced-motion: reduce), (pointer: coarse)").matches) return;
    gsap.set(surface, { transformPerspective: 900, transformOrigin: "center" });
    rotateX.current = gsap.quickTo(surface, "rotationX", { duration: 0.32, ease: "power2.out" });
    rotateY.current = gsap.quickTo(surface, "rotationY", { duration: 0.32, ease: "power2.out" });
  }, { scope: surfaceRef });

  function move(event: ReactPointerEvent<HTMLDivElement>) {
    if (!rotateX.current || !rotateY.current) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - bounds.left) / bounds.width - 0.5;
    const y = (event.clientY - bounds.top) / bounds.height - 0.5;
    rotateX.current(-y * 5);
    rotateY.current(x * 6);
  }

  function reset() {
    rotateX.current?.(0);
    rotateY.current?.(0);
  }

  return <div ref={surfaceRef} className={`tilt-surface ${className}`.trim()} onPointerMove={move} onPointerLeave={reset} onPointerCancel={reset}>{children}</div>;
}

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [location] = useLocation();
  const [open, setOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => window.localStorage.getItem("skillgo-sidebar-collapsed") === "true");
  const pageRef = useRef<HTMLDivElement>(null);

  useGSAP(() => {
    if (location === "/app" || window.matchMedia("(prefers-reduced-motion: reduce)").matches || !pageRef.current) return;
    gsap.fromTo(pageRef.current, { y: 10, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.38, ease: "power2.out", clearProps: "transform,opacity,visibility" });
  }, { scope: pageRef, dependencies: [location], revertOnUpdate: true });

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    window.localStorage.setItem("skillgo-sidebar-collapsed", String(collapsed));
  }, [collapsed]);

  if (!user) return null;
  const links = [
    { to: "/app", label: "开始任务", icon: Plus, end: true },
    { to: "/app/skills", label: "Skill 管理", icon: Library, end: false },
    { to: "/app/jobs", label: "运行记录", icon: Workflow, end: false },
    { to: "/app/endpoints", label: "API 接入", icon: Network, end: false }
  ];
  const adminLinks = user.role === "admin" || user.role === "super_admin"
    ? [
        { to: "/admin/reviews", label: "发布审核", icon: ShieldCheck, end: false },
        { to: "/admin/users", label: "用户管理", icon: Users, end: false },
        { to: "/admin/storage", label: "存储管理", icon: HardDrive, end: false }
      ]
    : [];
  const superLinks = user.role === "admin" || user.role === "super_admin"
    ? [{ to: "/super/system", label: "平台设置", icon: Settings2, end: false }]
    : [];

  return (
    <div className={`app-shell${collapsed ? " sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#skillgo-main">跳到主要内容</a>
      <button className="mobile-menu" onClick={() => setOpen(!open)} aria-controls="skillgo-sidebar" aria-expanded={open} aria-label={open ? "关闭工作台导航" : "打开工作台导航"}>
        {open ? <X /> : <Menu />}
      </button>
      {open && <button className="sidebar-backdrop" type="button" aria-label="关闭工作台导航" onClick={() => setOpen(false)} />}
      <aside id="skillgo-sidebar" className={`${open ? "sidebar open" : "sidebar"}${collapsed ? " collapsed" : ""}`}>
        <Brand to="/app" />
        <button className="sidebar-collapse" type="button" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"} title={collapsed ? "展开侧边栏" : "收起侧边栏"}><ChevronLeft /></button>
        <Link className="sidebar-home-link" to="/" onClick={() => setOpen(false)} aria-label="进入 Skill 社区" title={collapsed ? "Skill 社区" : undefined}><span className="sidebar-home-icon"><Compass size={16} /></span><strong>Skill 社区</strong><ChevronRight size={14} /></Link>
        <div className="sidebar-section-label">工作台</div>
        <nav className="sidebar-nav">
          {links.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)} title={collapsed ? label : undefined}>
              <Icon size={18} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        {(adminLinks.length > 0 || superLinks.length > 0) && <>
          <div className="sidebar-section-label sidebar-management-label">平台管理</div>
          <nav className="sidebar-nav">
            {[...adminLinks, ...superLinks].map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)} title={collapsed ? label : undefined}>
                <Icon size={18} /><span>{label}</span>
              </NavLink>
            ))}
          </nav>
        </>}
        <div className="sidebar-profile">
          <CircleUserRound size={30} />
          <div><strong>{user.display_name}</strong><span>{roleLabels[user.role]}</span></div>
          <button className="sidebar-logout" aria-label="退出登录" onClick={() => { logout(); navigate("/"); }}><LogOut size={17} /></button>
        </div>
      </aside>
      <main id="skillgo-main" className="app-main" tabIndex={-1}><div className="app-page-transition" ref={pageRef}>{children}</div></main>
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
    <TiltSurface className="skill-card-tilt"><Link className="skill-card" to={`/skills/${skill.slug}`} aria-label={`查看 ${skill.name}`}>
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
    </Link></TiltSurface>
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
