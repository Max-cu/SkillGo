"""Framework anti-loop guards: duplicate observations and verifier thrash."""

from __future__ import annotations

import json

from app.agent_policy import AgentExecutionState
from app import durable_checkpoint as cp


def test_stable_cache_survives_unrelated_write():
    state = AgentExecutionState(skill_count=1)
    read = {"action": "read_file", "path": "/workspace/work/design_spec.md", "offset": 0, "limit": 30000}
    state.record(read, {"ok": True, "path": "/workspace/work/design_spec.md", "content": "spec", "complete": True})
    assert state.cached_observation(read) is not None
    assert state.stable_cached_observation(read) is not None

    state.record(
        {"action": "write_file", "path": "/workspace/work/page-1.svg"},
        {"ok": True, "path": "/workspace/work/page-1.svg", "bytes": 12},
    )
    # Mutation-epoch cache clears, stable cache for untouched path remains.
    assert state.cached_observation(read) is None
    stable = state.stable_cached_observation(read)
    assert isinstance(stable, dict)
    assert stable.get("stable_cache") is True
    assert stable.get("content") == "spec"


def test_write_to_same_path_invalidates_stable_cache():
    state = AgentExecutionState(skill_count=1)
    read = {"action": "read_file", "path": "/workspace/work/design_spec.md"}
    state.record(read, {"ok": True, "content": "v1"})
    state.record(
        {"action": "write_file", "path": "/workspace/work/design_spec.md"},
        {"ok": True, "path": "/workspace/work/design_spec.md", "bytes": 2},
    )
    assert state.stable_cached_observation(read) is None


def test_duplicate_observation_streak_blocks():
    state = AgentExecutionState(skill_count=1)
    read = {"action": "read_file", "path": "/workspace/work/design_spec.md"}
    state.record(read, {"ok": True, "content": "spec", "complete": True})

    assert state.register_observation_access(read, reused=True) is None  # streak 1
    assert state.duplicate_observation_streak == 1
    assert state.register_observation_access(read, reused=True) is None  # streak 2
    assert state.observation_loop_nudge()
    blocked = state.register_observation_access(read, reused=True)  # streak 3
    assert blocked is not None
    assert blocked["error_code"] == "OBSERVATION_LOOP_BREAK"


def test_verifier_retry_without_progress_blocked():
    state = AgentExecutionState(skill_count=1)
    state.progress_epoch = 3
    failed = {"ok": False, "checks": [{"requirement_id": "r1", "passed": False, "observed": "bad"}]}
    annotated = state.register_verifier_failure(failed)
    assert annotated["verifier_fail_streak"] == 1
    assert state.verifier_retry_blocked() is not None

    # Progress then allows another attempt, but same signature nudges batching.
    state.progress_epoch = 4
    state.mutation_epoch += 1
    assert state.verifier_retry_blocked() is None
    annotated = state.register_verifier_failure(failed)
    assert annotated["same_failure_signature"] is True
    assert "BATCH" in annotated["hint"] or "Batch" in annotated["hint"] or "cohesive" in annotated["hint"]


def test_verifier_success_resets_streak():
    state = AgentExecutionState(skill_count=1)
    state.register_verifier_failure({"ok": False, "checks": [{"requirement_id": "r1", "passed": False}]})
    state.register_verifier_success()
    assert state.verifier_fail_streak == 0
    assert state.verifier_retry_blocked() is None


def test_anti_loop_fields_roundtrip_without_private_cache():
    state = AgentExecutionState(skill_count=1)
    state.duplicate_observation_streak = 2
    state.verifier_fail_streak = 1
    state.last_verifier_fail_signature = "abc"
    state.verifier_anchor_progress_epoch = 7
    state._stable_path_cache["read_file:/workspace/work/x"] = {"ok": True, "content": "secret"}
    blob = cp.state_to_json(state)
    assert "_stable_path_cache" not in blob
    assert blob["duplicate_observation_streak"] == 2
    restored = cp.state_from_json(json.loads(json.dumps(blob)))
    assert restored.duplicate_observation_streak == 2
    assert restored.verifier_fail_streak == 1
    assert restored._stable_path_cache == {}
