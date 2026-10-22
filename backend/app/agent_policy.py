from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable


PLAN_STATUSES = frozenset({"pending", "in_progress", "completed", "skipped"})
OBSERVATION_TOOLS = frozenset({"list_files", "read_file"})
# Binary inputs (PDF/DOCX/images) cannot be inspected with read_file: the
# model needs command/run_python to learn their structure before it can
# write an informed plan. Allow a small bounded number of such inspection
# calls before the first plan; write_file and fixed-skill execution stay
# blocked until a plan exists.
PRE_PLAN_INSPECTION_LIMIT = 3
PRE_PLAN_INSPECTION_HINT = (
    "Pre-plan inspection is allowed and this call ran, but you must now call "
    "update_plan with a valid execution plan before any further work. "
    "write_file and run_fixed_skill remain blocked until the plan exists."
)
WORKSPACE_MUTATING_TOOLS = frozenset({"write_file", "command", "run_python"})

# Immutable instruction material (Skill package reference/script files and
# user inputs) is read once and pinned in the execution-state memory message,
# so the model never has to re-read a spec after old exchanges leave the
# context window. 20 KiB per file covers every observed Skill reference doc
# (largest ~19 KiB); the shelf total (60 KiB) is projected into the model
# context greedily and oldest entries are dropped first when the input budget
# is tight.
REFERENCE_ENTRY_CAP = 20 * 1024
REFERENCE_SHELF_CAP = 60 * 1024
REFERENCE_SHELF_MAX_ENTRIES = 15

# Package subdirectories the agent itself writes into (generated sources,
# SVG/image batches, exports). Such paths are mutable task data, even though
# they physically live inside the extracted Skill package, and must never be
# served from a stale pinned copy.
MUTABLE_PACKAGE_DIRS = frozenset({"assets", "exports", "output", "work", "tmp"})


def is_reference_path(path: str, roots: Iterable[str]) -> bool:
    """True for task-immutable instruction/input locations.

    Qualifies: anything under /workspace/input, and static package content
    under a Skill root (references, scripts, SKILL.md, ...). Does not qualify:
    /workspace/work, /workspace/output, or generated package subdirectories
    such as assets/ or exports/ that the task writes into.
    """

    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or ".." in parsed.parts:
        return False
    if path == "/workspace/input" or path.startswith("/workspace/input/"):
        return True
    for root in roots:
        if not root:
            continue
        base = PurePosixPath(root.rstrip("/"))
        try:
            relative = parsed.relative_to(base)
        except ValueError:
            continue
        if str(relative) == ".":
            return True
        if any(part in MUTABLE_PACKAGE_DIRS for part in relative.parts):
            return False
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
        for key in ("exit_code", "error_code", "elapsed_seconds", "path", "bytes", "full_result_path"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)):
                item[key] = value
        if not item["ok"]:
            message = payload.get("message") or payload.get("stderr")
            if not (isinstance(message, str) and message.strip()):
                # Errors are sometimes printed to stdout only; keep its tail
                # so a silent-looking failure stays diagnosable in the
                # projected history.
                stdout = payload.get("stdout")
                message = stdout[-240:] if isinstance(stdout, str) and stdout.strip() else None
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
    # New tasks retain an outline, without model-driven checkbox bookkeeping.
    # Default True preserves the contract of older durable checkpoints.
    plan_status_tracking: bool = True
    ordered_skills: bool = False
    plan: dict[str, Any] | None = None
    validation_step_id: str | None = None
    validation: dict[str, Any] | None = None
    validation_failures: int = 0
    delivery_only: bool = False
    loaded_skills: set[int] = field(default_factory=set)
    completed_skill_indexes: set[int] = field(default_factory=set)
    observations: list[dict[str, Any]] = field(default_factory=list)
    action_counts: dict[str, int] = field(default_factory=dict)
    mutation_epoch: int = 0
    progress_epoch: int = 0
    # command/run_python calls dispatched before the first plan exists
    # (binary-input inspection). Bounded by PRE_PLAN_INSPECTION_LIMIT.
    pre_plan_inspection_calls: int = 0
    _result_signatures: dict[str, str] = field(default_factory=dict)
    verification: dict[str, Any] | None = None
    requirements: list[str] = field(default_factory=list)
    skill_evidence: dict[int, str] = field(default_factory=dict)
    step_artifacts: dict[str, dict[str, str]] = field(default_factory=dict)
    _observation_cache: dict[tuple[int, str], object] = field(default_factory=dict)
    # path -> {"sha256", "bytes", "chars", "text"}; plain JSON dict so it is
    # carried by durable snapshots and pinned through context projection.
    reference_shelf: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Completed command cursor for framework-declared stage workflows. Kept in
    # durable state so a recoverable failure does not replay successful exports.
    workflow_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Anti-loop counters (durable). Private path cache is mid-run only.
    duplicate_observation_streak: int = 0
    verifier_fail_streak: int = 0
    last_verifier_fail_signature: str = ""
    verifier_anchor_progress_epoch: int = -1
    verifier_anchor_mutation_epoch: int = -1
    _stable_path_cache: dict[str, object] = field(default_factory=dict, repr=False)
    observation_accesses: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    managed_artifacts: dict[str, str] = field(default_factory=dict)
    artifact_sources: dict[str, str] = field(default_factory=dict)

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

    def _shelf_hint(self, path: str, *, cached: bool) -> str:
        if cached:
            return (
                f"Served from the pinned reference shelf ({path}); no sandbox "
                "read was needed. The complete immutable text is held under "
                f"reference_shelf['{path}'] — do not read this path again."
            )
        return (
            "Full reference text is now pinned in the execution state "
            f"reference_shelf['{path}'] for the rest of the task. Do not "
            "re-read or paginate this same path."
        )

    def _put_shelf(self, path: str, digest: str, size: int, text: str) -> None:
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

    def reference_cached(self, path: str, offset: int, limit: int,
                         roots: Iterable[str]) -> dict[str, Any] | None:
        """Serve a pinned immutable reference slice without touching the sandbox.

        Returns ``None`` for mutable/unknown paths or when the file has not
        been pinned yet; the caller then performs the real read.
        """

        if not is_reference_path(path, roots):
            return None
        entry = self.reference_shelf.get(path)
        if entry is None:
            return None
        # Refresh LRU position and serve the requested character slice.
        self.reference_shelf.pop(path, None)
        self.reference_shelf[path] = entry
        text = entry["text"]
        content = text[offset:offset + limit]
        end = offset + len(content)
        eof = end >= len(text)
        return {
            "ok": True,
            "path": path,
            "reference": True,
            "cached": True,
            "unchanged": True,
            "sha256": entry["sha256"],
            "bytes": entry["bytes"],
            "chars": entry["chars"],
            "offset": offset,
            "limit": limit,
            "content": content,
            "total_chars": len(text),
            "returned_chars": len(content),
            "eof": eof,
            "complete": offset == 0 and eof,
            "truncated": not eof,
            "next_offset": None if eof else end,
            "hint": self._shelf_hint(path, cached=True),
        }

    def reference_pin(self, path: str, text: str, roots: Iterable[str], *,
                      offset: int, limit: int, eof: bool | None = None) -> dict[str, Any] | None:
        """Pin a freshly read immutable reference and return its first payload.

        Only a read from offset 0 that reached EOF (so the whole file was
        observed) and fits the per-file cap is pinned. Everything else keeps
        the ordinary paginated/offloaded read path.
        """

        if not is_reference_path(path, roots) or offset != 0:
            return None
        encoded = text.encode("utf-8", errors="replace")
        size = len(encoded)
        if size > REFERENCE_ENTRY_CAP or eof is False or (eof is None and len(text) >= limit):
            return None
        digest = hashlib.sha256(encoded).hexdigest()
        self._put_shelf(path, digest, size, text)
        return {
            "ok": True,
            "path": path,
            "reference": True,
            "retained": True,
            "sha256": digest,
            "bytes": size,
            "chars": len(text),
            "content": text,
            "hint": self._shelf_hint(path, cached=False),
        }

    def update_plan(self, action: dict[str, Any], *, files: dict[str, str] | None = None) -> dict[str, Any]:
        goal = str(action.get("goal") or "").strip()
        if not goal:
            # The model may omit goal when it only refines steps mid-run:
            # keep the current goal instead of burning a correction round-trip.
            goal = str((self.plan or {}).get("goal") or "").strip()
        raw_steps = action.get("steps")
        success_criteria = action.get("success_criteria")
        if success_criteria is None:
            # The model may omit criteria when it only refines steps mid-run:
            # keep the recorded criteria (they embed the requirements ledger)
            # instead of burning a correction round-trip.
            success_criteria = (self.plan or {}).get("success_criteria")
        validation_step_id = str(action.get("validation_step_id") or "").strip()[:40]
        if not validation_step_id:
            # The model may omit the validation step id when it only refines
            # steps mid-run: keep the recorded one instead of burning a
            # correction round-trip.
            validation_step_id = str(self.validation_step_id or "").strip()[:40]
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
            status = str(raw_step.get("status") or "pending").strip() if self.plan_status_tracking else "pending"
            evidence = str(raw_step.get("evidence") or "").strip()[:800] if self.plan_status_tracking else ""
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
        if not self.delivery_only and by_id[validation_step_id]['status'] in {'completed', 'skipped'}:
            if (not self.validation or self.validation.get('status') != 'passed'
                    or self.validation.get('mutation_epoch') != self.mutation_epoch
                    or criteria != self.requirements):
                return {"ok": False, "error_code": "PLAN_VERIFICATION_REQUIRED", "message": "Keep final verification pending/in_progress until run_verifier passes and the platform records validation for the current requirements and outputs."}
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
        if not self.plan_status_tracking:
            self.plan["tracking_mode"] = "outline"
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

    def pre_plan_inspection_allowed(self) -> bool:
        """True if one more command/run_python may run before any plan exists.

        Binary inputs can only be inspected via command/run_python, so a
        bounded number of pre-plan inspection calls is trusted; the result
        payload carries PRE_PLAN_INSPECTION_HINT demanding the plan next.
        """

        return (
            self.plan_required and self.plan is None
            and self.pre_plan_inspection_calls < PRE_PLAN_INSPECTION_LIMIT
        )

    def note_pre_plan_inspection(self) -> int:
        """Record one dispatched pre-plan inspection; return calls remaining."""

        self.pre_plan_inspection_calls += 1
        return PRE_PLAN_INSPECTION_LIMIT - self.pre_plan_inspection_calls

    def ensure_active_step(self) -> dict[str, Any]:
        """Make sure a plan step is in_progress before a mutating tool runs.

        Mirrors the plan auto-synchronization in record_validation: when the
        model starts work without explicitly activating a step, activate the
        unique dependency-ready pending step instead of rejecting the call —
        the rejection wasted a full reasoning round and surfaced as a red
        failed event in nearly every job. Ambiguous topologies stay hard
        errors so the model's intent is never guessed:
        - no plan: the model must create one (first-turn planning is never
          auto-created);
        - several dependency-ready steps: a parallel branch, the model must
          mark the one it means to work on;
        - no ready pending step: prerequisites are unfinished (or the plan is
          already complete and the task should finish).
        """

        if not self.plan and not self.plan_required:
            return {"ok": True, "auto_activated": False, "tracking_mode": "skill_flow"}
        if not self.plan:
            return {
                "ok": False,
                "error_code": "PLAN_REQUIRED",
                "message": (
                    "Create/update the plan with one in_progress step before executing work."
                    if self.plan_status_tracking
                    else "Create/update a valid execution outline before executing work. "
                         "Step statuses are not tracked in this task."
                ),
            }
        if not self.plan_status_tracking:
            return {"ok": True, "auto_activated": False, "tracking_mode": "outline"}
        steps = self.plan.get("steps", [])
        active = next((step for step in steps if step["status"] == "in_progress"), None)
        if active is not None:
            return {"ok": True, "step": deepcopy(active), "auto_activated": False}
        statuses = {step["id"]: step["status"] for step in steps}
        ready = [
            step
            for step in steps
            if step["status"] == "pending"
            and all(
                statuses.get(dep) in {"completed", "skipped"}
                for dep in step.get("depends_on", [])
            )
        ]
        if len(ready) == 1:
            step = ready[0]
            step["status"] = "in_progress"
            return {"ok": True, "step": deepcopy(step), "auto_activated": True}
        pending = [step for step in steps if step["status"] == "pending"]
        if not pending:
            return {
                "ok": False,
                "error_code": "PLAN_STEP_NONE_OPEN",
                "message": (
                    "Every plan step is already completed; finish the task or "
                    "update the plan before executing more work."
                ),
            }
        if not ready:
            return {
                "ok": False,
                "error_code": "PLAN_STEP_BLOCKED",
                "message": (
                    "No pending plan step has all its dependencies completed; "
                    "complete the prerequisite steps or update the plan with "
                    "the current step marked in_progress before executing work."
                ),
            }
        return {
            "ok": False,
            "error_code": "PLAN_STEP_AMBIGUOUS",
            "message": (
                f"{len(ready)} dependency-ready plan steps are pending; update "
                "the plan marking the single step you are working on as "
                "in_progress before executing work."
            ),
        }

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
        if self.plan_status_tracking and self.plan and all(step['status'] in {'completed', 'skipped'}
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
        if incomplete_skills and self.skill_count > 1:
            return f"Complete every selected Skill phase before finish; incomplete skill indexes: {incomplete_skills}."
        if self.plan_status_tracking and self.plan:
            incomplete = [
                step["id"] for step in self.plan["steps"]
                if step["status"] not in {"completed", "skipped"}
                and not (self.delivery_only and step['id'] == self.validation_step_id)
            ]
            if incomplete:
                return f"Update the plan before finish; incomplete step ids: {incomplete}."
        if self.delivery_only:
            return None
        if self.validation is None or self.validation.get("mutation_epoch") != self.mutation_epoch:
            return "Run one concentrated run_verifier check on the current artifacts before finish; successful validation is recorded automatically."
        if self.validation.get("artifacts") != dict(sorted(current_artifacts.items())):
            return "Current artifact bytes differ from the files bound to the latest validation; rerun verification."
        return None

    @staticmethod
    def _observation_path_key(action: dict[str, Any]) -> str | None:
        """Stable key for read_file/list_files; ignores reason/timeout."""
        name = str(action.get("action") or "")
        if name not in OBSERVATION_TOOLS:
            return None
        path = str(action.get("path") or "").strip()
        if not path:
            return None
        identity = {"action": name, "path": str(PurePosixPath(path))}
        if name == "read_file":
            identity["offset"] = int(action.get("offset") or 0)
            # Omitting both controls permits automatic reference completion;
            # it is a different operation from a bounded first-page read.
            identity["limit"] = int(action.get("limit") or 30_000)
            identity["auto_complete"] = action.get("offset") is None and action.get("limit") is None
        return action_fingerprint(identity)

    def _invalidate_stable_path(self, path: str) -> None:
        path = str(path or "").strip()
        if not path:
            return
        target = PurePosixPath(path)
        doomed = []
        for key in self._stable_path_cache:
            identity = json.loads(key)
            observed = PurePosixPath(identity["path"])
            if observed == target or (identity["action"] == "list_files" and target.is_relative_to(observed)):
                doomed.append(key)
        for key in doomed:
            self._stable_path_cache.pop(key, None)

    def _invalidate_stable_prefix(self, prefix: str) -> None:
        prefix = str(prefix or "").rstrip("/")
        if not prefix:
            return
        doomed = [key for key in self._stable_path_cache
                  if PurePosixPath(json.loads(key)["path"]).is_relative_to(prefix)]
        for key in doomed:
            self._stable_path_cache.pop(key, None)

    def note_stable_observation(self, action: dict[str, Any], payload: object) -> None:
        """Remember successful observations across write_file to unrelated paths."""
        if not _payload_succeeded(payload):
            return
        key = self._observation_path_key(action)
        if key is None:
            return
        # Bound this transient optimization independently of durable history.
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1_048_576:
            return
        self._stable_path_cache.pop(key, None)
        self._stable_path_cache[key] = deepcopy(payload)
        while (len(self._stable_path_cache) > 32 or
               len(json.dumps(self._stable_path_cache, ensure_ascii=False).encode("utf-8")) > 1_048_576):
            self._stable_path_cache.pop(next(iter(self._stable_path_cache)))

    def stable_cached_observation(self, action: dict[str, Any], *, allow_eof: bool = True) -> object | None:
        """Return a prior observation when the same path was not invalidated.

        Survives write_file to other paths (mutation_epoch cache does not).
        """
        key = self._observation_path_key(action)
        if key is None:
            return None
        value = self._stable_path_cache.get(key)
        if value is None and allow_eof and action.get('action') == 'read_file':
            # An unchanged file cannot acquire more text by increasing an
            # offset beyond its measured EOF. Reuse that proof across windows.
            offset = int(action.get('offset') or 0)
            path = str(PurePosixPath(str(action.get('path') or '')))
            for old_key, old in reversed(list(self._stable_path_cache.items())):
                if json.loads(old_key).get('path') != path or not isinstance(old, dict):
                    continue
                total = old.get('total_chars')
                if type(total) is int and offset >= total:
                    value = {**old, 'content': '', 'offset': offset,
                             'limit': int(action.get('limit') or 30_000),
                             'returned_chars': 0, 'eof': True, 'complete': offset == 0,
                             'truncated': False, 'next_offset': None}
                    value.pop('auto_completed', None)
                    break
        if value is None:
            return None
        cached = deepcopy(value)
        if isinstance(cached, dict):
            cached["cached"] = True
            cached["stable_cache"] = True
            cached["hint"] = (
                "This exact read window is unchanged. Use its content to decide "
                "the next operation. Read a different window if truncated; do not "
                "write files merely to satisfy a progress counter."
            )
            if cached.get('eof') is True and cached.get('content') == '':
                cached['hint'] = f"EOF: this file has {cached.get('total_chars')} characters. Increasing the offset cannot reveal more text; use the existing result or read from 0 if it is unavailable."
        return cached

    def register_observation_access(self, action: dict[str, Any], *, reused: bool,
                                    payload: object = None, content_available: bool = True) -> dict[str, Any] | None:
        """Detect identical, available information, never ordinary pagination.

        Only proven cache hits can be blocked. If context projection removed
        the earlier full result, supply it again rather than deny recovery.
        """
        key = self._observation_path_key(action)
        if key is None:
            self.duplicate_observation_streak = 0
            return None
        path = str(action.get("path") or "")
        if not reused or not content_available:
            self.duplicate_observation_streak = 0
            self.observation_accesses.pop(key, None)
            return None
        if isinstance(payload, dict):
            identity = {name: payload.get(name) for name in
                        ("path", "content", "offset", "limit", "total_chars", "eof", "complete")}
            # Advancing beyond EOF is still the same empty information. Return
            # an explicit EOF diagnosis instead of rewarding unbounded offsets.
            if payload.get("eof") is True and payload.get("content") == "":
                key = action_fingerprint({"action": "read_file", "path": path, "past_eof": True})
                identity.pop("offset", None)
                identity.pop("limit", None)
        else:
            identity = payload
        signature = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = self.observation_accesses.get(key, {})
        count = (previous.get("count", 0) + 1
                 if previous.get("signature") == signature and previous.get("mutation_epoch") == self.mutation_epoch else 1)
        self.observation_accesses.pop(key, None)
        self.observation_accesses[key] = {"signature": signature, "count": count, "mutation_epoch": self.mutation_epoch}
        while len(self.observation_accesses) > 32:
            self.observation_accesses.pop(next(iter(self.observation_accesses)))
        self.duplicate_observation_streak = count
        if self.duplicate_observation_streak >= 3:
            return {
                "ok": False,
                "error_code": "OBSERVATION_LOOP_BREAK",
                "message": (
                    f"This exact unchanged read result for {path[:240]} is already in the model context. "
                    "Repeated requests do not provide additional information."
                ),
                "path": path[:500],
                "duplicate_streak": self.duplicate_observation_streak,
                "hint": (
                    "Use the retained result. If it reaches EOF, there is no next page. "
                    "Choose the next task operation, read a genuinely different window, "
                    "or finish only when the task and its checks are fulfilled."
                ),
            }
        return None

    def observation_loop_nudge(self) -> str | None:
        if self.duplicate_observation_streak >= 2:
            return (
                "The same unchanged read window is already available. Use that result; "
                "read further only if truncated, or choose the next task operation. "
                "Do not create artificial mutations to bypass this diagnostic."
            )
        return None

    def register_verifier_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Track fail→edit→reverify thrashing; annotate payload with batch-fix guidance."""
        checks = payload.get("checks")
        signature_src = json.dumps(checks, ensure_ascii=False, sort_keys=True) if checks is not None else json.dumps(
            {k: payload.get(k) for k in ("exit_code", "error_code", "message") if k in payload},
            ensure_ascii=False, sort_keys=True,
        )
        signature = hashlib.sha256(signature_src.encode("utf-8")).hexdigest()
        same_signature = bool(self.last_verifier_fail_signature) and signature == self.last_verifier_fail_signature
        if self.progress_epoch == self.verifier_anchor_progress_epoch:
            self.verifier_fail_streak += 1
        else:
            self.verifier_fail_streak = 1
            self.verifier_anchor_progress_epoch = self.progress_epoch
        # Same failing checks after intervening edits still count as thrashing.
        if same_signature:
            self.verifier_fail_streak = max(self.verifier_fail_streak, 2)
        self.last_verifier_fail_signature = signature
        self.verifier_anchor_mutation_epoch = self.mutation_epoch
        annotated = dict(payload)
        annotated["verifier_fail_streak"] = self.verifier_fail_streak
        annotated["same_failure_signature"] = same_signature
        if self.verifier_fail_streak >= 2 or same_signature:
            annotated["hint"] = (
                "Verifier thrash guard: the same checks are still failing. "
                "Apply ALL remaining fixes in ONE cohesive write_file/run_python "
                "batch, then call run_verifier once. Do not alternate tiny edits "
                "with re-verify."
            )
        return annotated

    def verifier_retry_blocked(self) -> dict[str, Any] | None:
        """Block another run_verifier when artifacts did not progress after a failure."""
        if self.verifier_fail_streak < 1:
            return None
        # A failed correction may still have changed files. Never deny a
        # recheck merely because that corrective command exited nonzero.
        if self.mutation_epoch != self.verifier_anchor_mutation_epoch:
            return None
        return {
            "ok": False,
            "error_code": "VERIFIER_RETRY_WITHOUT_PROGRESS",
            "message": (
                "run_verifier is blocked until the workspace/artifacts change after the "
                "previous failure."
            ),
            "verifier_fail_streak": self.verifier_fail_streak,
            "hint": (
                "Batch every remaining verifier fix into one cohesive mutating turn "
                "(write_file / run_python / run_skill_workflow), then rerun run_verifier."
            ),
        }

    def register_verifier_success(self) -> None:
        self.verifier_fail_streak = 0
        self.last_verifier_fail_signature = ""
        self.verifier_anchor_progress_epoch = -1
        self.verifier_anchor_mutation_epoch = -1

    def register_artifact_collection(self, snapshot: dict[str,str], sources: dict[str,str]) -> bool:
        """Track real byte changes from framework collection, not dummy writes."""
        reference = (self.validation or self.verification or {}).get('artifacts', self.managed_artifacts)
        changed = any(reference.get(path) != digest for path,digest in snapshot.items())
        self.managed_artifacts.update(snapshot)
        self.artifact_sources.update(sources)
        if changed:
            self.mutation_epoch += 1
            self.progress_epoch += 1
            self.validation = self.verification = None
            self._observation_cache.clear()
            self._stable_path_cache.clear()
            self.observation_accesses.clear()
            self.duplicate_observation_streak = 0
            self.register_verifier_success()
        return changed

    def failure_recovery_hint(self, action: dict[str, Any]) -> str | None:
        failure = self.failed_operations.get(action_fingerprint(action), {})
        if failure.get('repeats', 0) < 2:
            return None
        return (
            f"The same operation returned the same failure {failure['repeats']} times: "
            f"{failure.get('diagnostic', '')}. Keep the required checks. Inspect the relevant "
            "inputs/requirements and address the reported causes together before retrying; "
            "do not regenerate already-correct outputs or blindly rerun a failed export. "
            "For a transient external error, use its diagnosis to choose a bounded retry."
        )

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
            self.note_stable_observation(action, payload)
        elif action.get("action") in WORKSPACE_MUTATING_TOOLS and isinstance(payload, dict) and ("exit_code" in payload or succeeded or payload.get("execution_started")):
            self.mutation_epoch += 1
            self.validation = None
            self.verification = None
            self._observation_cache.clear()
            self.duplicate_observation_streak = 0
            self.observation_accesses.clear()
            name = str(action.get("action") or "")
            if name == "write_file":
                self._invalidate_stable_path(str(action.get("path") or payload.get("path") or ""))
            else:
                # Scripts can change any mutable Skill package directory, not
                # just work/output. Immutable reference_shelf remains separate.
                self._stable_path_cache.clear()
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
        if action.get('action') in {'command', 'run_python', 'run_verifier'}:
            if succeeded:
                self.failed_operations.pop(fingerprint, None)
            elif isinstance(payload, dict) and ('exit_code' in payload or payload.get('execution_started')):
                diagnostic = (payload.get('stderr') or payload.get('stdout') or payload.get('message') or '')
                diagnostic = str(diagnostic)[-1600:]
                signature = hashlib.sha256(json.dumps({
                    'exit_code': payload.get('exit_code'), 'error_code': payload.get('error_code'),
                    'diagnostic': diagnostic}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                previous = self.failed_operations.pop(fingerprint, {})
                self.failed_operations[fingerprint] = {
                    'tool': action['action'], 'argv': action.get('argv', [])[:6],
                    'diagnostic': diagnostic[-640:], 'signature': signature,
                    'repeats': previous.get('repeats', 0) + 1 if previous.get('signature') == signature else 1,
                    'operation': operation or len(self.observations),
                }
                while len(self.failed_operations) > 16:
                    self.failed_operations.pop(next(iter(self.failed_operations)))

    def repeated_count(self, action: dict[str, Any]) -> int:
        return self.action_counts.get(action_fingerprint(action), 0)

    def checkpoint(self) -> str:
        snapshot = {
            "artifact_acceptance_mode": "delivery_only" if self.delivery_only else "full_verification",
            "plan": self.plan,
            "plan_status_tracking": self.plan_status_tracking,
            "validation_step_id": self.validation_step_id,
            "validation": {key: value for key, value in self.validation.items() if key != 'checks'} if self.validation else None,
            "loaded_skill_indexes": sorted(self.loaded_skills),
            "workflow_runs": self.workflow_runs,
            "unresolved_failures": list(self.failed_operations.values())[-3:],
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
