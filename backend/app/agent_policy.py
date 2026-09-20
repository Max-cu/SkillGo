from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable


PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "skipped"})
OBSERVATION_TOOLS = frozenset({"list_files", "read_file"})
WORKSPACE_MUTATING_TOOLS = frozenset({"write_file", "command", "run_python"})

# Immutable instruction material (Skill package files, user inputs) is read
# once and pinned in the execution-state memory message, so the model never
# has to re-read a spec after old exchanges leave the context window.
# 20 KiB per file covers every observed Skill reference doc (largest 18.6 KiB);
# the shelf total (60 KiB) is projected into the model context greedily and
# oldest entries are dropped first when the input budget is tight.
REFERENCE_ENTRY_CAP = 20 * 1024
REFERENCE_SHELF_CAP = 60 * 1024
REFERENCE_SHELF_MAX_ENTRIES = 15


def is_reference_path(path: str, roots: Iterable[str]) -> bool:
    """True for task-immutable instruction/input locations."""

    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts:
        return False
    if path == "/workspace/input" or path.startswith("/workspace/input/"):
        return True
    for root in roots:
        if not root:
            continue
        base = root.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


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


def _compact_observation(
    action: dict[str, Any],
    payload: object,
    *,
    operation: int,
    mutation_epoch: int,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "tool": str(action.get("action") or "unknown"),
        "ok": _payload_succeeded(payload),
        "operation": operation,
        "mutation_epoch": mutation_epoch,
    }
    if tool_call_id:
        item["tool_call_id"] = tool_call_id[:160]
    for key in ("path", "cwd"):
        value = action.get(key)
        if isinstance(value, str) and value:
            item[key] = value[:300]
    if action.get("action") == "command":
        argv = action.get('argv')
        item["argv"] = [str(value)[:120] for value in argv[:6]] if isinstance(argv, list) else []
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
    progress_epoch: int = 0
    _result_signatures: dict[str, str] = field(default_factory=dict)
    verification: dict[str, Any] | None = None
    requirements: list[str] = field(default_factory=list)
    skill_evidence: dict[int, str] = field(default_factory=dict)
    step_artifacts: dict[str, dict[str, str]] = field(default_factory=dict)
    _observation_cache: dict[tuple[int, str], object] = field(default_factory=dict)
    # path -> {"sha256", "bytes", "chars", "text"}; plain JSON dict so it is
    # carried by durable snapshots and pinned through context projection.
    reference_shelf: dict[str, dict[str, Any]] = field(default_factory=dict)

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
            "execution_mode": "fixed" if context.get("fixed_execution") else "agent",
            "entrypoint": list(context['fixed_execution'].entrypoint) if context.get('fixed_execution') else None,
            "skill_md": context["skill_md"],
        }

    def reference_read(self, path: str, text: str, roots: Iterable[str]) -> dict[str, Any] | None:
        """Build the read_file payload for an immutable Skill/input reference.

        Returns ``None`` when the path is mutable workspace data or the file
        exceeds the per-file retention cap; the caller then keeps the ordinary
        (possibly offloaded/paginated) read path.
        """

        if not is_reference_path(path, roots):
            return None
        encoded = text.encode("utf-8", errors="replace")
        size = len(encoded)
        if size > REFERENCE_ENTRY_CAP:
            return None
        digest = hashlib.sha256(encoded).hexdigest()
        previous = self.reference_shelf.get(path)
        if previous is not None and previous.get("sha256") == digest:
            # Refresh LRU position without duplicating the retained text.
            self.reference_shelf.pop(path, None)
            self.reference_shelf[path] = previous
            return {
                "ok": True,
                "path": path,
                "reference": True,
                "cached": True,
                "unchanged": True,
                "sha256": digest,
                "bytes": size,
                "chars": len(text),
                "hint": (
                    "The complete text of this immutable Skill/input reference "
                    "is already pinned in the execution state under "
                    f"reference_shelf['{path}'] (same sha256). Do not read it "
                    "again; use that retained text directly."
                ),
            }
        entry = {"sha256": digest, "bytes": size, "chars": len(text), "text": text}
        # Re-insert so a content change also refreshes the LRU position.
        self.reference_shelf.pop(path, None)
        self.reference_shelf[path] = entry
        while (len(self.reference_shelf) > REFERENCE_SHELF_MAX_ENTRIES
               or sum(item["bytes"] for item in self.reference_shelf.values()) > REFERENCE_SHELF_CAP):
            oldest = next(iter(self.reference_shelf))
            if oldest == path:
                # A single fresh entry never evicts itself (cap >> entry cap).
                break
            self.reference_shelf.pop(oldest, None)
        return {
            "ok": True,
            "path": path,
            "reference": True,
            "retained": True,
            "sha256": digest,
            "bytes": size,
            "chars": len(text),
            "content": text,
            "hint": (
                "Full reference text is now pinned in the execution state "
                f"reference_shelf['{path}'] for the rest of the task. Do not "
                "re-read this same path; read another offset slice only if you "
                "need a bounded part of a different file."
            ),
        }

    def update_plan(self, action: dict[str, Any], *, files: dict[str, str] | None = None) -> dict[str, Any]:
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

        steps: list[dict[str, Any]] = []
        previous = {step['id']: step for step in (self.plan or {}).get('steps', [])}
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
            # Evidence is encouraged but optional mid-run: the platform trusts
            # progress between tool turns and only verifies final deliverables
            # at finish, so a missing note must not block a legitimate step.
            if status == "in_progress":
                in_progress += 1
            seen_ids.add(step_id)
            step = {"id": step_id, "title": title, "status": status, "evidence": evidence}
            for key in ("depends_on", "input_refs", "output_refs"):
                values = raw_step.get(key, previous.get(step_id, {}).get(key, []))
                if not isinstance(values, list) or len(values) > 32 or not all(isinstance(v, str) and v for v in values):
                    return {"ok": False, "error_code": "PLAN_INVALID", "message": f"{key} must be a string array (at most 32 items)"}
                step[key] = list(dict.fromkeys(values))
            index = raw_step.get("skill_index", previous.get(step_id, {}).get("skill_index"))
            if index is not None and (type(index) is not int or not 1 <= index <= self.skill_count):
                return {"ok": False, "error_code": "PLAN_INVALID", "message": "Invalid skill_index"}
            step["skill_index"] = index
            steps.append(step)
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
        if criteria[:len(self.requirements)] != self.requirements:
            return {"ok": False, "error_code": "PLAN_REQUIREMENT_REMOVED", "message": "Preserve previously recorded requirements; add details without removing them."}
        by_id = {step["id"]: step for step in steps}
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(step_id: str) -> bool:
            if step_id in visiting or step_id not in by_id:
                return False
            if step_id in visited:
                return True
            visiting.add(step_id)
            if not all(visit(dep) for dep in by_id[step_id]["depends_on"]):
                return False
            visiting.remove(step_id)
            visited.add(step_id)
            return True
        if not all(visit(step["id"]) for step in steps):
            return {"ok": False, "error_code": "PLAN_DEPENDENCY_INVALID", "message": "Dependencies must reference existing steps and have no cycles."}
        if by_id[validation_step_id]['status'] in {'completed', 'skipped'}:
            if (not self.validation or self.validation.get('status') != 'passed'
                    or self.validation.get('mutation_epoch') != self.mutation_epoch
                    or criteria != self.requirements):
                return {"ok": False, "error_code": "PLAN_VERIFICATION_REQUIRED", "message": "Keep final verification pending/in_progress until run_verifier and record_validation pass for the current requirements and outputs."}
            if by_id[validation_step_id]['status'] == 'skipped':
                return {"ok": False, "error_code": "PLAN_VERIFICATION_REQUIRED", "message": "Final verification cannot be skipped; mark it completed after validation passes."}
        missing_refs: dict[str, list[str]] = {}
        for step in steps:
            if step["status"] in {"in_progress", "completed"}:
                if any(by_id[dep]["status"] not in {"completed", "skipped"} for dep in step["depends_on"]):
                    return {"ok": False, "error_code": "PLAN_DEPENDENCY_PENDING", "message": f"Complete dependencies before {step['id']}"}
                # Mid-run trust: a referenced path that is not present yet does
                # not block the step (outputs are often produced right after the
                # plan update, and directory outputs are bound as aggregates).
                # We record it as a warning; final deliverables are still proven
                # against /workspace/output at finish.
                if files is not None:
                    required = step["input_refs"] + (step["output_refs"] if step["status"] == "completed" else [])
                    absent = [path for path in required if path not in files]
                    if absent:
                        missing_refs[step["id"]] = absent
        if criteria != self.requirements:
            self.validation = self.verification = None
        self.requirements = criteria
        self.plan = {"goal": goal, "steps": steps, "success_criteria": criteria}
        if files is not None:
            # Merge bindings instead of replacing: a relaxed replan may not see
            # every previously bound path (transient read, output produced in a
            # later turn), and dropping the old hash would blind change/deletion
            # detection. Refresh present paths; retain still-relevant old ones.
            merged: dict[str, dict[str, str]] = {}
            for step in steps:
                if step["status"] != "completed":
                    continue
                previous = self.step_artifacts.get(step["id"], {})
                bound: dict[str, str] = {}
                for path in [*step["input_refs"], *step["output_refs"]]:
                    if path in files:
                        bound[path] = files[path]
                    elif path in previous:
                        bound[path] = previous[path]
                merged[step["id"]] = bound
            self.step_artifacts = merged
        self.validation_step_id = validation_step_id
        payload: dict[str, Any] = {"ok": True, "plan": deepcopy(self.plan), "validation_step_id": validation_step_id}
        if missing_refs:
            detail = "; ".join(f"{step_id}: {', '.join(paths)}" for step_id, paths in missing_refs.items())
            payload["warnings"] = [{
                "code": "PLAN_FILE_PENDING",
                "message": (
                    "Referenced paths are not present yet and were not blocked: "
                    f"{detail}. Generate them before finish; final deliverables under "
                    "/workspace/output are verified when the task completes."
                ),
                "missing": missing_refs,
            }]
        return payload

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
        self.skill_evidence[index] = evidence
        return {"ok": True, "skill_index": index, "evidence": evidence}

    def record_validation(
        self,
        action: dict[str, Any],
        *,
        artifact_snapshot: dict[str, str],
    ) -> dict[str, Any]:
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
        if not isinstance(checks, list) or not 1 <= len(checks) <= 200 or not all(
            isinstance(item, str) and item.strip() for item in checks
        ):
            return {
                "ok": False,
                "error_code": "VALIDATION_INVALID",
                "message": "checks must contain 1-200 non-empty observed results",
            }
        if not artifact_snapshot:
            return {
                "ok": False,
                "error_code": "VALIDATION_ARTIFACTS_MISSING",
                "message": "Generate at least one output artifact before recording validation",
            }
        recent_proof = self.verification
        if recent_proof is None or action.get("verification_id") != recent_proof.get("verification_id") or recent_proof.get("artifacts") != artifact_snapshot:
            return {
                "ok": False,
                "error_code": "VALIDATION_EVIDENCE_MISSING",
                "message": "Run run_verifier and reference its verification_id for the current output bytes.",
            }
        if not recent_proof.get("ok") or recent_proof.get("mutation_epoch", self.mutation_epoch) != self.mutation_epoch:
            return {"ok": False, "error_code": "VALIDATION_VERIFIER_FAILED", "message": "The latest verifier failed. Repair and rerun it; a passed claim cannot override its result."}
        normalized = {
            "status": status,
            "summary": summary[:1000],
            "evidence": evidence[:1000],
            "checks": deepcopy(recent_proof["checks"]),
            "mutation_epoch": self.mutation_epoch,
            "verifier": {key: deepcopy(recent_proof[key]) for key in ('verification_id', 'operation', 'tool', 'argv', 'full_result_path', 'exit_code') if key in recent_proof},
            "artifacts": dict(sorted(artifact_snapshot.items())),
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
        synchronized = False
        if self.plan and all(step['status'] in {'completed', 'skipped'}
                             for step in self.plan['steps'] if step['id'] != self.validation_step_id):
            for step in self.plan['steps']:
                if step['id'] == self.validation_step_id:
                    step['status'] = 'completed'
                    step['evidence'] = f"Platform verification {recent_proof['verification_id']} passed: {len(recent_proof['checks'])} checks."
                    synchronized = True
        return {"ok": True, "validation": deepcopy(normalized), "validation_step_completed": synchronized}

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

    def finish_blocker(self, *, current_artifacts: dict[str, str]) -> str | None:
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
        if self.validation.get("artifacts") != dict(sorted(current_artifacts.items())):
            return "Current artifact bytes differ from the files bound to the latest validation; rerun verification."
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

    def record(
        self,
        action: dict[str, Any],
        payload: object,
        *,
        operation: int | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        fingerprint = action_fingerprint(action)
        self.action_counts[fingerprint] = self.action_counts.get(fingerprint, 0) + 1
        succeeded = _payload_succeeded(payload)
        if action.get("action") in OBSERVATION_TOOLS and succeeded:
            self._observation_cache[(self.mutation_epoch, fingerprint)] = deepcopy(payload)
        elif action.get("action") in WORKSPACE_MUTATING_TOOLS and isinstance(payload, dict) and ("exit_code" in payload or succeeded or payload.get("execution_started")):
            self.mutation_epoch += 1
            self.validation = None
            self.verification = None
            self._observation_cache.clear()
            if self.plan:
                for step in self.plan['steps']:
                    if step['id'] == self.validation_step_id and step['status'] == 'completed':
                        step['status'] = 'pending'
                        step['evidence'] = 'Workspace changed; rerun final verification.'
            # Invalidation is conservative; a successful no-op is not progress.
            observable = {key: payload[key] for key in ('exit_code', 'stdout', 'stderr', 'path', 'bytes', 'artifacts') if key in payload}
            signature = hashlib.sha256(json.dumps(observable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if succeeded and self._result_signatures.get(fingerprint) != signature:
                self.action_counts.clear()
                self.progress_epoch += 1
            if succeeded:
                self._result_signatures[fingerprint] = signature
        self.observations.append(
            _compact_observation(
                action,
                payload,
                operation=operation or len(self.observations) + 1,
                mutation_epoch=self.mutation_epoch,
                tool_call_id=tool_call_id,
            )
        )
        self.observations = self.observations[-24:]

    def repeated_count(self, action: dict[str, Any]) -> int:
        return self.action_counts.get(action_fingerprint(action), 0)

    def checkpoint(self) -> str:
        snapshot = {
            "plan": self.plan,
            "validation_step_id": self.validation_step_id,
            "validation": {key: value for key, value in self.validation.items() if key != 'checks'} if self.validation else None,
            "loaded_skill_indexes": sorted(self.loaded_skills),
            "completed_skill_indexes": sorted(self.completed_skill_indexes),
            "skill_evidence": self.skill_evidence,
            "requirements": self.requirements,
            "recent_observations": self.observations[-10:],
            "reference_shelf": [
                {"path": path, "sha256": item["sha256"], "bytes": item["bytes"],
                 "chars": item["chars"], "content": item["text"]}
                for path, item in self.reference_shelf.items()
            ],
        }
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

    def invalidate_changed_steps(self, files: dict[str, str]) -> None:
        if not self.plan:
            return
        invalid = {step_id for step_id, snapshot in self.step_artifacts.items() if any(files.get(path) != digest for path, digest in snapshot.items())}
        while True:
            expanded = invalid | {step["id"] for step in self.plan["steps"] if set(step["depends_on"]) & invalid}
            if expanded == invalid:
                break
            invalid = expanded
        for step in self.plan["steps"]:
            if step["id"] in invalid:
                step["status"] = "pending"
                step["evidence"] = "Input or output changed; this step must be repeated."
                self.step_artifacts.pop(step["id"], None)
                if step.get("skill_index"):
                    self.completed_skill_indexes.discard(step["skill_index"])
        if invalid:
            self.validation = None
            self.verification = None
