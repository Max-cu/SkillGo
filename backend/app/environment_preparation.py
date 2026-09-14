"""Platform-owned environment plans; no package names or code from Skill are executed."""
from __future__ import annotations
import hashlib
import json
import re
import ast
import io
import zipfile
from sqlalchemy.exc import IntegrityError
from .config import settings
from .environment_capabilities import CAPABILITIES, capability_requirements
from .models import PreparedEnvironment, SkillEnvironmentBinding

POLICY_VERSION = 1
EXTENSIONS = {
    'image.qr': {
        'wheels': [{'name': 'qrcode', 'version': '8.2', 'filename': 'qrcode-8.2-py3-none-any.whl',
                    'url': 'https://files.pythonhosted.org/packages/dd/b8/d2d6d731733f51684bbf76bf34dab3b70a9148e8f2cef2bb544fccec681a/qrcode-8.2-py3-none-any.whl',
                    'sha256': '16e64e0716c14960108e85d853062c9e8bba5ca8252c0b4d0231b9df4060ff4f'}],
        'probe': "import io,qrcode; b=io.BytesIO(); qrcode.make('SkillGo synthetic probe').save(b,format='PNG'); assert b.getvalue().startswith(b'\\x89PNG')",
    },
}
IMPORT_CAPABILITIES = {'fitz': 'pdf.read', 'pymupdf': 'pdf.read', 'docx': 'office.docx',
    'openpyxl': 'office.xlsx', 'pptx': 'office.pptx', 'PIL': 'image.basic', 'pandas': 'data.tabular', 'qrcode': 'image.qr'}


def analyze_capabilities(skill_md, manifest):
    analysis = capability_requirements(skill_md, manifest)
    # Imports in examples are hints, never executable code or arbitrary pip requirements.
    # Only import statements, not prose such as "do not import qrcode".
    imports = set(re.findall(r'^\s*(?:from|import)\s+([A-Za-z_][A-Za-z_0-9]*)', skill_md, re.M))
    inferred = set(analysis['inferred_capabilities'])
    evidence = []
    for number, line in enumerate(skill_md.splitlines(), 1):
        for clause in re.split(r'[。；;.!?\n]', line):
            if re.search(r'禁止|严禁|不要|无需|不得|不需要|不生成|不创建|\b(?:not|never|without|avoid)\b', clause, re.I):
                continue
            if re.search(r'(?:生成|创建|制作|绘制|输出)\s*(?:一个|新的|彩色)?\s*二维码|\b(?:generate|create|make|render)\s+(?:(?:a|an|the|new)\s+)?qr[ -]?codes?\b', clause, re.I):
                inferred.add('image.qr')
                evidence.append({'capability': 'image.qr', 'source': 'generation_intent', 'line': number})
    inferred.update(IMPORT_CAPABILITIES[n] for n in imports if n in IMPORT_CAPABILITIES)
    evidence.extend({'capability': IMPORT_CAPABILITIES[n], 'source': 'import_statement', 'module': n}
                    for n in sorted(imports) if n in IMPORT_CAPABILITIES)
    evidence.extend({'capability': c, 'source': 'declaration'} for c in analysis['declared_capabilities'])
    explained = {item['capability'] for item in evidence}
    evidence.extend({'capability': c, 'source': 'document_hint'} for c in sorted(inferred - explained))
    analysis['evidence'] = evidence
    analysis['inferred_capabilities'] = sorted(inferred - set(analysis['declared_capabilities']))
    analysis['method'] = 'declarations_and_catalog_hints_v2'
    analysis['limitations'] = 'Hints are not a complete semantic dependency analysis; only catalog capabilities can be prepared.'
    return analysis


def package_import_hints(package):
    """Read bounded Python source without importing or executing Skill code."""
    modules = set()
    remaining = 1024 * 1024
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        for info in archive.infolist():
            if not info.filename.endswith('.py') or info.file_size > min(256 * 1024, remaining):
                continue
            remaining -= info.file_size
            try:
                tree = ast.parse(archive.read(info).decode('utf-8-sig'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules.add(node.module.split('.')[0])
    return sorted({IMPORT_CAPABILITIES[name] for name in modules if name in IMPORT_CAPABILITIES})


def environment_spec(capabilities, base_image=None):
    base = base_image or settings.environment_base_image
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', base):
        raise ValueError('Platform environment base must be pinned to an immutable local image ID')
    caps = sorted(set(capabilities))
    unknown = set(caps) - set(CAPABILITIES) - set(EXTENSIONS)
    if unknown:
        raise ValueError('Capabilities are not in the platform catalog: ' + ', '.join(sorted(unknown)))
    wheels = {}
    for cap in caps:
        for wheel in EXTENSIONS.get(cap, {}).get('wheels', []):
            old = wheels.get(wheel['name'])
            if old and old != wheel:
                raise ValueError('Conflicting platform locks: ' + wheel['name'])
            wheels[wheel['name']] = wheel
    from pathlib import Path
    probe_digest = hashlib.sha256(Path(__file__).with_name('environment_probe.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    extension_digest = hashlib.sha256(json.dumps(EXTENSIONS, sort_keys=True).encode()).hexdigest()
    return {'policy': POLICY_VERSION, 'probe_digest': probe_digest, 'extension_digest': extension_digest, 'base_image': base, 'platform': 'linux/amd64',
            'capabilities': caps, 'wheels': [wheels[k] for k in sorted(wheels)]}


def enqueue_environment(db, spec, error=None):
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    env = db.get(PreparedEnvironment, digest)
    if env is None:
        try:
            with db.begin_nested():
                env = PreparedEnvironment(digest=digest, spec=spec, status='failed' if error else 'queued',
                                          error_message=error, inventory={})
                db.add(env)
                db.flush()
        except IntegrityError:
            env = db.get(PreparedEnvironment, digest)
    return env


def bind_version_environment(db, version, *, package=None):
    if version.environment_binding is not None:
        return version.environment_binding.environment
    if not settings.environment_preparation_enabled or version.execution_mode != 'sandbox_required':
        return None
    analysis = analyze_capabilities(version.skill_md, version.manifest or {})
    analysis['source_capabilities'] = package_import_hints(package) if package is not None else []
    caps = analysis['declared_capabilities'] + analysis['inferred_capabilities'] + analysis['source_capabilities']
    try:
        spec = environment_spec(caps)
        error = None
    except ValueError as exc:
        spec = {'policy': POLICY_VERSION, 'base_image': settings.environment_base_image, 'capabilities': sorted(set(caps))}
        error = str(exc)
    env = enqueue_environment(db, spec, error)
    version.environment_binding = SkillEnvironmentBinding(environment=env, analysis=analysis)
    db.flush()
    return env


def require_ready_version(version):
    from fastapi import HTTPException
    binding = version.environment_binding
    if binding and binding.environment.status != 'ready':
        raise HTTPException(status_code=409, detail='运行环境尚未就绪：' + (binding.environment.error_message or binding.environment.status))


def prepare_job_environment(db, versions):
    envs = [env for v in versions if (env := bind_version_environment(db, v)) is not None]
    if not envs:
        return None
    if any(e.status in {'failed', 'revoked'} for e in envs):
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='Skill 运行环境准备失败或已停用，请先处理版本环境状态')
    # Existing bindings remain authoritative after catalog/config changes.
    if len({e.digest for e in envs}) == 1:
        return envs[0]
    common = [{k: v for k, v in e.spec.items() if k not in {'capabilities', 'wheels'}} for e in envs]
    if any(item != common[0] for item in common[1:]):
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='所选 Skill 的基础环境或构建策略不兼容，无法安全组合')
    wheels = {}
    for env in envs:
        for wheel in env.spec['wheels']:
            if wheel['name'] in wheels and wheels[wheel['name']] != wheel:
                from fastapi import HTTPException
                raise HTTPException(status_code=409, detail='所选 Skill 的依赖锁定版本冲突')
            wheels[wheel['name']] = wheel
    spec = {**common[0], 'capabilities': sorted({cap for e in envs for cap in e.spec['capabilities']}),
            'wheels': [wheels[name] for name in sorted(wheels)]}
    return enqueue_environment(db, spec)
