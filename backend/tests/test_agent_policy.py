from __future__ import annotations

from app.agent_policy import AgentExecutionState, action_fingerprint


def _completed_plan() -> dict:
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
                "status": "completed",
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


def _pass_validation(state: AgentExecutionState) -> dict:
    return state.record_validation(
        {
            "action": "record_validation",
            "status": "passed",
            "summary": "The concentrated verifier passed",
            "evidence": "verify.py exit 0 with observed values",
            "checks": ["PARAGRAPHS=4", "TITLE_FONT=方正小标宋简体"],
        }
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


def test_plan_requires_evidence_for_completed_steps():
    state = AgentExecutionState(skill_count=1)
    action = _completed_plan()
    action["steps"][0]["evidence"] = ""
    result = state.update_plan(action)
    assert result["error_code"] == "PLAN_EVIDENCE_REQUIRED"


def test_validation_requires_a_real_recent_tool_observation():
    state = AgentExecutionState(skill_count=1)
    result = _pass_validation(state)
    assert result["error_code"] == "VALIDATION_EVIDENCE_MISSING"


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
    assert "execution plan" in (state.finish_blocker() or "")
    assert state.update_plan(_completed_plan())["ok"] is True
    assert "incomplete skill indexes" in (state.finish_blocker() or "")
    assert state.complete_skill(1, "/workspace/output/result.docx")["ok"] is True
    assert "record_validation" in (state.finish_blocker() or "")
    _record_verifier(state)
    assert _pass_validation(state)["ok"] is True
    assert state.finish_blocker() is None


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
    assert "record_validation" in (state.finish_blocker() or "")


def test_failed_validation_allows_only_two_targeted_corrections():
    state = AgentExecutionState(skill_count=1)
    _record_verifier(state, "PARAGRAPHS=1")
    action = {
        "action": "record_validation",
        "status": "failed",
        "summary": "Semantic paragraphing is missing",
        "evidence": "verify.py reported PARAGRAPHS=1",
        "checks": ["PARAGRAPHS=1"],
    }

    first = state.record_validation(action)
    second = state.record_validation(action)
    third = state.record_validation(action)
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
