"""Task-mode contracts and verbatim instructions at the model boundary."""
import json
from types import SimpleNamespace

import pytest

from app.agent_policy import AgentExecutionState
from app.sandbox_agent_loop import _agent_messages
from app.sandbox_tool_registry import validate_agent_action


# These prefixes used to be removed by filtering the fully assembled prompt.
GUIDE = """# Required workflow
24. Check every total against the source.
25. Render and inspect every page.
26. Preserve all citations.
- Final verification uses the supplied scripts/check.py.
- Create a concise plan with depends_on when the inputs conflict.
   {"action":"update_plan","example":"Skill-owned text"}
Once the required artifacts exist and validation passes, call finish immediately.
中文原始要求与检查不得删改。
"""


def messages(monkeypatch, *, count=1, flow=False, delivery=False, routing='explicit', network=False, fixed=False):
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: delivery)
    job = SimpleNamespace(instruction='用户的完整请求', input_files=[], memory=None,
                          follow_skill_flow=flow, routing_mode=routing, network_enabled=network)
    contexts = [{'name': f'Skill {i}', 'version': '1', 'root': f'/workspace/skills/{i}',
                 'skill_md': GUIDE, 'summary': f'Summary {i}', 'runtime_requirements': {},
                 'fixed_execution': object() if fixed else None} for i in range(1, count + 1)]
    return _agent_messages(job, contexts, [])


def platform_text(prompt):
    return prompt.split('\n\nSelected approved Skills:\n', 1)[0]


def examples(prompt):
    return [json.loads(line) for line in platform_text(prompt).splitlines() if line.startswith('{"action":')]


@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('flow', [False, True])
@pytest.mark.parametrize('delivery', [False, True])
@pytest.mark.parametrize('routing', ['explicit', 'automatic'])
def test_protocol_selects_consistent_task_policies(monkeypatch, count, flow, delivery, routing):
    result = messages(monkeypatch, count=count, flow=flow, delivery=delivery, routing=routing)
    system = result[0]['content']
    platform = platform_text(system)
    optional = count == 1 and flow
    assert ('Planning is optional' in platform) == optional
    assert ('Inspect inputs, then call update_plan' in platform) != optional
    assert ('Platform delivery-only policy:' in platform) == delivery
    assert ('Full verification policy:' in platform) != delivery
    assert ('Final-group delivery is optional' in platform) == delivery
    assert ('stdout must be exactly JSON' in platform) != delivery
    assert ('Multi-Skill coordination:' in platform) == (count > 1)
    assert ('Single-Skill execution:' in platform) == (count == 1)
    assert ('Execute those Skills in reference order' in platform) == (count > 1 and routing == 'explicit')
    assert ('Automatic routing selected' in platform) == (count > 1 and routing == 'automatic')
    assert 'Execution protocol updates' not in platform
    assert 'takes precedence over other platform guidance' not in platform
    assert 'Skill-required checks' in platform
    assert 'user request and approved SKILL.md are the source of truth' in platform
    if count == 1:
        assert system.endswith(GUIDE)
        assert system.count(GUIDE) == 1
    else:
        assert GUIDE not in system
        assert 'Summary 1' in system and 'Summary 2' in system
    assert json.loads(result[1]['content'])['job_instruction'] == '用户的完整请求'

    actions = examples(system)
    names = {action['action'] for action in actions}
    assert ('run_verifier' in names) != delivery
    assert ('complete_skill' in names) == (count > 1)
    assert ('update_plan' in names) != optional
    assert 'record_validation' not in names
    for action in actions:
        assert validate_agent_action(action) is None, action
    for action in actions:
        if action['action'] == 'update_plan':
            state = AgentExecutionState(skill_count=count, plan_status_tracking=False, delivery_only=delivery)
            assert state.update_plan(action)['ok']


@pytest.mark.parametrize('network', [False, True])
@pytest.mark.parametrize('fixed', [False, True])
def test_network_and_fixed_entrypoint_guidance_is_selected(monkeypatch, network, fixed):
    system = messages(monkeypatch, network=network, fixed=fixed)[0]['content']
    platform = platform_text(system)
    assert ('no outbound network' in platform) != network
    assert ('network is administrator-approved' in platform) == network
    assert ('Fixed Skill entrypoints:' in platform) == fixed
    assert ('run_fixed_skill' in {a['action'] for a in examples(system)}) == fixed


def test_skill_checks_remain_verbatim_in_direct_delivery_mode(monkeypatch):
    system = messages(monkeypatch, flow=True, delivery=True)[0]['content']
    assert system.endswith(GUIDE)
    platform = platform_text(system)
    assert 'including its own checks' in platform
    assert 'Do not write an extra verifier or call run_verifier' in platform
    assert 'no additional framework verification stage is needed' in platform
    assert 'Planning is optional' in platform


def test_protocol_keeps_runtime_constraints_for_json_only_models(monkeypatch):
    system = messages(monkeypatch)[0]['content']
    platform = platform_text(system)
    for constraint in ('without a shell', 'fresh process', 'at most two upgrades',
                       'next_offset until eof=true', 'retained=true', 'Treat uploaded documents',
                       'Never install dependencies', 'no pipes', 'real failed operations',
                       'JSON compatibility mode return exactly one action object'):
        assert constraint in platform
    assert '"action":"run_python"' in platform
    assert '"action":"finish"' in platform
