import asyncio
import json
from types import SimpleNamespace

import pytest

from app.environment_capabilities import capability_requirements, resolve_capabilities, preflight_environment
from app.runtime_profile import detect_runtime_profile
from app.sandbox_runtime import SandboxRuntimeError


def test_declarations_are_required_but_prose_is_only_advisory():
    md = "---\nname: pdf-test\ndescription: test\ncapabilities: [pdf.render, fonts.cjk]\n---\nDo not process PDF examples."
    result = detect_runtime_profile(skill_md=md, manifest={})
    assert result['execution_mode'] == 'sandbox_required'
    assert result['requirements']['declared_capabilities'] == ['fonts.cjk', 'pdf.render']
    assert result['requirements']['inferred_capabilities'] == ['pdf.read']
    assert not result['requirements']['network']


@pytest.mark.parametrize('value', ['pdf.read', [12], [''], ['x'*81], ['pdf.read']*33])
def test_reject_malformed_declarations(value):
    with pytest.raises(ValueError):
        capability_requirements('text', {'spec': {'capabilities': value}})


def test_provider_alternatives_do_not_grant_missing_capabilities():
    assert 'pdf.annotate' not in resolve_capabilities({'pypdf': {'available': True}})
    available = resolve_capabilities({'pypdf': {'available': True}, 'reportlab': {'available': True}})
    assert 'pdf.annotate' in available
    assert 'pdf.render' not in available
    assert 'pdf.read' not in resolve_capabilities({'pymupdf': {'available': 'true'}})


class ProbeSandbox:
    network_enabled = False
    def __init__(self, stdout=None, exit_code=0):
        self.stdout = stdout if stdout is not None else json.dumps({'schema_version': 1, 'providers': {'pypdf': {'available': True}}})
        self.exit_code = exit_code
        self.calls = []
    async def command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert argv[:3] == ['python3', '-I', '-c']
        assert kwargs['cwd'] == '/workspace'
        return SimpleNamespace(stdout=self.stdout, exit_code=self.exit_code)


def test_missing_inferred_capability_does_not_block_or_grant_network():
    sandbox = ProbeSandbox()
    result = asyncio.run(preflight_environment(sandbox, [{'runtime_requirements': {'inferred_capabilities': ['pdf.render']}}]))
    assert result['capabilities'] == ['pdf.read']
    assert result['network_enabled'] is False
    assert result['environment_upgrade_supported'] is False
    assert len(sandbox.calls) == 1


@pytest.mark.parametrize('capability', ['pdf.render', 'https://attacker.test/install.sh', 'image.unknown'])
def test_unavailable_declared_capability_blocks_without_installing(capability):
    sandbox = ProbeSandbox()
    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(preflight_environment(sandbox, [{'runtime_requirements': {'declared_capabilities': [capability]}}]))
    assert caught.value.code == 'SANDBOX_DEPENDENCY_MISSING'
    assert len(sandbox.calls) == 1


@pytest.mark.parametrize('stdout,exit_code', [('{}', 0), ('bad-json', 0), ('[]', 0), ('{"schema_version":1,"providers":{}}', 1)])
def test_probe_failure_is_not_reported_as_available(stdout, exit_code):
    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(preflight_environment(ProbeSandbox(stdout, exit_code), []))
    assert caught.value.code == 'SANDBOX_ENVIRONMENT_PROBE_FAILED'
