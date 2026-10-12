import json

from app.agent_policy import AgentExecutionState
from app.durable_checkpoint import state_from_json, state_to_json


def outline():
    return {"goal": "Create the requested file", "steps": [
        {"id": "inspect", "title": "Inspect input", "status": "pending"},
        {"id": "produce", "title": "Produce output", "status": "pending", "depends_on": ["inspect"]},
        {"id": "verify", "title": "Verify", "status": "pending", "depends_on": ["produce"]},
    ], "success_criteria": ["Requested output exists"], "validation_step_id": "verify"}


def test_outline_keeps_initial_plan_required_and_executes_without_ticks():
    state = AgentExecutionState(skill_count=1, plan_status_tracking=False, delivery_only=True, loaded_skills={1})
    assert state.ensure_active_step()["error_code"] == "PLAN_REQUIRED"
    assert state.finish_blocker(current_artifacts={}) == "Create a concise execution plan before finish."
    assert state.update_plan(outline())["ok"]
    assert state.ensure_active_step()["ok"]
    state.record({"action": "command", "argv": ["python3", "produce.py"]}, {"exit_code": 0, "stdout": "Produced file"})
    assert state.finish_blocker(current_artifacts={"/workspace/output/report.pptx": "hash"}) is None
    assert all(s["status"] == "pending" for s in state.plan["steps"])
    assert state.plan["tracking_mode"] == "outline"


def test_outline_does_not_fabricate_completion_on_failure():
    state = AgentExecutionState(skill_count=1, plan_status_tracking=False)
    state.update_plan(outline())
    state.record({"action": "command", "argv": ["python3", "produce.py"]}, {"exit_code": 1, "stderr": "Failed"})
    assert all(s["status"] == "pending" for s in state.plan["steps"])
    assert not state.observations[-1]["ok"]


def test_outline_preserves_full_verification_and_multiskill_gates():
    state = AgentExecutionState(skill_count=1, plan_status_tracking=False, loaded_skills={1})
    state.update_plan(outline())
    assert "run_verifier" in state.finish_blocker(current_artifacts={})
    state.skill_count = 2
    assert "unread" in state.finish_blocker(current_artifacts={})
    state.loaded_skills.add(2)
    assert "incomplete skill" in state.finish_blocker(current_artifacts={})


def test_outline_replanning_preserves_requirements_and_rejects_cycles():
    state = AgentExecutionState(skill_count=1, plan_status_tracking=False)
    assert state.update_plan(outline())["ok"]
    change = outline()
    change["steps"][1]["title"] = "Produce revised output"
    assert state.update_plan(change)["ok"]
    change["success_criteria"] = ["Different requirement"]
    assert state.update_plan(change)["error_code"] == "PLAN_REQUIREMENT_REMOVED"
    change = outline()
    change["steps"][0]["depends_on"] = ["produce"]
    assert state.update_plan(change)["error_code"] == "PLAN_DEPENDENCY_INVALID"


def test_outline_snapshot_roundtrip_and_older_snapshot_compatibility():
    state = AgentExecutionState(skill_count=1, plan_status_tracking=False)
    state.update_plan(outline())
    restored = state_from_json(json.loads(json.dumps(state_to_json(state))))
    assert restored.plan_status_tracking is False
    assert restored.ensure_active_step()["ok"]
    old = state_to_json(AgentExecutionState(skill_count=1))
    del old["plan_status_tracking"]
    assert state_from_json(old).plan_status_tracking is True


def test_outline_ignores_model_checkbox_claims():
    state = AgentExecutionState(skill_count=1, plan_status_tracking=False)
    plan = outline()
    for step in plan["steps"]:
        step.update(status="completed", evidence="Claimed done")
    assert state.update_plan(plan)["ok"]
    assert all(s["status"] == "pending" and not s["evidence"] for s in state.plan["steps"])
