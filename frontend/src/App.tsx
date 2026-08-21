import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { Route, Switch } from "wouter";
import { AppShell } from "./components";
import { useAuth } from "./auth";
import { Navigate } from "./router";
import { useLocation } from "wouter";
import type { Role } from "./types";

const DashboardPage = lazy(() => import("./DashboardPage").then((module) => ({ default: module.DashboardPage })));
const MarketplacePage = lazy(() => import("./pages").then((module) => ({ default: module.MarketplacePage })));
const SkillDetailPage = lazy(() => import("./pages").then((module) => ({ default: module.SkillDetailPage })));
const AuthPage = lazy(() => import("./pages").then((module) => ({ default: module.AuthPage })));
const MySkillsPage = lazy(() => import("./pages").then((module) => ({ default: module.MySkillsPage })));
const NewSkillPage = lazy(() => import("./pages").then((module) => ({ default: module.NewSkillPage })));
const WorkflowPage = lazy(() => import("./pages").then((module) => ({ default: module.WorkflowPage })));
const RunSkillPage = lazy(() => import("./pages").then((module) => ({ default: module.RunSkillPage })));
const ManageSkillPage = lazy(() => import("./pages").then((module) => ({ default: module.ManageSkillPage })));
const WorkflowJobsPage = lazy(() => import("./pages").then((module) => ({ default: module.WorkflowJobsPage })));
const EndpointsPage = lazy(() => import("./pages").then((module) => ({ default: module.EndpointsPage })));
const ComingSoonPage = lazy(() => import("./pages").then((module) => ({ default: module.ComingSoonPage })));
const AdminReviewsPage = lazy(() => import("./pages").then((module) => ({ default: module.AdminReviewsPage })));
const AdminUsersPage = lazy(() => import("./pages").then((module) => ({ default: module.AdminUsersPage })));
const ModelSettingsPage = lazy(() => import("./pages").then((module) => ({ default: module.ModelSettingsPage })));

function PageLoading() {
  return <div className="boot-screen"><span /><p>正在加载…</p></div>;
}

function WorkspaceLoading() {
  return <div className="workspace-page-loading" role="status"><span /><p>正在加载页面…</p></div>;
}

function Protected({ children, roles }: { children: ReactNode; roles?: Role[] }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="boot-screen"><span /><p>正在进入 SkillGo…</p></div>;
  if (!user) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(user.role)) return <Navigate to="/app" replace />;
  return <AppShell><Suspense fallback={<WorkspaceLoading />}>{children}</Suspense></AppShell>;
}

export default function App() {
  const [location] = useLocation();
  useEffect(() => {
    const page = location === "/" ? "发现 Skill"
      : location === "/login" ? "登录"
      : location === "/register" ? "创建账号"
      : location === "/app" ? "开始任务"
      : location === "/app/skills" ? "我的 Skill"
      : location === "/app/jobs" ? "任务"
      : location === "/app/endpoints" ? "API"
      : location === "/admin/reviews" ? "发布审核"
      : location === "/admin/users" ? "用户管理"
      : location === "/super/system" ? "平台设置"
      : location.startsWith("/skills/") ? "Skill 详情"
      : location.includes("/workflow") ? "Agent 任务"
      : location.includes("/run") ? "Skill 对话"
      : location.includes("/app/skills/") ? "Skill 管理"
      : "SkillGo";
    document.title = `${page} · SkillGo`;
  }, [location]);
  return <Suspense fallback={<PageLoading />}><Switch>
    <Route path="/"><MarketplacePage /></Route>
    <Route path="/skills/:slug"><SkillDetailPage /></Route>
    <Route path="/login"><AuthPage mode="login" /></Route>
    <Route path="/register"><AuthPage mode="register" /></Route>
    <Route path="/app"><Protected><DashboardPage /></Protected></Route>
    <Route path="/app/skills"><Protected><MySkillsPage /></Protected></Route>
    <Route path="/app/skills/new"><Protected><NewSkillPage /></Protected></Route>
    <Route path="/app/skills/:skillId/workflow"><Protected><WorkflowPage /></Protected></Route>
    <Route path="/app/skills/:skillId/run"><Protected><RunSkillPage /></Protected></Route>
    <Route path="/app/skills/:skillId"><Protected><ManageSkillPage /></Protected></Route>
    <Route path="/app/jobs"><Protected><WorkflowJobsPage /></Protected></Route>
    <Route path="/app/runs"><Protected><Navigate to="/app/jobs" replace /></Protected></Route>
    <Route path="/app/endpoints"><Protected><EndpointsPage /></Protected></Route>
    <Route path="/app/credentials"><Protected><ComingSoonPage kind="credentials" /></Protected></Route>
    <Route path="/admin/reviews"><Protected roles={["admin", "super_admin"]}><AdminReviewsPage /></Protected></Route>
    <Route path="/admin/users"><Protected roles={["admin", "super_admin"]}><AdminUsersPage /></Protected></Route>
    <Route path="/super/system"><Protected roles={["admin", "super_admin"]}><ModelSettingsPage /></Protected></Route>
    <Route path="/:rest*"><Navigate to="/" replace /></Route>
  </Switch></Suspense>;
}
