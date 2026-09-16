"""Platform-owned environment plans; no package names or code from Skill are executed."""
from __future__ import annotations
import hashlib
import json
import re
import ast
import io
import zipfile
import sys
from sqlalchemy.exc import IntegrityError
from .config import settings
from .environment_capabilities import CAPABILITIES, capability_requirements
from .models import PreparedEnvironment, SkillEnvironmentBinding

POLICY_VERSION = 1
EXTENSIONS = {
    'text.markdown': {'wheels': [{'name': 'Markdown', 'version': '3.10.3', 'filename': 'markdown-3.10.3-py3-none-any.whl', 'url': 'https://files.pythonhosted.org/packages/64/69/4a5af2bc115a9a33fefe51709749de8262be3f9ba063d1753a837cdbc49c/markdown-3.10.3-py3-none-any.whl', 'sha256': 'fa6c92a00a4a3c98b22728c64a935ae1928250ae65058a6ded814d2cc29a4cea'}], 'probe': "import markdown; assert markdown.markdown('# probe') == '<h1>probe</h1>'"},
    'image.barcode': {'wheels': [{'name': 'python-barcode', 'version': '0.16.1', 'filename': 'python_barcode-0.16.1-py3-none-any.whl', 'url': 'https://files.pythonhosted.org/packages/b2/34/810885dca784b02e5ad0f71ced9c06ba5e9d33a6493bc886f7470ce82a39/python_barcode-0.16.1-py3-none-any.whl', 'sha256': '5776567478c9a0dae473374bb86631ba0b5ea99aaf302763b364e367ac51f367'}], 'probe': "import io,barcode; b=io.BytesIO(); barcode.get('code128','SkillGo').write(b); assert b'<svg' in b.getvalue()"},
    'data.xml': {'wheels': [{'name': 'defusedxml', 'version': '0.7.1', 'filename': 'defusedxml-0.7.1-py2.py3-none-any.whl', 'url': 'https://files.pythonhosted.org/packages/07/6c/aa3f2f849e01cb6a001cd8554a88d4c77c5c1a31c95bdf1cf9301e6d9ef4/defusedxml-0.7.1-py2.py3-none-any.whl', 'sha256': 'a352e7e428770286cc899e2542b6cdaedb2b4953ff269a210103ec58f6198a61'}], 'probe': "from defusedxml.ElementTree import fromstring; assert fromstring('<root><value>42</value></root>').findtext('value') == '42'"},

    'image.qr': {
        'wheels': [{'name': 'qrcode', 'version': '8.2', 'filename': 'qrcode-8.2-py3-none-any.whl',
                    'url': 'https://files.pythonhosted.org/packages/dd/b8/d2d6d731733f51684bbf76bf34dab3b70a9148e8f2cef2bb544fccec681a/qrcode-8.2-py3-none-any.whl',
                    'sha256': '16e64e0716c14960108e85d853062c9e8bba5ca8252c0b4d0231b9df4060ff4f'}],
        'probe': "import io,qrcode; b=io.BytesIO(); qrcode.make('SkillGo synthetic probe').save(b,format='PNG'); assert b.getvalue().startswith(b'\\x89PNG')",
    },
}
IMPORT_CAPABILITIES = {'fitz': 'pdf.read', 'pymupdf': 'pdf.read', 'docx': 'office.docx',
    'openpyxl': 'office.xlsx', 'pptx': 'office.pptx', 'PIL': 'image.basic', 'pandas': 'data.tabular', 'qrcode': 'image.qr', 'markdown': 'text.markdown', 'barcode': 'image.barcode', 'defusedxml': 'data.xml',
    'pypdf': 'pdf.read', 'pdfplumber': 'pdf.read', 'reportlab': 'pdf.write', 'numpy': 'data.tabular'}


def import_evidence(skill_md):
    """Parse import statements without executing code, including aliases/lists."""
    found = []
    for number, line in enumerate(skill_md.splitlines(), 1):
        if not re.match(r'^\s*(?:from|import)\s+', line):
            continue
        try:
            tree = ast.parse(line.strip())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            found.extend({'module': name.split('.')[0], 'line': number} for name in names if name)
    return found


def analyze_capabilities(skill_md, manifest):
    analysis = capability_requirements(skill_md, manifest)
    # Imports in examples are hints, never executable code or arbitrary pip requirements.
    # Only import statements, not prose such as "do not import qrcode".
    import_refs = import_evidence(skill_md)
    imports = {item['module'] for item in import_refs}
    inferred = set(analysis['inferred_capabilities'])
    evidence = []
    for number, line in enumerate(skill_md.splitlines(), 1):
        for clause in re.split(r'[。；;.!?\n]', line):
            if re.search(r'禁止|严禁|不要|无需|不得|不需要|不生成|不创建|\b(?:not|never|without|avoid)\b', clause, re.I):
                continue
            if re.search(r'(?:生成|创建|制作|绘制|输出)\s*(?:一个|新的|彩色)?\s*二维码|\b(?:generate|create|make|render)\s+(?:(?:a|an|the|new)\s+)?qr[ -]?codes?\b', clause, re.I):
                inferred.add('image.qr')
                evidence.append({'capability': 'image.qr', 'source': 'generation_intent', 'line': number})
            for cap, pattern in {
                'image.barcode': r'(?:生成|创建|制作|绘制|输出)\s*(?:一维)?条[形码]*码|\b(?:generate|create|render)\s+(?:(?:a|the)\s+)?barcodes?\b',
                'text.markdown': r'markdown\s*(?:转换?为|转成|转为|to|into)\s*html',
                'data.xml': r'(?:解析|读取)\s*XML|\b(?:parse|read)\s+(?:an?\s+)?XML\b',
            }.items():
                if re.search(pattern, clause, re.I):
                    inferred.add(cap)
                    evidence.append({'capability': cap, 'source': 'task_intent', 'line': number})
    inferred.update(IMPORT_CAPABILITIES[n] for n in imports if n in IMPORT_CAPABILITIES)
    evidence.extend({'capability': IMPORT_CAPABILITIES[item['module']], 'source': 'import_statement', **item}
                    for item in import_refs if item['module'] in IMPORT_CAPABILITIES)
    evidence.extend({'capability': c, 'source': 'declaration'} for c in analysis['declared_capabilities'])
    explained = {item['capability'] for item in evidence}
    evidence.extend({'capability': c, 'source': 'document_hint'} for c in sorted(inferred - explained))
    analysis['evidence'] = evidence
    analysis['inferred_capabilities'] = sorted(inferred - set(analysis['declared_capabilities']))
    analysis['method'] = 'declarations_and_catalog_hints_v3'
    analysis['unresolved_imports'] = sorted(imports - set(IMPORT_CAPABILITIES) - sys.stdlib_module_names)[:32]
    analysis['unsupported_capabilities'] = sorted(set(analysis['declared_capabilities']) - set(CAPABILITIES))
    analysis['warnings'] = (
        ['存在未映射的导入模块；可能是本地模块或未支持的依赖，平台不会据此自动安装。']
        if analysis['unresolved_imports'] else [])
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


def environment_spec(capabilities, base_image=None, *, python_requirements=None, python_imports=None):
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
    result = {'policy': POLICY_VERSION, 'probe_digest': probe_digest, 'extension_digest': extension_digest, 'base_image': base, 'platform': 'linux/amd64',
            'capabilities': caps, 'wheels': [wheels[k] for k in sorted(wheels)]}
    if python_requirements or python_imports:
        from .python_dependencies import normalize_requirements, normalize_imports
        result.update(python_requirements=normalize_requirements(python_requirements or []),
                      python_imports=normalize_imports(python_imports or []), python_policy=1)
    return result


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
        from .python_dependencies import analyze_python_dependencies
        python_analysis = analyze_python_dependencies(version.skill_md, version.manifest or {}, package)
        analysis.update(python_analysis)
        analysis['unresolved_imports'] = []
        analysis['warnings'] = []
        spec = environment_spec(caps, python_requirements=python_analysis['python_requirements'], python_imports=python_analysis['python_imports'])
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
    common = [{k: v for k, v in e.spec.items() if k not in {'capabilities', 'wheels', 'python_requirements', 'python_imports', 'python_policy'}} for e in envs]
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
    if any(e.spec.get('python_requirements') or e.spec.get('python_imports') for e in envs):
        from .python_dependencies import normalize_requirements, normalize_imports
        spec.update(python_policy=1,
            python_requirements=normalize_requirements([r for e in envs for r in e.spec.get('python_requirements',[])]),
            python_imports=normalize_imports([r for e in envs for r in e.spec.get('python_imports',[])]))
    return enqueue_environment(db, spec)
