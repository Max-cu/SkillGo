import { useState } from "react";
import { api } from "./api";
import type { WorkflowJob } from "./types";

export function WorkflowQuestion({ job, onAnswered }: { job: WorkflowJob; onAnswered: (job: WorkflowJob) => void }) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const question = job.pending_question;
  if (job.status !== "waiting_user" || !question) return null;
  return <form className="panel workflow-question" onSubmit={async (event) => {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const updated = await api<WorkflowJob>(`/jobs/${job.id}/answer`, { method: "POST", body: JSON.stringify({ question_id: question.id, answer }) });
      setAnswer(""); onAnswered(updated);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "回答保存失败"); }
    finally { setBusy(false); }
  }}>
    <strong>需要补充信息</strong>
    <p>{question.question}</p>
    <label>你的回答<textarea required maxLength={8000} value={answer} onChange={(event) => setAnswer(event.target.value)} disabled={busy} /></label>
    <small>回答会与原始输入一起用于新一轮执行。</small>
    {error && <p role="alert" className="form-error">{error}</p>}
    <button className="button primary" disabled={busy || !answer.trim()}>{busy ? "正在保存…" : "提交并继续"}</button>
  </form>;
}
