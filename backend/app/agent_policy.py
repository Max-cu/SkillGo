from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "skipped"})
OBSERVATION_TOOLS = frozenset({"list_files", "read_file"})
WORKSPACE_MUTATING_TOOLS = frozenset({"write_file", "command", "run_python"})


def action_fingerprint(action: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint without presentation-only fields."""

    normalized = {
        key: value
        for key, value in action.items()
        if key not in {"reason", "timeout_seconds"}
    }
    if normalized.get("action") == "run_python" and isinstance(normalized.get("code"), str):
        normalized["code_sha256"] = hashlib.sha256(
            normalized.pop("code").encode("utf-8")
        ).hexdigest()
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_succeeded(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    if payload.get("ok") is False:
        return False
    exit_code = payload.get("exit_code")
    return not isinstance(exit_code, int) or exit_code == 0


def _compact_observation(action: dict[str, Any], payload: object) -> dict[str, Any]:
    item: dict[str, Any] = {
        "tool": str(action.get("action") or "unknown"),
        "ok": _payload_succeeded(payload),
    }
    for key in ("path", "cwd"):
        value = action.get(key)
        if isinstance(value, str) and value:
            item[key] = value[:300]
    if action.get("action") == "command":
        item["argv"] = [str(value)[:120] for value in (action.get("argv") or [])[:6]]
    if isinstance(payload, dict):
        for key in ("exit_code", "error_code", "path", "bytes", "full_result_path"):
            value = payload.get(key)
            if isinstance(value, (str, int, bool)):
                item[key] = value
        if not item["ok"]:
            message = payload.get("message") or payload.get("stderr")
            if isinstance(message, str) and message:
                item["diagnostic"] = message[:320]
        else:
            for key in ("stdout", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value.strip()[:1800]
                    break
    elif isinstance(payload, str) and payload.strip():
        item["result_excerpt"] = payload.strip()[:1800]
    return item


@dataclass
class AgentExecutionState:
    """Small trusted state machine around a model-driven Skill execution."""

    skill_count: int
    plan_required: bool = True
    ordered_skills: bool = False
    plan: dict[str, Any] | None = None
    validation_step_id: str | None = None
    validation: dict[str, Any] | None = None
    validation_failures: int = 0
    loaded_skills: set[int] = field(default_factory=set)
    completed_skill_indexes: set[int] = field(default_factory=set)
    observations: list[dict[str, Any]] = field(default_factory=list)
    action_counts: dict[str, int] = field(default_factory=dict)
    mutation_epoch: int = 0
    _observation_cache: dict[tuple[int, str], object] = field(default_factory=dict)

    def read_skill(self, index: int, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        if index < 1 or index > len(contexts):
            return {
                "ok": False,
                "error_code": "SKILL_INDEX_INVALID",
                "message": f"skill_index must be between 1 and {len(contexts)}",
            }
        if self.ordered_skills and index not in self.loaded_skills:
            expected = len(self.loaded_skills) + 1
            if index != expected:
                return {
                    "ok": False,
                    "error_code": "SKILL_ORDER_INVALID",
                    "message": f"Explicit Skill routing requires loading index {expected} before index {index}.",
                }
        context = contexts[index - 1]
        self.loaded_skills.add(index)
        return {
            "ok": True,
            "skill_index": index,
            "name": context["name"],
            "version": context["version"],
            "root": context["root"],
            "runtime_requirements": context.get("runtime_requirements") or {},
            "skill_md": context["skill_md"],
        }

    def update_plan(self, action: dict[str, Any]) -> dict[str, Any]:
        goal = str(action.get("goal") or "").strip()
        raw_steps = action.get("steps")
        success_criteria = action.get("success_criteria")
        validation_step_id = str(action.get("validation_step_id") or "").strip()[:40]
        if not goal or len(goal) > 800:
            return {"ok": False, "error_code": "PLAN_INVALID", "message": "goal must contain 1-800 characters"}
        if not isinstance(raw_steps, list) or not 2 <= len(raw_steps) <= 8:
            return {"ok": False, "error_code": "PLAN_INVALID", "message": "steps must contain 2-8 items"}
        if not isinstance(success_criteria, list) or not 1 <= len(success_criteria) <= 8:
            return {"ok": False, "error_code": "PLAN_INVALID", "message": "success_criteria must contain 1-8 items"}

        steps: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        in_progress = 0
        for position, raw_step in enumerate(raw_steps, 1):
            if not isinstance(raw_step, dict):
                return {"ok": False, "error_code": "PLAN_INVALID", "message": f"step {position} must be an object"}
            step_id = str(raw_step.get("id") or position).strip()[:40]
            title = str(raw_step.get("title") or "").strip()[:300]
            status = str(raw_step.get("status") or "pending").strip()
            evidence = str(raw_step.get("evidence") or "").strip()[:800]
            if not step_id or step_id in seen_ids or not title or status not in PLAN_STATUSES:
                return {"ok": False, "error_code": "PLAN_INVALID", "message": f"step {position} has an invalid id, title, or status"}
            if status in {"completed", "skipped"} and not evidence:
                return {"ok": False, "error_code": "PLAN_EVIDENCE_REQUIRED", "message": f"step {step_id} requires evidence when {status}"}
            if status == "in_progress":
                in_progress += 1
            seen_ids.add(step_id)
            steps.append({"id": step_id, "title": title, "status": status, "evidence": evidence})
        if in_progress > 1:
            return {"ok": False, "error_code": "PLAN_INVALID", "message": "at most one plan step may be in_progress"}
        if validation_step_id not in seen_ids:
            return {
                "ok": False,
                "error_code": "PLAN_VALIDATION_STEP_REQUIRED",
                "message": "validation_step_id must reference one plan step dedicated to final verification",
            }
        criteria = [str(value).strip()[:400] for value in success_criteria if str(value).strip()]
        if not criteria:
            return {"ok": False, "error_code": "PLAN_INVALID", "message": "success_criteria cannot be empty"}
        self.plan = {"goal": goal, "steps": steps, "success_criteria": criteria}
        self.validation_step_id = validation_step_id
        return {"ok": True, "plan": deepcopy(self.plan), "validation_step_id": validation_step_id}

    def complete_skill(self, index: int, evidence: str) -> dict[str, Any]:
        evidence = evidence.strip()
        if index not in self.loaded_skills:
            return {"ok": False, "error_code": "SKILL_NOT_LOADED", "message": f"Read Skill {index} before completing its phase."}
        if self.ordered_skills:
            expected = len(self.completed_skill_indexes) + 1
            if index != expected:
                return {"ok": False, "error_code": "SKILL_ORDER_INVALID", "message": f"Complete Skill {expected} before Skill {index}."}
        if not evidence or len(evidence) > 1000:
            return {"ok": False, "error_code": "SKILL_EVIDENCE_REQUIRED", "message": "evidence must contain 1-1000 characters"}
        self.completed_skill_indexes.add(index)
        return {"ok": True, "skill_index": index, "evidence": evidence}

    def record_validation(self, action: dict[str, Any]) -> dict[str, Any]:
        status = str(action.get("status") or "").strip().lower()
        summary = str(action.get("summary") or "").strip()
        evidence = str(action.get("evidence") or "").strip()
        checks = action.get("checks")
        if status not in {"passed", "failed"} or not summary or not evidence:
            return {
                "ok": False,
                "error_code": "VALIDATION_INVALID",
                "message": "status, summary, and evidence are required",
            }
        if not isinstance(checks, list) or not 1 <= len(checks) <= 20 or not all(
            isinstance(item, str) and item.strip() for item in checks
        ):
            return {
                "ok": False,
                "error_code": "VALIDATION_INVALID",
                "message": "checks must contain 1-20 non-empty observed results",
            }
        recent_proof = next(
            (
                item
                for item in reversed(self.observations)
                if item.get("ok") and item.get("tool") in {"command", "run_python", "read_file"}
            ),
            None,
        )
        if recent_proof is None:
            return {
                "ok": False,
                "error_code": "VALIDATION_EVIDENCE_MISSING",
                "message": "Run or read a real verifier result before recording validation",
            }
        normalized = {
            "status": status,
            "summary": summary[:1000],
            "evidence": evidence[:1000],
            "checks": [str(item).strip()[:300] for item in checks],
            "mutation_epoch": self.mutation_epoch,
        }
        if status == "failed":
            self.validation = None
            self.validation_failures += 1
            return {
                "ok": False,
                "error_code": "SKILL_VALIDATION_FAILED",
                "message": summary[:1000],
                "checks": normalized["checks"],
                "retry_allowed": self.validation_failures <= 2,
                "failure_number": self.validation_failures,
            }
        self.validation = normalized
        return {"ok": True, "validation": deepcopy(normalized)}

    def block_workflow(self, summary: str, evidence: str) -> dict[str, Any]:
        """Allow an honest blocked outcome only after a real failed operation."""

        summary = summary.strip()
        evidence = evidence.strip()
        if not summary or not evidence:
            return {
                "ok": False,
                "error_code": "BLOCK_EVIDENCE_REQUIRED",
                "message": "summary and evidence are required for a blocked outcome",
            }
        recent_failure = next(
            (
                item
                for item in reversed(self.observations)
                if not item.get("ok") and item.get("tool") in {"command", "run_python", "read_file"}
            ),
            None,
        )
        if recent_failure is None:
            return {
                "ok": False,
                "error_code": "BLOCK_EVIDENCE_MISSING",
                "message": "Run a real operation that proves the blocking condition before reporting blocked",
            }
        return {
            "ok": True,
            "summary": summary[:1200],
            "evidence": evidence[:1200],
            "observation": deepcopy(recent_failure),
        }

    def finish_blocker(self) -> str | None:
        if self.plan_required and self.plan is None:
            return "Create a concise execution plan before finish."
        unread = [index for index in range(1, self.skill_count + 1) if index not in self.loaded_skills]
        if unread:
            return f"Read every selected Skill before finish; unread skill indexes: {unread}."
        incomplete_skills = [
            index for index in range(1, self.skill_count + 1)
            if index not in self.completed_skill_indexes
        ]
        if incomplete_skills:
            return f"Complete every selected Skill phase before finish; incomplete skill indexes: {incomplete_skills}."
        if self.plan:
            incomplete = [
                step["id"] for step in self.plan["steps"]
                if step["status"] not in {"completed", "skipped"}
            ]
            if incomplete:
                return f"Update the plan before finish; incomplete step ids: {incomplete}."
        if self.validation is None or self.validation.get("mutation_epoch") != self.mutation_epoch:
            return "Run one concentrated final verification on the current artifacts and record_validation before finish."
        return None

    def cached_observation(self, action: dict[str, Any]) -> object | None:
        if action.get("action") not in OBSERVATION_TOOLS:
            return None
        value = self._observation_cache.get((self.mutation_epoch, action_fingerprint(action)))
        if value is None:
            return None
        cached = deepcopy(value)
        if isinstance(cached, dict):
            cached["cached"] = True
            cached["hint"] = "Reused a prior observation because the workspace has not changed."
        return cached

    def record(self, action: dict[str, Any], payload: object) -> None:
        fingerprint = action_fingerprint(action)
        self.action_counts[fingerprint] = self.action_counts.get(fingerprint, 0) + 1
        succeeded = _payload_succeeded(payload)
        if action.get("action") in OBSERVATION_TOOLS and succeeded:
            self._observation_cache[(self.mutation_epoch, fingerprint)] = deepcopy(payload)
        elif action.get("action") in WORKSPACE_MUTATING_TOOLS and succeeded:
            self.mutation_epoch += 1
            self.validation = None
        self.observations.append(_compact_observation(action, payload))
        self.observations = self.observations[-24:]

    def repeated_count(self, action: dict[str, Any]) -> int:
        return self.action_counts.get(action_fingerprint(action), 0)

    def checkpoint(self) -> str:
        snapshot = {
            "plan": self.plan,
            "validation_step_id": self.validation_step_id,
            "validation": self.validation,
            "loaded_skill_indexes": sorted(self.loaded_skills),
            "completed_skill_indexes": sorted(self.completed_skill_indexes),
            "recent_observations": self.observations[-10:],
        }
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
