"""Opt-in fast projection: model context shrinks; durable history does not."""
import copy
import hashlib
import inspect
import json
from types import SimpleNamespace

from app.agent_context import estimate_tokens, project_context
from app.config import Settings
from app.context_thinning import ThinContextPolicy, resolve_thin_context_policy
from app.sandbox_agent_loop import _run_agent_loop


def _project(messages, checkpoint, budget, policy, diagnostics=None):
    return project_context(
        messages,
        checkpoint=json.dumps(checkpoint),
        skill_contexts=[],
        loaded=set(),
        completed=set(),
        max_tokens=budget,
        diagnostics=diagnostics,
        thin=policy,
    )


def _exchange(index, payload):
    return [
        {
            "role": "assistant",
            "reasoning_content": f"reason-{index}",
            "tool_calls": [{
                "id": f"call-{index}",
                "function": {"name": "command", "arguments": json.dumps({"argv": ["echo", str(index)]})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": f"call-{index}",
            "content": json.dumps({"tool_result": "command", "payload": payload}, ensure_ascii=False),
        },
    ]


def _fast(**overrides):
    values = dict(mode="fast", max_exchanges=4, compact_older_tools=True, thin_reference_shelf=True)
    values.update(overrides)
    return ThinContextPolicy(**values)


def _audit():
    return ThinContextPolicy()


def _long_run():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    bodies = []
    for index in range(8):
        stdout = f"TURN{index}-" + ("<path d='M1 2'/>" * 500) + f"-END{index}"
        body = {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": ""}
        bodies.append(body)
        messages.extend(_exchange(index, body))
    return messages, bodies


def test_fast_mode_shrinks_a_multi_tool_run_and_keeps_durable_history():
    messages, bodies = _long_run()
    before = copy.deepcopy(messages)
    audit_diag, fast_diag = {}, {}
    audit = _project(messages, {}, 500_000, _audit(), audit_diag)
    fast = _project(messages, {}, 500_000, _fast(), fast_diag)

    audit_tokens = estimate_tokens(audit)
    fast_tokens = estimate_tokens(fast)
    assert fast_tokens < audit_tokens * 0.5
    assert fast_diag["summarized_count"] == 3
    assert fast_diag["removed_tokens"] > 1000
    assert fast_diag["preserved_exchange_count"] == 4
    assert fast_diag["exchange_cap_dropped_count"] == 4
    assert fast_diag["dropped_exchange_count"] == 4
    assert audit_diag["summarized_count"] == 0
    assert audit_diag["preserved_exchange_count"] == 8
    assert messages == before

    latest = json.loads(fast[-2]["content"])["payload"]["stdout"]
    assert latest == bodies[-1]["stdout"]
    older = json.loads(fast[-4]["content"])
    assert older["context_compacted"] is True
    assert "sha256=" in older["payload"]["stdout"]
    assert "omitted from model context" in older["payload"]["stdout"]
    assert bodies[-2]["stdout"] not in older["payload"]["stdout"]
    assert f"END{6}" in older["payload"]["stdout"]
    assert fast[-4]["tool_call_id"] == "call-6"
    assert fast[-5]["tool_calls"][0]["id"] == "call-6"
    assert fast[-5]["reasoning_content"] == "reason-6"


def test_older_offloaded_stdout_keeps_workspace_path_and_sha():
    stdout = "HEAD-" + ("x" * 2000) + "-TAIL"
    digest_source = stdout.encode()
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "request"},
        {"role": "assistant", "tool_calls": [{"id": "old"}]},
        {
            "role": "tool",
            "tool_call_id": "old",
            "content": json.dumps({
                "tool_result": "command",
                "payload": {
                    "exit_code": 0,
                    "stdout": stdout,
                    "full_result_path": "/workspace/work/tool-results/turn-3-op-1.json",
                    "sha256": "f" * 64,
                },
            }),
        },
        {"role": "assistant", "tool_calls": [{"id": "new"}]},
        {"role": "tool", "tool_call_id": "new", "content": '{"tool_result":"command","payload":{"ok":true}}'},
    ]
    projected = _project(messages, {}, 500_000, _fast(max_exchanges=None, thin_reference_shelf=False))
    payload = json.loads(projected[3]["content"])["payload"]
    assert payload["full_result_path"] == "/workspace/work/tool-results/turn-3-op-1.json"
    assert payload["full_result_path"] in payload["stdout"]
    assert "HEAD-" in payload["stdout"] and "-TAIL" in payload["stdout"]
    assert "x" * 2000 not in payload["stdout"]
    assert hashlib.sha256(digest_source).hexdigest() in payload["stdout"]
    assert messages[3]["content"].count("x") > projected[3]["content"].count("x")


def test_latest_exchange_and_assistant_arguments_stay_intact():
    code = "print('svg')\n" * 400
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "request"},
        {
            "role": "assistant",
            "reasoning_content": "private reasoning",
            "tool_calls": [{
                "id": "old",
                "function": {"name": "run_python", "arguments": json.dumps({"code": code})},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "old",
            "content": json.dumps({
                "tool_result": "run_python",
                "payload": {"exit_code": 0, "stdout": "OLD-" + ("line\n" * 400) + "-TAIL"},
            }),
        },
        {"role": "assistant", "content": "latest"},
        {"role": "user", "content": "latest-result"},
    ]
    before = copy.deepcopy(messages)
    projected = _project(messages, {}, 500_000, _fast(max_exchanges=None))
    assert projected[2] is messages[2]
    assert json.loads(projected[2]["tool_calls"][0]["function"]["arguments"])["code"] == code
    assert "private reasoning" == projected[2]["reasoning_content"]
    assert "omitted from model context" in projected[3]["content"]
    assert projected[-3]["content"] == "latest"
    assert projected[-2]["content"] == "latest-result"
    assert messages == before


def test_pinned_reference_inside_an_older_exchange_is_not_compacted():
    spec = "必须遵守的规范。" * 400
    payload = {
        "ok": True,
        "path": "/workspace/skills/demo/references/spec.md",
        "reference": True,
        "retained": True,
        "complete": True,
        "sha256": "abc",
        "content": spec,
    }
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "request"},
        {"role": "assistant", "tool_calls": [{"id": "ref"}]},
        {
            "role": "tool",
            "tool_call_id": "ref",
            "content": json.dumps({"tool_result": "read_file", "payload": payload}, ensure_ascii=False),
        },
        {"role": "assistant", "tool_calls": [{"id": "now"}]},
        {"role": "tool", "tool_call_id": "now", "content": json.dumps({"tool_result": "command", "payload": {"ok": True}})},
    ]
    projected = _project(messages, {}, 500_000, _fast(max_exchanges=None))
    body = json.loads(projected[3]["content"])
    assert body["payload"]["content"] == spec
    assert "context_compacted" not in body


def test_deep_reference_shelf_becomes_path_and_sha_while_ordinary_specs_stay():
    deep = "DEEP_RULE " * 800
    ordinary = "SHORT_SPEC keep the whole instruction"
    digest = "d" * 64
    checkpoint = {
        "requirements": ["follow the spec"],
        "reference_shelf": [
            {
                "path": "/workspace/skills/ppt/references/deep/contract_elements.md",
                "sha256": digest,
                "bytes": len(deep.encode()),
                "chars": len(deep),
                "content": deep,
            },
            {
                "path": "/workspace/skills/ppt/references/spec.md",
                "sha256": "e" * 64,
                "bytes": len(ordinary.encode()),
                "chars": len(ordinary),
                "content": ordinary,
            },
        ],
    }
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "request"}]
    audit = _project(messages, checkpoint, 500_000, _audit())
    fast = _project(messages, checkpoint, 500_000, _fast())
    audit_memory = audit[-1]["content"]
    fast_memory = fast[-1]["content"]
    assert deep in audit_memory
    assert deep not in fast_memory
    assert "content_omitted" in fast_memory
    assert digest in fast_memory
    assert ordinary in fast_memory
    assert estimate_tokens(fast) < estimate_tokens(audit)


def test_short_tool_results_and_audit_default_do_not_compact():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "request"}]
    messages.extend(_exchange(1, {"ok": True, "stdout": "tiny"}))
    messages.extend(_exchange(2, {"ok": True, "stdout": "also tiny"}))
    before = copy.deepcopy(messages)
    projected = _project(messages, {}, 500_000, _fast(max_exchanges=None))
    assert projected[:-1] == messages == before


def test_resolve_policy_uses_fast_defaults_and_explicit_overrides():
    fast = resolve_thin_context_policy(SimpleNamespace(
        orchestration_mode="fast",
        context_max_exchanges=None,
        compact_older_tools=None,
        thin_reference_shelf=None,
    ))
    assert fast.mode == "fast"
    assert fast.max_exchanges == 4
    assert fast.compact_older_tools is True
    assert fast.thin_reference_shelf is True

    overridden = resolve_thin_context_policy(SimpleNamespace(
        orchestration_mode=" fast ",
        context_max_exchanges=0,
        compact_older_tools=False,
        thin_reference_shelf=False,
    ))
    assert overridden.mode == "fast"
    assert overridden.max_exchanges is None
    assert overridden.compact_older_tools is False
    assert overridden.thin_reference_shelf is False

    audit = resolve_thin_context_policy(SimpleNamespace(
        orchestration_mode="audit",
        context_max_exchanges=2,
        compact_older_tools=None,
        thin_reference_shelf=None,
    ))
    assert audit.max_exchanges == 2
    assert audit.compact_older_tools is False
    assert audit.thin_reference_shelf is False


def test_explicit_exchange_cap_can_apply_without_compaction():
    messages, _bodies = _long_run()
    diag = {}
    projected = _project(
        messages, {}, 500_000,
        ThinContextPolicy(mode="audit", max_exchanges=2, compact_older_tools=False),
        diag,
    )
    assert diag["preserved_exchange_count"] == 2
    assert diag["exchange_cap_dropped_count"] == 6
    assert diag["summarized_count"] == 0
    assert "TURN7-" in projected[-2]["content"]
    assert "TURN6-" in projected[-4]["content"]
    assert "omitted from model context" not in json.dumps(projected)


def test_thin_context_does_not_gate_durable_snapshots():
    source = inspect.getsource(_run_agent_loop)
    settings_source = inspect.getsource(Settings)
    assert "save_checkpoint" in source
    assert "DURABLE_CHECKPOINT_EVERY" not in source
    assert "SKILLGO_DURABLE_CHECKPOINTS_ENABLED" in settings_source
    assert "DURABLE_CHECKPOINT_EVERY" not in settings_source
    assert "orchestration_mode" in settings_source
