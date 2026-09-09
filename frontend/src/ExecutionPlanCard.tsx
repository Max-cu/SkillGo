import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, ListChecks, Minus } from "lucide-react";
import type { WorkflowJob, WorkflowJobStatus } from "./types";

// An explicit choice survives polling and status changes. Parents key by job ID.
export function RunDisclosure({ className, heading, defaultOpen = false, open: controlledOpen, onOpenChange, children }: {
  className: string; heading: ReactNode; defaultOpen?: boolean; children: ReactNode;
  open?: boolean; onOpenChange?: (open: boolean) => void;
}) {
  const [manualOpen, setManualOpen] = useState<boolean>();
  const open = controlledOpen ?? manualOpen ?? defaultOpen;
  const id = useId();
  return <section className={`${className} run-disclosure`} data-open={open}>
    <button type="button" className="run-disclosure-trigger" aria-expanded={open} aria-controls={id}
      onClick={() => { setManualOpen(!open); onOpenChange?.(!open); }}>
      {heading}<ChevronDown className="run-disclosure-chevron" aria-hidden="true" />
    </button>
    <div id={id} className="run-disclosure-body" hidden={!open}>{children}</div>
  </section>;
}

function Evidence({ children }: { children: string }) {
  const [open, setOpen] = useState(false);
  const [clipped, setClipped] = useState(false);
  const paragraph = useRef<HTMLParagraphElement>(null);
  const id = useId();
  useEffect(() => {
    const element = paragraph.current;
    if (!element) return;
    const measure = () => { if (!open) setClipped(element.scrollHeight > element.clientHeight + 1); };
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, [children, open]);
  return <div className="run-plan-evidence">
    <p ref={paragraph} id={id} className={open ? "" : "is-clamped"}>{children}</p>
    {(open || clipped) && <button type="button" aria-expanded={open} aria-controls={id}
      onClick={() => setOpen(!open)}>{open ? "收起详情" : "查看详情"}</button>}
  </div>;
}

export function ExecutionPlanCard({ plan, status }: {
  plan: NonNullable<WorkflowJob["execution_plan"]>; status: WorkflowJobStatus;
}) {
  const completed = plan.steps.filter(step => step.status === "completed").length;
  const skipped = plan.steps.filter(step => step.status === "skipped").length;
  const running = ["created", "preparing", "queued", "running", "producing_artifacts", "verifying"].includes(status);
  const labels: Record<string, string> = { pending: "待执行", in_progress: running ? "执行中" : "未完成", completed: "已完成", skipped: "已跳过" };
  return <RunDisclosure className="run-plan-card" defaultOpen={status !== "succeeded"}
    heading={<><span className="run-plan-title"><ListChecks aria-hidden="true" />执行计划</span>
      <span className="run-plan-count">{skipped ? `${completed} 步完成 · ${skipped} 步跳过` : `已完成 ${completed} / ${plan.steps.length} 步`}</span></>}>
    <p className="run-plan-goal">{plan.goal}</p>
    <ol className="run-plan-steps">
      {plan.steps.map((step, index) => <li key={step.id} className={`run-plan-step is-${step.status} ${running ? "" : "is-inactive"}`}>
        <span className="run-plan-rail" aria-hidden="true"><span className="run-plan-node">
          {step.status === "completed" ? <Check /> : step.status === "skipped" ? <Minus /> : step.status === "in_progress" && running ? <i /> : index + 1}
        </span></span>
        <div className="run-plan-content">
          <div className="run-plan-step-heading"><strong>{step.title}</strong><span>{labels[step.status] || step.status}</span></div>
          {step.evidence && <Evidence>{step.evidence}</Evidence>}
        </div>
      </li>)}
    </ol>
  </RunDisclosure>;
}
