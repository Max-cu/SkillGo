"""Generic Python dependency preparation: parsing, resolver guard, builder orchestration."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import io
import json
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import environment_builder as builder
from app import environment_preparation as prep
from app.python_dependencies import (
    analyze_python_dependencies,
    normalize_imports,
    normalize_requirements,
    requirements_satisfied,
)
from app.sandbox_tool_registry import validate_agent_action

BASE = 'sha256:' + 'a' * 64
NEW_IMAGE = 'sha256:' + 'b' * 64


# ---------- normalize_requirements ----------

def test_requirements_canonicalized_deduped_and_sorted():
    out = normalize_requirements(['Scipy>=1.10', 'scipy >=1.10', 'Pillow[PDF]'])
    assert out == ['pillow[PDF]', 'scipy>=1.10']


@pytest.mark.parametrize('bad', [
    'requests @ https://example.com/x.whl',   # 禁止 URL
    'requests; python_version<"3.9"',        # 禁止环境标记
    'numpy\\x',                                # 禁止反斜杠
    'numpy\nrm -rf /',                         # 禁止换行
    'x' * 201,                                 # 超长
    '',
])
def test_requirements_reject_unsafe_shapes(bad):
    with pytest.raises(ValueError):
        normalize_requirements([bad])


def test_requirements_reject_non_string_and_overflow():
    with pytest.raises(ValueError):
        normalize_requirements([1])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        normalize_requirements(['pkg'] * 65)


# ---------- normalize_imports ----------

@pytest.mark.parametrize('good', [['scipy'], ['a.b.c'], ['_x', 'Y2']])
def test_imports_accept_module_names(good):
    assert normalize_imports(good) == sorted(set(good))


@pytest.mark.parametrize('bad', [['0scipy'], ['a-b'], ['.rel'], ['a/../b'], ['x.y' * 60]])
def test_imports_reject_non_identifiers(bad):
    with pytest.raises(ValueError):
        normalize_imports(bad)


# ---------- analyze_python_dependencies ----------

def test_analysis_declarations_import_inference_and_stdlib_exclusion():
    skill_md = (
        '---\n'
        'name: demo\n'
        'python_dependencies: [defusedxml]\n'
        '---\n'
        'import os, json\n'            # 标准库，不产生依赖
        'from fitz import Document\n'  # import 名 -> 发行名映射
    )
    result = analyze_python_dependencies(skill_md, {})
    reqs = result['python_requirements']
    assert 'defusedxml' in reqs
    assert 'pymupdf' in reqs
    assert not any(r.lower().startswith(('os', 'json')) for r in reqs)
    assert set(result['python_imports']) >= {'fitz'}
    sources = {e['source'] for e in result['python_evidence']}
    assert {'declaration', 'import_inference'} <= sources


def test_analysis_reads_requirements_txt_alongside_skill_md_and_ignores_nested():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('SKILL.md', 'import pandas\n')
        z.writestr('requirements.txt', 'scipy==1.11.0\n# comment\n')
        z.writestr('nested/requirements.txt', 'evil==9.9.9\n')
    result = analyze_python_dependencies('import pandas\n', {}, out.getvalue())
    assert 'scipy==1.11.0' in result['python_requirements']
    assert 'pandas' in result['python_requirements']
    assert not any('evil' in r for r in result['python_requirements'])


def test_analysis_pip_install_line_is_declarative_only():
    result = analyze_python_dependencies('$ pip install pandas==2.2.0 --no-cache-dir', {})
    assert 'pandas==2.2.0' in result['python_requirements']
    assert any(e['source'] == 'install_example' for e in result['python_evidence'])


def test_analysis_rejects_oversized_requirements_txt():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('SKILL.md', 'x')
        z.writestr('requirements.txt', 'pkg\n' * 11000)
    with pytest.raises(ValueError):
        analyze_python_dependencies('x', {}, out.getvalue())


def test_analysis_rejects_url_in_requirements_txt():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('SKILL.md', 'x')
        z.writestr('requirements.txt', 'pkg @ https://evil.example/x.whl\n')
    with pytest.raises(ValueError):
        analyze_python_dependencies('x', {}, out.getvalue())


# ---------- requirements_satisfied ----------

def test_requirements_satisfied_uses_observed_distributions():
    inventory = {'python_distributions': {'scipy': '1.11.0'}}
    assert requirements_satisfied(['scipy>=1.10'], inventory)
    assert not requirements_satisfied(['scipy>=1.12'], inventory)
    assert not requirements_satisfied(['numpy'], inventory)


# ---------- spec: generic requirements enter the cache key ----------

def test_environment_spec_carries_generic_requirements_and_round_trips():
    spec = prep.environment_spec([], BASE, python_requirements=['scipy'], python_imports=['scipy'])
    assert spec['python_policy'] == 1 and spec['python_requirements'] == ['scipy']
    # Builder 会用同参数重算 spec 防篡改，必须完全相等
    assert spec == prep.environment_spec(spec['capabilities'], spec['base_image'],
                                         python_requirements=spec['python_requirements'],
                                         python_imports=spec['python_imports'])
    other = prep.environment_spec([], BASE, python_requirements=['numpy'])
    import hashlib
    digest = lambda s: hashlib.sha256(json.dumps(s, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert digest(spec) != digest(other)


# ---------- agent tool validation ----------

def test_tool_validation_for_request_python_dependencies():
    assert validate_agent_action({'action': 'request_python_dependencies',
                                  'requirements': ['scipy'], 'reason': 'numerical'}) is None
    assert validate_agent_action({'action': 'request_python_dependencies',
                                  'requirements': [], 'reason': 'x'})
    assert validate_agent_action({'action': 'request_python_dependencies',
                                  'requirements': ['bad\nx'], 'reason': 'x'})
    assert validate_agent_action({'action': 'request_python_dependencies',
                                  'requirements': ['scipy'], 'reason': ''})
    assert validate_agent_action({'action': 'request_python_dependencies',
                                  'requirements': ['scipy'], 'reason': 'x' * 501})
    assert validate_agent_action({'action': 'request_python_dependencies',
                                  'requirements': ['scipy'], 'imports': ['0bad'], 'reason': 'x'})


# ---------- resolver script: PyPI host guard and lock extraction ----------

RESOLVER_SRC = Path(__file__).resolve().parents[1] / 'app' / 'python_dependency_resolver.py'


class _FakePath:
    files: dict[str, str] = {}

    def __init__(self, name):
        self.name = name

    def write_text(self, text):
        self.files[self.name] = text

    def read_text(self):
        return self.files[self.name]


def _run_resolver(monkeypatch, capsys, report):
    """Execute the injected resolver script with pip/network stubbed; returns parsed stdout."""
    _FakePath.files.clear()

    def fake_main(args):
        _FakePath.files['/tmp/report.json'] = json.dumps(report)
        return 0

    fake_session = ModuleType('pip._internal.network.session')

    class PipSession:  # mimics the class the script patches
        def send(self, request, **kwargs):
            raise AssertionError('network must be intercepted')

    fake_session.PipSession = PipSession
    fake_cli = ModuleType('pip._internal.cli.main')
    fake_cli.main = fake_main
    fake_utils = ModuleType('pip._vendor.packaging.utils')
    from packaging.utils import canonicalize_name
    fake_utils.canonicalize_name = canonicalize_name
    fake_pathlib = ModuleType('pathlib')
    fake_pathlib.Path = _FakePath
    for name, module in {
        'pip._internal.network.session': fake_session,
        'pip._internal.cli.main': fake_cli,
        'pip._vendor.packaging.utils': fake_utils,
        'pathlib': fake_pathlib,
    }.items():
        monkeypatch.setitem(__import__('sys').modules, name, module)

    ns = {'SPEC': {'requirements': ['scipy']}}
    exec(compile(RESOLVER_SRC.read_text(encoding='utf-8-sig'), str(RESOLVER_SRC), 'exec'), ns)
    return json.loads(capsys.readouterr().out), ns


def _wheel_item(name, version, sha='', filename=None):
    filename = filename or f'{name}-{version}-py3-none-any.whl'
    return {
        'metadata': {'name': name, 'version': version},
        'download_info': {
            'url': f'https://files.pythonhosted.org/packages/ab/cd/{filename}',
            'archive_info': {'hashes': {'sha256': sha or ('1' * 64)}},
        },
    }


def test_resolver_emits_verified_locks(monkeypatch, capsys):
    report = {'install': [_wheel_item('scipy', '1.11.0')]}
    locks, ns = _run_resolver(monkeypatch, capsys, report)
    assert locks['wheels'] == [{
        'name': 'scipy', 'version': '1.11.0',
        'filename': 'scipy-1.11.0-py3-none-any.whl',
        'url': f'https://files.pythonhosted.org/packages/ab/cd/scipy-1.11.0-py3-none-any.whl',
        'sha256': '1' * 64,
    }]
    # 主机守卫对非法请求抛错
    send = ns['guarded_send']
    ns['original_send'] = lambda self, request, **kw: 'sent'
    for url in [
        'http://files.pythonhosted.org/x.whl',                       # 非 https
        'https://evil.example.com/x.whl',                            # 非 PyPI 主机
        'https://files.pythonhosted.org:8443/x.whl',                 # 非标准端口
        'https://user:pass@files.pythonhosted.org/x.whl',            # 带凭据
    ]:
        with pytest.raises(ValueError):
            send(ns['PipSession'](), SimpleNamespace(url=url))
    assert send(ns['PipSession'](), SimpleNamespace(url='https://pypi.org/simple/scipy/')) == 'sent'


@pytest.mark.parametrize('bad_report', [
    {'install': [{'metadata': {'name': 'x', 'version': '1'}, 'download_info': {
        'url': 'https://evil.example.com/x-1-py3-none-any.whl',
        'archive_info': {'hashes': {'sha256': '1' * 64}}}}]},          # 非固定主机
    {'install': [{'metadata': {'name': 'x', 'version': '1'}, 'download_info': {
        'url': 'https://files.pythonhosted.org/x-1.tar.gz',
        'archive_info': {'hashes': {'sha256': '1' * 64}}}}]},          # sdist 而非 wheel
    {'install': [{'metadata': {'name': 'x', 'version': '1'}, 'download_info': {
        'url': 'https://files.pythonhosted.org/x-1-py3-none-any.whl',
        'archive_info': {'hashes': {'sha256': 'abc'}}}}]},             # 哈希长度不足
])
def test_resolver_rejects_bad_locks(monkeypatch, capsys, bad_report):
    with pytest.raises(AssertionError):
        _run_resolver(monkeypatch, capsys, bad_report)


# ---------- builder generic orchestration (fake docker) ----------

def _extension_tar():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as t:
        data = b'# scipy stub'
        info = tarfile.TarInfo('extension/scipy/__init__.py')
        info.size = len(data)
        info.mode = 0o644
        t.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def test_builder_generic_path_uses_guarded_resolver_then_offline_install_and_probes_imports(monkeypatch):
    monkeypatch.setattr(builder, 'settings', replace(builder.settings, environment_base_image=BASE,
                                                     environment_build_seconds=900))
    created = []

    sha = hashlib.sha256(b'wheel-bytes').hexdigest()
    wheel = {'name': 'scipy', 'version': '1.11.0',
             'filename': 'scipy-1.11.0-py3-none-any.whl',
             'url': 'https://files.pythonhosted.org/packages/ab/cd/scipy-1.11.0-py3-none-any.whl',
             'sha256': sha}

    class Container:
        def __init__(self, argv, network, writable, user, mem_limit):
            self.argv = argv
            self.network = network
            self.writable = writable
            self.user = user
            self.mem_limit = mem_limit
            self.status = 'created'
            self.attrs = {'State': {'ExitCode': 0}}
            created.append(self)

        def start(self):
            self.status = 'exited'

        def reload(self):
            self.status = 'exited'

        def logs(self, stdout=True, stderr=True):
            code = self.argv[-1]
            if 'PipSession' in code:  # 联网解析器
                assert self.network == 'bridge' and self.mem_limit == '1536m'
                return json.dumps({'wheels': [wheel], 'base_distributions': {}}).encode()
            if 'NoRedirect' in code:   # 固定主机下载器
                assert self.network == 'bridge'
                payload = {wheel['filename']: base64.b64encode(b'wheel-bytes').decode()}
                return json.dumps(payload).encode()
            if 'pip' in code and 'install' in code:  # 断网离线安装器
                assert self.network == 'none' and self.writable and self.user == '0:0'
                return _extension_tar().encode()
            # 功能探针：断网、只读、非 root
            assert self.network == 'none' and not self.writable and self.user == '10001:10001'
            assert 'importlib.import_module' in code and "'scipy'" in code
            return json.dumps({'schema_version': 1, 'providers': {}}).encode()

        def put_archive(self, path, data):
            self.archive = (path, data)

        def remove(self, force=True):
            pass

    def create(image, command, **kw):
        return Container(command, kw['network_mode'], kw['read_only'] is False, kw['user'], kw['mem_limit'])

    client = SimpleNamespace(
        images=SimpleNamespace(
            get=lambda image_id: SimpleNamespace(id=BASE, attrs={'Architecture': 'amd64', 'Os': 'linux'}),
            build=lambda **kw: (SimpleNamespace(id=NEW_IMAGE), iter([])),
        ),
        containers=SimpleNamespace(create=create),
    )

    spec = prep.environment_spec([], BASE, python_requirements=['scipy'], python_imports=['scipy'])
    image_id, inventory = builder.build_environment(client, spec, attempt='test-generic')

    assert image_id == NEW_IMAGE
    assert inventory['python_imports_verified'] is True
    assert inventory['python_lock'] == [wheel]
    assert inventory['python_imports'] == ['scipy']
    assert inventory['image_id'] == NEW_IMAGE
    # 四个阶段容器都被调度过
    assert len(created) == 4


# ---------- runtime request validation ----------

def test_runtime_request_rejects_invalid_requirement_without_building(monkeypatch):
    from app import runtime_capability as rc
    monkeypatch.setattr(rc, 'settings', replace(rc.settings, environment_preparation_enabled=True))
    job = SimpleNamespace(memory=SimpleNamespace(data={}))

    async def scenario():
        return await rc.request_capability(
            SimpleNamespace(), job, SimpleNamespace(), None, lambda: False,
            requirements=['numpy\nrm'], imports=[])

    payload = asyncio.run(scenario())
    assert payload['ok'] is False and payload['error_code'] == 'ENVIRONMENT_DEPENDENCY_INVALID'
