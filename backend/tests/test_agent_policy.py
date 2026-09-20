from __future__ import annotations

from app.agent_policy import AgentExecutionState, action_fingerprint


ARTIFACT_SNAPSHOT = {
    "/workspace/output/result.docx": "a" * 64,
}


def _completed_plan(*, verified=False) -> dict:
    return {
        "goal": "Inspect, process, and verify the requested Skill result",
        "steps": [
            {
                "id": "process",
                "title": "Process the input",
                "status": "completed",
                "evidence": "/workspace/output/result.docx",
            },
            {
                "id": "verify",
                "title": "Concentrated final verification",
                "status": "completed" if verified else "pending",
                "evidence": "verify.py observed the requested output properties",
            },
        ],
        "success_criteria": ["Requested result is produced and the verifier passes"],
        "validation_step_id": "verify",
    }


def _record_verifier(state: AgentExecutionState, stdout: str = "PARAGRAPHS=4\nTITLE_FONT=方正小标宋简体") -> None:
    state.record(
        {"action": "command", "argv": ["python3", "verify.py"]},
        {"exit_code": 0, "stdout": stdout, "stderr": ""},
    )
    state.verification = {"ok": True, "verification_id": "verified-1", "checks": [{"requirement_id": "r1", "passed": True, "observed": stdout}], "artifacts": ARTIFACT_SNAPSHOT, "tool": "run_verifier", "operation": 1}


def _pass_validation(state: AgentExecutionState) -> dict:
    return state.record_validation(
        {
            "action": "record_validation",
            "verification_id": "verified-1",
            "status": "passed",
            "summary": "The concentrated verifier passed",
            "evidence": "verify.py exit 0 with observed values",
            "checks": ["PARAGRAPHS=4", "TITLE_FONT=方正小标宋简体"],
        },
        artifact_snapshot=ARTIFACT_SNAPSHOT,
    )


def test_action_fingerprint_ignores_reason_and_timeout_but_not_read_chunk():
    first = {
        "action": "read_file",
        "path": "/workspace/work/report.txt",
        "offset": 0,
        "limit": 1000,
        "reason": "inspect once",
    }
    equivalent = {**first, "reason": "inspect again", "timeout_seconds": 10}
    next_chunk = {**first, "offset": 1000}

    assert action_fingerprint(first) == action_fingerprint(equivalent)
    assert action_fingerprint(first) != action_fingerprint(next_chunk)


def test_observation_cache_reuses_reads_until_workspace_mutates():
    state = AgentExecutionState(skill_count=1)
    read = {
        "action": "read_file",
        "path": "/workspace/work/report.txt",
        "offset": 0,
        "limit": 1000,
    }
    state.record(read, "first result")
    assert state.cached_observation(read) == "first result"

    state.record(
        {"action": "write_file", "path": "/workspace/work/report.txt"},
        {"ok": True, "path": "/workspace/work/report.txt", "bytes": 10},
    )
    assert state.cached_observation(read) is None


def test_plan_is_concise_and_names_one_validation_step():
    state = AgentExecutionState(skill_count=1)
    missing_validation = _completed_plan()
    missing_validation.pop("validation_step_id")
    result = state.update_plan(missing_validation)
    assert result["error_code"] == "PLAN_VALIDATION_STEP_REQUIRED"

    result = state.update_plan(_completed_plan())
    assert result["ok"] is True
    assert result["validation_step_id"] == "verify"


def test_plan_allows_completed_step_without_evidence_mid_run():
    # Mid-run trust: an evidence note is optional; final verification still
    # requires its own passed record_validation evidence.
    state = AgentExecutionState(skill_count=1)
    action = _completed_plan()
    action["steps"][0]["evidence"] = ""
    result = state.update_plan(action)
    assert result["ok"] is True


def test_final_plan_step_requires_current_validation_and_cannot_be_skipped():
    state = AgentExecutionState(skill_count=1)
    assert state.update_plan(_completed_plan())["ok"]
    assert state.update_plan(_completed_plan(verified=True))["error_code"] == "PLAN_VERIFICATION_REQUIRED"
    _record_verifier(state)
    assert _pass_validation(state)["ok"]
    skipped = _completed_plan(verified=True)
    skipped['steps'][-1]['status'] = 'skipped'
    assert state.update_plan(skipped)["error_code"] == "PLAN_VERIFICATION_REQUIRED"
    assert state.update_plan(_completed_plan(verified=True))["ok"]
    state.record({'action': 'run_python', 'code': 'partial_write_then_fail()'}, {'exit_code': 1})
    assert state.plan['steps'][-1]['status'] == 'pending'
    assert state.update_plan(_completed_plan(verified=True))["error_code"] == "PLAN_VERIFICATION_REQUIRED"


def test_new_requirements_cannot_reuse_completed_verification():
    state = AgentExecutionState(skill_count=1)
    assert state.update_plan(_completed_plan())["ok"]
    _record_verifier(state)
    assert _pass_validation(state)["ok"]
    updated = _completed_plan(verified=True)
    updated['success_criteria'].append('All records are covered')
    assert state.update_plan(updated)['error_code'] == 'PLAN_VERIFICATION_REQUIRED'


def test_validation_requires_a_real_recent_tool_observation():
    state = AgentExecutionState(skill_count=1)
    result = _pass_validation(state)
    assert result["error_code"] == "VALIDATION_EVIDENCE_MISSING"


def test_validation_requires_output_artifacts_and_binds_verifier_operation():
    state = AgentExecutionState(skill_count=1)
    _record_verifier(state)
    missing = state.record_validation(
        {
            "status": "passed",
            "summary": "Verified",
            "evidence": "verify.py exit 0",
            "checks": ["PARAGRAPHS=4"],
        },
        artifact_snapshot={},
    )
    assert missing["error_code"] == "VALIDATION_ARTIFACTS_MISSING"

    passed = _pass_validation(state)
    assert passed["validation"]["artifacts"] == ARTIFACT_SNAPSHOT
    assert passed["validation"]["verifier"]["tool"] == "run_verifier"
    assert passed["validation"]["verifier"]["operation"] == 1


def test_blocked_outcome_requires_a_real_failed_operation():
    state = AgentExecutionState(skill_count=1)
    missing = state.block_workflow("Remote service unavailable", "DNS lookup failed")
    assert missing["error_code"] == "BLOCK_EVIDENCE_MISSING"

    state.record(
        {"action": "command", "argv": ["curl", "https://example.test"]},
        {"exit_code": 6, "stdout": "", "stderr": "Could not resolve host"},
    )
    blocked = state.block_workflow("Remote service unavailable", "curl exited 6")
    assert blocked["ok"] is True
    assert blocked["observation"]["exit_code"] == 6


def test_complete_workflow_requires_skill_plan_and_current_validation():
    state = AgentExecutionState(skill_count=1, loaded_skills={1})
    assert "execution plan" in (
        state.finish_blocker(current_artifacts=ARTIFACT_SNAPSHOT) or ""
    )
    assert state.update_plan(_completed_plan())["ok"] is True
    assert "incomplete skill indexes" in (
        state.finish_blocker(current_artifacts=ARTIFACT_SNAPSHOT) or ""
    )
    assert state.complete_skill(1, "/workspace/output/result.docx")["ok"] is True
    assert "incomplete step ids" in (
        state.finish_blocker(current_artifacts=ARTIFACT_SNAPSHOT) or ""
    )
    _record_verifier(state)
    assert _pass_validation(state)["ok"] is True
    assert state.update_plan(_completed_plan(verified=True))["ok"] is True
    assert state.finish_blocker(current_artifacts=ARTIFACT_SNAPSHOT) is None
    assert "differ" in (
        state.finish_blocker(
            current_artifacts={"/workspace/output/result.docx": "b" * 64}
        )
        or ""
    )


def test_workspace_mutation_invalidates_previous_validation():
    state = AgentExecutionState(
        skill_count=1,
        loaded_skills={1},
        completed_skill_indexes={1},
    )
    state.update_plan(_completed_plan())
    _record_verifier(state)
    assert _pass_validation(state)["ok"] is True

    state.record(
        {"action": "run_python", "code": "rewrite_artifact()"},
        {"exit_code": 0, "stdout": "rewritten", "stderr": ""},
    )
    assert state.validation is None
    assert "incomplete step ids" in (
        state.finish_blocker(current_artifacts=ARTIFACT_SNAPSHOT) or ""
    )


def test_old_verifier_cannot_be_rebound_after_a_later_workspace_write():
    state = AgentExecutionState(skill_count=1)
    _record_verifier(state)
    state.record(
        {"action": "write_file", "path": "/workspace/output/result.txt"},
        {"ok": True, "path": "/workspace/output/result.txt", "bytes": 7},
    )

    result = _pass_validation(state)

    assert result["error_code"] == "VALIDATION_EVIDENCE_MISSING"


def test_failed_validation_allows_only_two_targeted_corrections():
    state = AgentExecutionState(skill_count=1)
    _record_verifier(state, "PARAGRAPHS=1")
    action = {
        "action": "record_validation",
            "verification_id": "verified-1",
        "status": "failed",
        "summary": "Semantic paragraphing is missing",
        "evidence": "verify.py reported PARAGRAPHS=1",
        "checks": ["PARAGRAPHS=1"],
    }

    first = state.record_validation(action, artifact_snapshot=ARTIFACT_SNAPSHOT)
    second = state.record_validation(action, artifact_snapshot=ARTIFACT_SNAPSHOT)
    third = state.record_validation(action, artifact_snapshot=ARTIFACT_SNAPSHOT)
    assert first["retry_allowed"] is True
    assert second["retry_allowed"] is True
    assert third["retry_allowed"] is False


def test_checkpoint_preserves_plan_validation_skills_and_observations():
    state = AgentExecutionState(
        skill_count=1,
        loaded_skills={1},
        completed_skill_indexes={1},
    )
    state.update_plan(_completed_plan())
    _record_verifier(state)
    _pass_validation(state)

    checkpoint = state.checkpoint()
    assert '"loaded_skill_indexes":[1]' in checkpoint
    assert '"validation_step_id":"verify"' in checkpoint
    assert '"status":"passed"' in checkpoint
    assert '"tool":"command"' in checkpoint


def test_explicit_multi_skill_loading_and_completion_follow_user_order():
    state = AgentExecutionState(skill_count=2, ordered_skills=True)
    contexts = [
        {"name": "First", "version": "1", "root": "/workspace/skills/1", "skill_md": "# First"},
        {"name": "Second", "version": "1", "root": "/workspace/skills/2", "skill_md": "# Second"},
    ]

    assert state.read_skill(2, contexts)["error_code"] == "SKILL_ORDER_INVALID"
    assert state.read_skill(1, contexts)["ok"] is True
    assert state.complete_skill(1, "first output")["ok"] is True
    assert state.read_skill(2, contexts)["ok"] is True
    assert state.complete_skill(2, "second output")["ok"] is True


def test_successful_validation_syncs_plan_without_an_extra_model_turn():
    state = AgentExecutionState(skill_count=1, loaded_skills={1}, completed_skill_indexes={1})
    assert state.update_plan(_completed_plan())["ok"]
    _record_verifier(state)
    result = _pass_validation(state)
    assert result['validation_step_completed'] is True


def test_reference_path_classification_covers_skill_roots_and_input_only():
    from app.agent_policy import is_reference_path
    roots = ["/workspace/skills/01-demo/demo", "/workspace/skills/01-demo"]
    assert is_reference_path("/workspace/skills/01-demo/demo/references/a.md", roots)
    assert is_reference_path("/workspace/skills/01-demo/demo/scripts/build.py", roots)
    assert is_reference_path("/workspace/skills/01-demo/demo/SKILL.md", roots)
    assert is_reference_path("/workspace/input/brief.docx.txt", roots)
    # Generated working directories inside the package are mutable.
    assert not is_reference_path("/workspace/skills/01-demo/demo/assets/sources/x.md", roots)
    assert not is_reference_path("/workspace/skills/01-demo/demo/exports/p.pptx", roots)
    assert not is_reference_path("/workspace/work/notes.txt", roots)
    assert not is_reference_path("/workspace/output/report.docx", roots)
    assert not is_reference_path("/workspace/skills/../etc/passwd", roots)
    assert not is_reference_path("references/a.md", roots)


def test_reference_pin_then_cached_slice_served_without_reread():
    import json
    state = AgentExecutionState(skill_count=1)
    roots = ["/workspace/skills/01-demo/demo"]
    spec = "## 设计规范\n" + "必须遵循的条目。\n" * 200

    # Complete first read from offset 0 pins the whole document.
    first = state.reference_pin(
        "/workspace/skills/01-demo/demo/references/spec.md", spec, roots,
        offset=0, limit=30000,
    )
    assert first is not None and first["ok"] and first["retained"]
    assert first["content"] == spec
    assert first["sha256"] and first["bytes"] == len(spec.encode("utf-8"))

    # Later reads — including explicit offset/limit slices the model prefers —
    # are served from the pinned text without any sandbox read.
    second = state.reference_cached(
        "/workspace/skills/01-demo/demo/references/spec.md", 2000, 30000, roots
    )
    assert second["cached"] and second["unchanged"]
    assert second["content"] == spec[2000:32000]
    assert second["chars"] == len(spec)
    assert second["sha256"] == first["sha256"]

    # Shelf is pinned into the checkpoint memory with the full text.
    shelf = json.loads(state.checkpoint())["reference_shelf"]
    assert len(shelf) == 1 and shelf[0]["content"] == spec


def test_reference_pin_ignores_mutable_data_partial_reads_and_oversized_files():
    from app.agent_policy import REFERENCE_ENTRY_CAP
    state = AgentExecutionState(skill_count=1)
    roots = ["/workspace/skills/01-demo/demo"]
    # Mutable locations never pin.
    assert state.reference_pin("/workspace/work/scratch.txt", "data", roots, offset=0, limit=30000) is None
    assert state.reference_pin("/workspace/output/r.txt", "data", roots, offset=0, limit=30000) is None
    assert state.reference_pin("/workspace/skills/01-demo/demo/assets/sources/x.md",
                               "data", roots, offset=0, limit=30000) is None
    # Non-zero offset slices cannot prove the whole file was seen.
    assert state.reference_pin("/workspace/skills/01-demo/demo/references/a.md",
                               "part", roots, offset=600, limit=6000) is None
    # A read that hit the response limit is a truncated page, not a full file.
    assert state.reference_pin("/workspace/skills/01-demo/demo/references/b.md",
                               "x" * 100, roots, offset=0, limit=100) is None
    # Oversized files keep the ordinary paged path.
    assert state.reference_pin("/workspace/skills/01-demo/demo/huge.md",
                               "x" * (REFERENCE_ENTRY_CAP + 1), roots,
                               offset=0, limit=30000) is None
    assert state.reference_shelf == {}
    # Cache misses return None even for a reference path.
    assert state.reference_cached("/workspace/skills/01-demo/demo/references/c.md",
                                  0, 30000, roots) is None


def test_reference_shelf_refreshes_changed_content_and_evicts_oldest_first(monkeypatch):
    from app import agent_policy
    monkeypatch.setattr(agent_policy, "REFERENCE_SHELF_CAP", 3000)
    state = AgentExecutionState(skill_count=1)
    roots = ["/workspace/skills/01-demo/demo"]
    base = "/workspace/skills/01-demo/demo/references"

    def pin(name, text):
        return state.reference_pin(f"{base}/{name}", text, roots, offset=0, limit=30000)

    first = pin("a.md", "A" * 1200)
    pin("b.md", "B" * 1200)
    pin("c.md", "C" * 1200)
    assert f"{base}/a.md" not in state.reference_shelf  # oldest evicted
    assert f"{base}/c.md" in state.reference_shelf

    changed = pin("c.md", "D" * 1200)
    assert changed["retained"] and changed["content"].startswith("D")
    # Reading c fresh moved it to the MRU end; b is now the oldest entry.
    assert list(state.reference_shelf)[0] == f"{base}/b.md"
    assert list(state.reference_shelf)[-1] == f"{base}/c.md"
    assert first["sha256"] != changed["sha256"]


def test_reference_shelf_survives_durable_state_roundtrip():
    import json
    from app.durable_checkpoint import state_from_json, state_to_json
    state = AgentExecutionState(skill_count=1)
    roots = ["/workspace/skills/01-demo/demo"]
    state.reference_pin("/workspace/skills/01-demo/demo/SKILL.md", "# Guide 内容",
                        roots, offset=0, limit=30000)

    encoded = json.loads(json.dumps(state_to_json(state)))
    restored = state_from_json(encoded)
    assert restored.reference_shelf == state.reference_shelf
    # Snapshots from older code without a shelf restore to an empty dict.
    legacy = {key: value for key, value in encoded.items() if key != "reference_shelf"}
    assert state_from_json(legacy).reference_shelf == {}


def test_validation_does_not_complete_unfinished_business_steps():
    state = AgentExecutionState(skill_count=1)
    plan = _completed_plan()
    plan['steps'][0]['status'] = 'in_progress'
    assert state.update_plan(plan)['ok']
    _record_verifier(state)
    result = _pass_validation(state)
    assert result['validation_step_completed'] is False
    assert state.plan['steps'][0]['status'] == 'in_progress'
    assert state.plan['steps'][-1]['status'] == 'pending'
