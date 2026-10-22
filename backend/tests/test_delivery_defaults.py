"""Deployment defaults are tested independently of full-verification fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


@pytest.mark.parametrize('configured,expected', [(None, True), ('true', True), ('false', False)])
def test_default_receipt_and_explicit_full_verification(configured, expected):
    env = dict(os.environ)
    env.pop('SKILLGO_ARTIFACT_DELIVERY_ONLY', None)
    env.pop('SKILLGO_DELIVERY_ONLY_SKILL_IDS', None)
    if configured is not None:
        env['SKILLGO_ARTIFACT_DELIVERY_ONLY'] = configured
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-c', '''
import json
from types import SimpleNamespace
from app.artifact_collection import delivery_only_job
from app.config import settings
from app.model_gateway import SANDBOX_AGENT_TOOLS
print(json.dumps({'policy': settings.artifact_delivery_only,
 'arbitrary_skill': delivery_only_job(SimpleNamespace(skill_id='new-skill',selected_skills=[])),
 'generic_delivery_tools': all('delivery' in t['function']['parameters']['properties']
  for t in SANDBOX_AGENT_TOOLS if t['function']['name'] in ('command','run_commands','run_python'))}))
'''], cwd=backend, env=env, capture_output=True, text=True, check=True)
    actual = json.loads(result.stdout)
    assert actual == {'policy': expected, 'arbitrary_skill': expected, 'generic_delivery_tools': True}


def test_sample_envs_and_compose_do_not_disable_default_delivery():
    root = Path(__file__).resolve().parents[2]
    for name in ['.env.example', 'backend/.env.example', 'deploy/ecs.env.example']:
        entries = [line for line in (root/name).read_text(encoding='utf-8').splitlines()
                   if line.startswith('SKILLGO_ARTIFACT_DELIVERY_ONLY=')]
        assert entries == ['SKILLGO_ARTIFACT_DELIVERY_ONLY=true']
    compose = yaml.safe_load((root/'compose.yaml').read_text(encoding='utf-8'))
    for service in ['api', 'worker']:
        assert compose['services'][service]['environment']['SKILLGO_ARTIFACT_DELIVERY_ONLY'] == '${SKILLGO_ARTIFACT_DELIVERY_ONLY:-true}'
