import json
from types import SimpleNamespace

import pytest

from app.prepared_dependencies import annotate_prepared_dependencies
from app.sandbox_agent_loop import _agent_messages
from app.sandbox_tool_registry import validate_agent_action


IMAGE = 'sha256:' + 'a' * 64


def prepared(**changes):
    fields = dict(status='ready', image_id=IMAGE,
        spec={'python_requirements': ['beautifulsoup4>=4.10', 'curl-cffi', 'Pillow']}, inventory={})
    return SimpleNamespace(**{**fields, **changes})


def observed():
    return {'image_id': IMAGE, 'python_distributions': {
        'beautifulsoup4': '4.15.0', 'curl_cffi': '0.17.0', 'pillow': '13.0.0'}}


def test_prepared_versions_reach_model_without_rewriting_skill(monkeypatch):
    environment = observed()
    annotate_prepared_dependencies(environment, prepared())
    dependencies = environment['prepared_dependencies']
    assert dependencies['status'] == 'ready'
    assert dependencies['missing_requirements'] == []
    assert dependencies['packages'] == [
        {'name': 'beautifulsoup4', 'version': '4.15.0'},
        {'name': 'curl-cffi', 'version': '0.17.0'}, {'name': 'Pillow', 'version': '13.0.0'}]
    guide = '# Skill\nInstall all dependencies before starting.\npython -m pip install Pillow\n'
    job = SimpleNamespace(instruction='Create slides', memory=SimpleNamespace(data={'environment': environment}),
                          input_files=[], follow_skill_flow=True)
    contexts = [dict(root='/workspace/skill', name='Slides', version='1', skill_md=guide, runtime_requirements={})]
    messages = _agent_messages(job, contexts, [])
    assert 'Treat the corresponding Skill dependency-installation steps as completed' in messages[0]['content']
    assert 'beautifulsoup4==4.15.0' in messages[0]['content']
    assert messages[0]['content'].endswith(guide)
    assert json.loads(messages[1]['content'])['runtime_environment']['prepared_dependencies'] == dependencies


@pytest.mark.parametrize('change', [None, {'status': 'failed'}, {'image_id': 'sha256:' + 'b' * 64}, {'spec': {}}])
def test_unrelated_or_unprepared_image_cannot_claim_setup_complete(change):
    environment = observed()
    annotate_prepared_dependencies(environment, None if change is None else prepared(**change))
    assert 'prepared_dependencies' not in environment


@pytest.mark.parametrize('packages,missing', [({}, ['beautifulsoup4>=4.10', 'curl-cffi', 'Pillow']),
    ({'beautifulsoup4': '4.0', 'curl-cffi': '0.17.0', 'Pillow': '13.0.0'}, ['beautifulsoup4>=4.10'])])
def test_missing_or_wrong_versions_never_skip_install_prerequisites(packages, missing):
    environment = {'image_id': IMAGE, 'python_distributions': packages}
    annotate_prepared_dependencies(environment, prepared())
    assert environment['prepared_dependencies']['status'] == 'not_verified'
    assert environment['prepared_dependencies']['missing_requirements'] == missing


def test_unverified_imports_do_not_claim_setup_complete():
    environment = observed()
    p = prepared()
    p.spec = {**p.spec, 'python_imports': ['curl_cffi']}
    annotate_prepared_dependencies(environment, p)
    assert environment['prepared_dependencies']['status'] == 'not_verified'
    p.inventory = {'python_imports_verified': True}
    annotate_prepared_dependencies(environment, p)
    assert environment['prepared_dependencies']['status'] == 'ready'


@pytest.mark.parametrize('code', [
    "import subprocess,sys; subprocess.run([sys.executable, '-m', 'pip', 'install', 'mammoth'])",
    "import subprocess as sp; packages=['mammoth']; cmd=['python3','-m','pip','install']+packages; sp.check_call(cmd)",
    "from subprocess import check_call as run; from sys import executable; run([executable,'-I','-m','pip','install',package])",
    "import subprocess; subprocess.Popen(args=['pip3.12', 'install', 'Pillow'])",
    "import os; os.system('python3 -m pip install mammoth -i https://mirrors.example/simple')",
    "import os; os.system('echo preparing && pip install mammoth')",
    "import subprocess; subprocess.run('python3 -m pip install requests', shell=True)",
    "from pip._internal.cli.main import main as pip_main; pip_main(['install','mammoth'])",
    "import asyncio; asyncio.create_subprocess_exec('python3','-m','pip','install','mammoth')",
    "import os; package='mammoth'; os.system(f'python3 -m pip install {package}')",
    "import subprocess; subprocess.run(['sh','-c','npm install pptxgenjs'])",
])
def test_python_install_calls_are_rejected_before_execution(code):
    for action in ({'action': 'run_python', 'code': code},
                   {'action': 'command', 'argv': ['python3', '-c', code]}):
        error = validate_agent_action(action)
        assert error and 'managed by the platform' in error
        assert 'request_python_dependencies' in error


@pytest.mark.parametrize('argv', [
    ['python3', '-I', '-m', 'pip', 'install', 'mammoth'],
    ['/usr/bin/env', 'python3', '-m', 'pip', 'install', 'mammoth'],
    ['bash', '-lc', 'pip install mammoth'],
    ['uv', 'pip', 'install', 'Pillow'],
])
def test_command_install_wrappers_are_rejected(argv):
    assert 'managed by the platform' in validate_agent_action({'action': 'command', 'argv': argv})


@pytest.mark.parametrize('code', [
    "import importlib.metadata; print(importlib.metadata.version('mammoth'))",
    "import mammoth; import PIL; import pptx",
    "instructions='python -m pip install mammoth'; print(instructions)",
    "import subprocess; subprocess.run(['python3','scripts/build.py'])",
    "from subprocess import run; run(['python3','scripts/install_images.py'])",
    "import subprocess; subprocess.run(['python3','-m','pip','list'])",
    "import os; os.system('echo \"pip install is handled by the platform\"')",
    "from pptx import Presentation; Presentation().save('/workspace/output/report.pptx')",
])
def test_normal_generation_inspection_and_documentation_remain_allowed(code):
    assert validate_agent_action({'action': 'run_python', 'code': code}) is None


@pytest.mark.parametrize('attempt_install', [False, True])
def test_worker_hands_off_prepared_environment_and_blocks_reinstall(client, user_headers, monkeypatch, attempt_install):
    import asyncio
    from dataclasses import replace
    from app import config, sandbox_worker
    from app.database import SessionLocal
    from app.model_gateway import ModelResult
    from app.models import PreparedEnvironment, SkillEnvironmentBinding, SkillVersion, WorkflowJob, JobStatus
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    monkeypatch.setattr(config, 'settings', replace(config.settings, sandbox_worker_enabled=True))
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job', lambda job: True)
    monkeypatch.setattr(sandbox_worker, 'delivery_only_job', lambda job: True)
    _, uploaded = create_version(client, user_headers, slug='preloaded-handoff', package=sandbox_skill_zip())
    with SessionLocal() as db:
        spec = prepared().spec
        env = PreparedEnvironment(digest='a' * 64, status='ready', image_id=IMAGE, spec=spec, inventory={})
        db.add(env)
        db.get(SkillVersion, uploaded['id']).environment_binding = SkillEnvironmentBinding(environment=env, analysis={})
        db.commit()
    created = client.post('/api/v1/jobs', headers=user_headers,
        data={'version_id': uploaded['id'], 'instruction': 'Create answer'}).json()
    with SessionLocal() as db:
        db.get(WorkflowJob, created['id']).follow_skill_flow = True
        db.commit()

    class Sandbox(MemorySandbox):
        calls = []
        def __init__(self, *args, **kwargs):
            super().__init__()
            assert kwargs['image_id'] == IMAGE
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def put_files(self, files): self.files.update({f'/workspace/{path}': data for path, data in files.items()})
        async def command(self, argv, **kwargs):
            self.calls.append(argv)
            assert 'pip' not in str(argv)
            return await super().command(argv, **kwargs)
    async def probe(*args):
        return {**observed(), 'schema_version': 1, 'capabilities': [], 'providers': {}}
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        count = 0
        def for_model(self, name): return self
        async def agent_step(self, *, messages):
            self.count += 1
            if self.count == 1:
                assert 'Dependencies already prepared for this task:' in messages[0]['content']
                assert json.loads(messages[1]['content'])['runtime_environment']['prepared_dependencies']['status'] == 'ready'
                if attempt_install:
                    return ModelResult({'action': 'run_python', 'code': "import subprocess; subprocess.run(['pip','install','mammoth'])"}, 'test', {})
            if self.count == 1 or attempt_install and self.count == 2:
                if attempt_install:
                    assert 'managed by the platform' in str(messages)
                return ModelResult({'action': 'write_file', 'path': '/workspace/output/answer.txt', 'content': '42'}, 'test', {})
            return ModelResult({'action': 'finish', 'summary': 'Done', 'artifacts': ['/workspace/output/answer.txt']}, 'test', {})
    gateway = Gateway()
    monkeypatch.setattr(sandbox_worker, 'DockerSandbox', Sandbox)
    monkeypatch.setattr(sandbox_worker, 'preflight_environment', probe)
    monkeypatch.setattr(sandbox_worker, 'get_model_gateway', lambda: gateway)
    lease = sandbox_worker._claim_job('preloaded-test')
    assert lease and lease.job_id == created['id']
    docker = SimpleNamespace(images=SimpleNamespace(get=lambda image: SimpleNamespace(id=image)))
    asyncio.run(sandbox_worker.execute_sandbox_job(lease.job_id, docker, lease=lease))
    with SessionLocal() as db:
        job = db.get(WorkflowJob, lease.job_id)
        assert job.status == JobStatus.SUCCEEDED, job.error_message
        assert any(e.title == '依赖已预加载' for e in job.events)
        assert job.memory.data['environment']['prepared_dependencies']['status'] == 'ready'
