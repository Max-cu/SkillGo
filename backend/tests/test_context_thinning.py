"""Opt-in fast projection: model context shrinks; durable history does not."""
import copy
import hashlib
import inspect
import json
import logging
from types import SimpleNamespace

import pytest

from app.agent_context import estimate_tokens, project_context
from app.config import Settings
from app.context_thinning import (
    DEFAULT_FAST_PIN_PATTERNS,
    DEFAULT_FAST_PIN_READS,
    DEFAULT_FAST_PIN_TOKEN_CEILING,
    DEFAULT_FAST_PIN_TOOLS,
    ThinContextPolicy,
    resolve_thin_context_policy,
)
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
    assert fast.pin_tools == DEFAULT_FAST_PIN_TOOLS
    assert fast.pin_tool_priorities[0] == 0
    assert fast.pin_tool_priorities[1:] == (1, 1, 1)
    assert fast.pin_patterns == DEFAULT_FAST_PIN_PATTERNS
    assert fast.pin_reads == DEFAULT_FAST_PIN_READS
    assert "assets/design_spec.md" in fast.pin_reads
    assert fast.pin_token_ceiling == DEFAULT_FAST_PIN_TOKEN_CEILING

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
    assert audit.pin_tools == ()
    assert audit.pin_patterns == ()
    assert audit.pin_reads == ()


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
    assert "SKILLGO_FAST_PIN_TOOLS" in settings_source
    assert "SKILLGO_FAST_PIN_READS" in settings_source
    assert "SKILLGO_FAST_PIN_TOKEN_CEILING" in settings_source


def _tool_exchange(index, argv, payload, *, tool_name="command"):
    return [
        {
            "role": "assistant",
            "reasoning_content": f"reason-{index}",
            "tool_calls": [{
                "id": f"call-{index}",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps({"argv": argv}, ensure_ascii=False),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": f"call-{index}",
            "content": json.dumps(
                {"tool_result": tool_name, "payload": payload},
                ensure_ascii=False,
            ),
        },
    ]


def _long_stdout(marker, chars=1200):
    return f"{marker}-" + ("x" * chars) + f"-END{marker}"


def _pin_policy(**overrides):
    values = dict(
        pin_tools=DEFAULT_FAST_PIN_TOOLS,
        pin_patterns=DEFAULT_FAST_PIN_PATTERNS,
        pin_reads=DEFAULT_FAST_PIN_READS,
        pin_token_ceiling=DEFAULT_FAST_PIN_TOKEN_CEILING,
    )
    values.update(overrides)
    return _fast(**values)


def _deck_run(exchanges):
    """exchanges is a list of (argv, payload) in order."""

    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    for index, (argv, payload) in enumerate(exchanges):
        messages.extend(_tool_exchange(index, argv, payload))
    return messages


def _render(index):
    return (
        ["python3", "scripts/render.py"],
        {"ok": True, "exit_code": 0, "stdout": _long_stdout(f"TURN{index}"), "stderr": ""},
    )


def _tool_body(projected, call_id):
    matches = [message for message in projected if message.get("tool_call_id") == call_id]
    assert len(matches) == 1
    return json.loads(matches[0]["content"])


@pytest.mark.parametrize("script", [
    "scripts/validators/count_photos.py",
    "scripts/fetch_web_images.py",
    "scripts/Fetch_Web_Images.py",
    "scripts/inject_image_credits.py",
    "scripts/icon_sync.py",
])
def test_pinned_tool_result_survives_cap_and_is_not_compacted(script):
    photo_stdout = _long_stdout("PHOTO_LIST") + "\n" + "\n".join(
        f"assets/sources/image_{i:03d}.png" for i in range(4)
    )
    exchanges = []
    for index in range(8):
        if index == 1:
            exchanges.append((
                ["python3", script],
                {"ok": True, "exit_code": 0, "stdout": photo_stdout, "stderr": ""},
            ))
        else:
            exchanges.append(_render(index))
    messages = _deck_run(exchanges)
    before = copy.deepcopy(messages)
    diag = {}
    projected = _project(messages, {}, 500_000, _pin_policy(), diag)

    assert messages == before
    photo = _tool_body(projected, "call-1")
    assert photo["payload"]["stdout"] == photo_stdout
    assert "context_compacted" not in photo
    assert "omitted from model context" not in json.dumps(photo)
    # The pin is extra: the original 4-exchange window is still there.
    assert diag["preserved_exchange_count"] == 5
    assert diag["exchange_cap_dropped_count"] == 3
    assert diag["pinned_result_count"] == 1
    assert 0 < diag["pinned_result_estimated_tokens"] <= DEFAULT_FAST_PIN_TOKEN_CEILING
    assert diag["pinned_rescued_exchange_tokens"] >= diag["pinned_result_estimated_tokens"]
    assert diag["pinned_ceiling_dropped_count"] == 0
    assert diag["pinned_estimated_tokens"] != diag["pinned_result_estimated_tokens"]
    for call_id in ("call-4", "call-5", "call-6", "call-7"):
        assert any(message.get("tool_call_id") == call_id for message in projected)
    for call_id in ("call-0", "call-2", "call-3"):
        assert all(message.get("tool_call_id") != call_id for message in projected)
    # Non-pinned tool text inside the retained window is still compacted.
    older = _tool_body(projected, "call-4")
    assert older["context_compacted"] is True
    assert "omitted from model context" in older["payload"]["stdout"]
    assert "x" * 1200 not in older["payload"]["stdout"]
    latest = _tool_body(projected, "call-7")
    assert latest["payload"]["stdout"].startswith("TURN7-")
    assert "omitted from model context" not in latest["payload"]["stdout"]


def test_in_window_pinned_result_is_not_compacted():
    photo_stdout = _long_stdout("IN_WINDOW_PHOTO")
    exchanges = [_render(index) for index in range(8)]
    exchanges[5] = (
        ["python3", "scripts/count_photos.py"],
        {"ok": True, "exit_code": 0, "stdout": photo_stdout, "stderr": ""},
    )
    messages = _deck_run(exchanges)
    diag = {}
    projected = _project(messages, {}, 500_000, _pin_policy(), diag)
    assert _tool_body(projected, "call-5")["payload"]["stdout"] == photo_stdout
    assert _tool_body(projected, "call-4")["context_compacted"] is True
    # Index 5 is already inside the last 4, so the pin is not an extra exchange.
    assert diag["preserved_exchange_count"] == 4
    assert diag["exchange_cap_dropped_count"] == 4
    assert diag["pinned_result_count"] == 1
    assert diag["pinned_result_estimated_tokens"] <= DEFAULT_FAST_PIN_TOKEN_CEILING
    assert diag["pinned_rescued_exchange_tokens"] == 0


def test_rescued_exchange_keeps_the_pinned_result_and_drops_the_sibling():
    photo_stdout = _long_stdout("PINNED_PHOTO")
    sibling_stdout = _long_stdout("SIBLING_LOG")
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    messages.extend(_tool_exchange(0, ["python3", "scripts/render.py"], {
        "ok": True, "stdout": _long_stdout("TURN0"), "stderr": "",
    }))
    messages.append({
        "role": "assistant",
        "reasoning_content": "keep this off the rescued stub",
        "content": "also drop the prose",
        "tool_calls": [
            {
                "id": "call-pin",
                "function": {
                    "name": "command",
                    "arguments": json.dumps({"argv": ["python3", "scripts/count_photos.py"]}),
                },
            },
            {
                "id": "call-sib",
                "function": {
                    "name": "command",
                    "arguments": json.dumps({"argv": ["python3", "scripts/render.py"]}),
                },
            },
        ],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "call-pin",
        "content": json.dumps({"tool_result": "command", "payload": {"stdout": photo_stdout, "stderr": ""}}),
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "call-sib",
        "content": json.dumps({"tool_result": "command", "payload": {"stdout": sibling_stdout, "stderr": ""}}),
    })
    for index in range(2, 8):
        messages.extend(_tool_exchange(index, ["python3", "scripts/render.py"], {
            "ok": True, "stdout": _long_stdout(f"TURN{index}"), "stderr": "",
        }))
    diag = {}
    projected = _project(messages, {}, 500_000, _pin_policy(), diag)
    assert _tool_body(projected, "call-pin")["payload"]["stdout"] == photo_stdout
    assert all(message.get("tool_call_id") != "call-sib" for message in projected)
    assert sibling_stdout not in json.dumps(projected)
    stubs = [
        message for message in projected
        if message.get("role") == "assistant" and any(
            isinstance(call, dict) and call.get("id") == "call-pin"
            for call in (message.get("tool_calls") or [])
        )
    ]
    assert len(stubs) == 1
    assert [call["id"] for call in stubs[0]["tool_calls"]] == ["call-pin"]
    assert "reasoning_content" not in stubs[0]
    assert "content" not in stubs[0]
    assert diag["pinned_rescued_exchange_tokens"] >= diag["pinned_result_estimated_tokens"] > 0


def _photo_stdout(tag: str) -> str:
    # Tags are equal length so repeated calls have the same token estimate.
    return f"{tag}-HEAD-" + ("Q" * 1500) + f"-{tag}-MID-" + ("Z" * 1500) + f"-{tag}-TAIL"


def _read_exchange(index, path, content):
    return [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": f"call-{index}",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": path}, ensure_ascii=False),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": f"call-{index}",
            "content": json.dumps({
                "tool_result": "read_file",
                "payload": {"path": path, "content": content},
            }, ensure_ascii=False),
        },
    ]


def test_fallback_signatures_pin_without_the_script_name():
    listing = _long_stdout("LISTING") + "\n✅ 可用照片: 8 张\n[✓] image_001\n"
    web = _long_stdout("WEB") + "\n联网补图 3 张\n[[image_002.jpg]]\n"
    section = _long_stdout("SECTION") + "\n按 §0d 和 §0f 执行，不要钉住章节号\n"
    mentioned = _long_stdout("MENTION") + "\nnext I would run count_photos\n"
    exchanges = [
        (["python3", "scripts/notes.py"], {"ok": True, "stdout": listing, "stderr": ""}),
        (["python3", "scripts/notes.py"], {"ok": True, "stdout": web, "stderr": ""}),
        (["python3", "scripts/step.py"], {"ok": True, "stdout": section, "stderr": ""}),
        (["echo", "§0d"], {"ok": True, "stdout": mentioned, "stderr": ""}),
    ]
    exchanges.extend(_render(index) for index in range(4, 8))
    messages = _deck_run(exchanges)
    diag = {}
    # No command-name pins: only the fallback regexes may match.
    projected = _project(
        messages, {}, 500_000,
        _pin_policy(pin_tools=(), pin_reads=(), max_exchanges=None),
        diag,
    )
    assert _tool_body(projected, "call-0")["payload"]["stdout"] == listing
    assert _tool_body(projected, "call-1")["payload"]["stdout"] == web
    assert _tool_body(projected, "call-2")["context_compacted"] is True
    assert _tool_body(projected, "call-3")["context_compacted"] is True
    assert diag["pinned_result_count"] == 2
    assert "omitted from model context" in _tool_body(projected, "call-4")["payload"]["stdout"]


def test_repeated_count_photos_pins_only_the_latest_and_tokens_stay_flat():
    def build(copies: int):
        exchanges = []
        for index in range(copies):
            exchanges.append((
                ["python3", "scripts/validators/count_photos.py"],
                {"ok": True, "exit_code": 0, "stdout": _photo_stdout(f"{index:02d}"), "stderr": ""},
            ))
        exchanges.append((
            ["python3", "scripts/render.py"],
            {"ok": True, "exit_code": 0, "stdout": _long_stdout("AFTER"), "stderr": ""},
        ))
        messages = _deck_run(exchanges)
        diag = {}
        projected = _project(messages, {}, 500_000, _pin_policy(max_exchanges=None), diag)
        return projected, diag

    projected, diag = build(3)
    latest = _tool_body(projected, "call-2")
    assert latest["payload"]["stdout"] == _photo_stdout("02")
    assert "context_compacted" not in latest
    for call_id, tag in (("call-0", "00"), ("call-1", "01")):
        earlier = _tool_body(projected, call_id)
        assert earlier["context_compacted"] is True
        assert f"{tag}-MID-" not in earlier["payload"]["stdout"]
        assert earlier["payload"]["stdout"] != _photo_stdout(tag)
    assert diag["pinned_result_count"] == 1
    assert diag["pinned_ceiling_dropped_count"] == 0
    tokens_for_three = diag["pinned_result_estimated_tokens"]
    assert tokens_for_three == estimate_tokens(
        [message for message in projected if message.get("tool_call_id") == "call-2"][0]
    )

    _projected_six, diag_six = build(6)
    assert diag_six["pinned_result_count"] == 1
    assert diag_six["pinned_result_estimated_tokens"] == tokens_for_three


def test_icon_sync_stderr_is_pinned_verbatim():
    stderr = _photo_stdout("EE")
    exchanges = [_render(index) for index in range(8)]
    exchanges[5] = (
        ["python3", "scripts/icon_sync.py", "."],
        {"ok": True, "exit_code": 0, "stdout": "", "stderr": stderr},
    )
    projected = _project(_deck_run(exchanges), {}, 500_000, _pin_policy())
    body = _tool_body(projected, "call-5")
    assert body["payload"]["stderr"] == stderr
    assert "context_compacted" not in body
    assert _tool_body(projected, "call-4")["context_compacted"] is True


def test_file_rereads_keep_only_the_latest_including_design_spec():
    spec_path = "/workspace/ppt/assets/design_spec.md"
    text_path = "/workspace/ppt/assets/sources/文本.md"
    assert "assets/sources" not in spec_path
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    messages.extend(_read_exchange(0, text_path, _photo_stdout("T0")))
    messages.extend(_read_exchange(1, spec_path, _photo_stdout("S0")))
    messages.extend(_read_exchange(2, text_path, _photo_stdout("T1")))
    messages.extend(_read_exchange(3, spec_path, _photo_stdout("S1")))
    # Mentioning the path in a later command is not a read and must not
    # replace the pinned design_spec.md body.
    messages.extend(_tool_exchange(4, ["python3", "scripts/render.py", spec_path], {
        "ok": True, "stdout": _photo_stdout("NO"), "stderr": "",
    }))
    messages.extend(_tool_exchange(5, ["python3", "scripts/render.py"], {
        "ok": True, "stdout": _long_stdout("AFTER"), "stderr": "",
    }))
    diag = {}
    projected = _project(messages, {}, 500_000, _pin_policy(max_exchanges=None), diag)
    assert _tool_body(projected, "call-2")["payload"]["content"] == _photo_stdout("T1")
    assert _tool_body(projected, "call-3")["payload"]["content"] == _photo_stdout("S1")
    assert "context_compacted" not in _tool_body(projected, "call-2")
    assert "context_compacted" not in _tool_body(projected, "call-3")
    mention = _tool_body(projected, "call-4")
    assert mention["context_compacted"] is True
    assert "NO-MID-" not in mention["payload"]["stdout"]
    for call_id, tag in (("call-0", "T0"), ("call-1", "S0")):
        earlier = _tool_body(projected, call_id)
        assert earlier["context_compacted"] is True
        assert f"{tag}-MID-" not in earlier["payload"]["content"]
    assert diag["pinned_result_count"] == 2
    kept = diag["pinned_result_estimated_tokens"]
    # Another full re-read of both files does not grow the pinned token total
    # when the newest bodies stay the same size.
    longer = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    longer.extend(_read_exchange(0, text_path, _photo_stdout("T0")))
    longer.extend(_read_exchange(1, spec_path, _photo_stdout("S0")))
    longer.extend(_read_exchange(2, text_path, _photo_stdout("T1")))
    longer.extend(_read_exchange(3, spec_path, _photo_stdout("S1")))
    longer.extend(_read_exchange(4, text_path, _photo_stdout("T2")))
    longer.extend(_read_exchange(5, spec_path, _photo_stdout("S2")))
    longer.extend(_tool_exchange(6, ["python3", "scripts/render.py"], {
        "ok": True, "stdout": _long_stdout("TAIL"), "stderr": "",
    }))
    longer_diag = {}
    longer_projected = _project(longer, {}, 500_000, _pin_policy(max_exchanges=None), longer_diag)
    assert longer_diag["pinned_result_count"] == 2
    assert longer_diag["pinned_result_estimated_tokens"] == kept
    assert _tool_body(longer_projected, "call-4")["payload"]["content"] == _photo_stdout("T2")
    assert _tool_body(longer_projected, "call-5")["payload"]["content"] == _photo_stdout("S2")


def test_pin_ceiling_prefers_count_photos_over_newer_file_reads(caplog):
    caplog.set_level(logging.INFO, logger="app.agent_context")
    photo = _photo_stdout("PH")
    spec = _photo_stdout("DS")
    sample = _tool_exchange(0, ["python3", "scripts/count_photos.py"], {
        "ok": True, "stdout": photo, "stderr": "",
    })[1]
    one_result = estimate_tokens(sample)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    messages.extend(_tool_exchange(0, ["python3", "scripts/validators/count_photos.py"], {
        "ok": True, "stdout": photo, "stderr": "",
    }))
    messages.extend(_read_exchange(1, "/workspace/ppt/assets/design_spec.md", spec))
    messages.extend(_tool_exchange(2, ["python3", "scripts/render.py"], {
        "ok": True, "stdout": _long_stdout("AFTER"), "stderr": "",
    }))
    diag = {}
    projected = _project(
        messages, {}, 500_000,
        _pin_policy(max_exchanges=None, pin_token_ceiling=one_result),
        diag,
    )
    assert _tool_body(projected, "call-0")["payload"]["stdout"] == photo
    assert "context_compacted" not in _tool_body(projected, "call-0")
    spec_body = _tool_body(projected, "call-1")
    assert spec_body["context_compacted"] is True
    assert "DS-MID-" not in spec_body["payload"]["content"]
    assert diag["pinned_result_count"] == 1
    assert diag["pinned_result_estimated_tokens"] == one_result
    assert diag["pinned_result_estimated_tokens"] <= one_result
    assert diag["pinned_ceiling_dropped_count"] == 1
    assert "fast pin token ceiling exceeded" in caplog.text
    assert "dropped 1 older pinned result" in caplog.text


def test_later_ls_of_web_images_does_not_displace_count_photos():
    photo = _photo_stdout("PH")
    photo_exchange = _tool_exchange(0, ["python3", "scripts/validators/count_photos.py"], {
        "ok": True, "stdout": photo, "stderr": "",
    })
    photo_tokens = estimate_tokens(photo_exchange[1])
    # These signatures used to share the count_photos / fetch_web_images keys.
    listing = (
        "_web_images.json\n[[image_009.png]]\n✅ 可用照片: 9 张\n[✓] image_009\n"
        + _long_stdout("LS")
    )
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    messages.extend(photo_exchange)
    messages.extend(_tool_exchange(1, ["ls", "_web_images.json"], {
        "ok": True, "stdout": listing, "stderr": "",
    }))
    messages.extend(_tool_exchange(2, ["python3", "scripts/write_svg.py"], {
        "ok": True,
        "stdout": "wrote page\n[[image_010.svg]]\n" + _long_stdout("SVG"),
        "stderr": "",
    }))
    messages.extend(_tool_exchange(3, ["python3", "scripts/render.py"], {
        "ok": True, "stdout": _long_stdout("AFTER"), "stderr": "",
    }))
    diag = {}
    projected = _project(
        messages, {}, 500_000,
        _pin_policy(max_exchanges=None, pin_token_ceiling=photo_tokens),
        diag,
    )
    assert _tool_body(projected, "call-0")["payload"]["stdout"] == photo
    assert "context_compacted" not in _tool_body(projected, "call-0")
    listed = _tool_body(projected, "call-1")
    assert listed["context_compacted"] is True
    assert "x" * 1200 not in listed["payload"]["stdout"]
    written = _tool_body(projected, "call-2")
    assert written["context_compacted"] is True
    assert diag["pinned_result_count"] == 1
    assert diag["pinned_result_estimated_tokens"] == photo_tokens
    assert diag["pinned_ceiling_dropped_count"] >= 1


def test_tight_budget_rescues_count_photos_ahead_of_a_newer_file_read():
    photo = _photo_stdout("PH")
    spec = "SPEC-" + ("S" * 8000) + "-ENDSPEC"
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build the deck"}]
    messages.extend(_tool_exchange(0, ["python3", "scripts/validators/count_photos.py"], {
        "ok": True, "stdout": photo, "stderr": "",
    }))
    messages.extend(_read_exchange(1, "/workspace/ppt/assets/design_spec.md", spec))
    for index in range(2, 6):
        messages.extend(_tool_exchange(index, ["python3", "scripts/render.py"], {
            "ok": True, "stdout": f"TURN{index}", "stderr": "",
        }))
    photo_only = {}
    _project(
        messages, {}, 500_000,
        _pin_policy(pin_reads=(), pin_token_ceiling=100_000),
        photo_only,
    )
    budget = photo_only["request_estimated_tokens"]
    diag = {}
    projected = _project(
        messages, {}, budget,
        _pin_policy(pin_token_ceiling=100_000),
        diag,
    )
    assert _tool_body(projected, "call-0")["payload"]["stdout"] == photo
    assert all(message.get("tool_call_id") != "call-1" for message in projected)
    assert "ENDSPEC" not in json.dumps(projected)
    assert any(message.get("tool_call_id") == "call-2" for message in projected)
    assert any(message.get("tool_call_id") == "call-5" for message in projected)
    assert diag["preserved_exchange_count"] == 5
    assert diag["exchange_cap_dropped_count"] == 1
    assert diag["pinned_result_count"] == 1
    assert diag["pinned_ceiling_dropped_count"] == 0
    assert diag["pinned_rescued_exchange_tokens"] >= diag["pinned_result_estimated_tokens"] > 0


def test_audit_mode_does_not_apply_fast_pins():
    photo_stdout = _long_stdout("PHOTO_LIST")
    exchanges = []
    for index in range(8):
        if index == 1:
            exchanges.append((
                ["python3", "scripts/count_photos.py"],
                {"ok": True, "stdout": photo_stdout, "stderr": ""},
            ))
        else:
            exchanges.append(_render(index))
    messages = _deck_run(exchanges)
    plain_diag, pinned_diag = {}, {}
    plain = _project(messages, {}, 500_000, ThinContextPolicy(), plain_diag)
    pinned_audit = _project(messages, {}, 500_000, ThinContextPolicy(
        mode="audit",
        pin_tools=DEFAULT_FAST_PIN_TOOLS,
        pin_patterns=DEFAULT_FAST_PIN_PATTERNS,
        pin_token_ceiling=100,
    ), pinned_diag)
    assert plain == pinned_audit
    assert plain_diag["pinned_result_estimated_tokens"] == 0
    assert plain_diag["pinned_result_count"] == 0
    assert plain_diag["pinned_ceiling_dropped_count"] == 0
    assert plain_diag["pinned_rescued_exchange_tokens"] == 0
    assert pinned_diag["pinned_result_estimated_tokens"] == 0
    assert pinned_diag["pinned_rescued_exchange_tokens"] == 0
    assert "PHOTO_LIST-" in json.dumps(plain)
    assert "omitted from model context" not in json.dumps(plain)

    capped_diag = {}
    capped = _project(
        messages, {}, 500_000,
        ThinContextPolicy(mode="audit", max_exchanges=4, pin_tools=("count_photos",)),
        capped_diag,
    )
    assert capped_diag["exchange_cap_dropped_count"] == 4
    assert capped_diag["preserved_exchange_count"] == 4
    assert capped_diag["pinned_result_estimated_tokens"] == 0
    assert capped_diag["pinned_ceiling_dropped_count"] == 0
    assert capped_diag["pinned_rescued_exchange_tokens"] == 0
    assert all(message.get("tool_call_id") != "call-1" for message in capped)

    ignored = resolve_thin_context_policy(SimpleNamespace(
        orchestration_mode="audit",
        context_max_exchanges=None,
        compact_older_tools=None,
        thin_reference_shelf=None,
        fast_pin_tools=("count_photos.py",),
        fast_pin_patterns=(r"assets/sources/",),
        fast_pin_reads=("assets/design_spec.md",),
        fast_pin_token_ceiling=100,
    ))
    assert ignored.pin_tools == ()
    assert ignored.pin_patterns == ()
    assert ignored.pin_reads == ()
    assert ignored.pin_token_ceiling == 100


def test_fast_pin_settings_can_be_cleared_and_reject_bad_regex():
    cleared = resolve_thin_context_policy(SimpleNamespace(
        orchestration_mode="fast",
        context_max_exchanges=None,
        compact_older_tools=None,
        thin_reference_shelf=None,
        fast_pin_tools=(),
        fast_pin_patterns=(),
        fast_pin_reads=(),
        fast_pin_token_ceiling=128,
    ))
    assert cleared.pin_tools == ()
    assert cleared.pin_patterns == ()
    assert cleared.pin_reads == ()
    assert cleared.pin_token_ceiling == 128
    with pytest.raises(ValueError, match="SKILLGO_FAST_PIN_PATTERNS"):
        resolve_thin_context_policy(SimpleNamespace(
            orchestration_mode="fast",
            context_max_exchanges=None,
            compact_older_tools=None,
            thin_reference_shelf=None,
            fast_pin_tools=None,
            fast_pin_patterns=("[",),
            fast_pin_token_ceiling=None,
        ))
