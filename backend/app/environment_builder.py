"""Isolated wheel fetch, offline installation and non-root functional probes.
Only platform-generated specifications enter these containers. No Skill ZIP,
workspace, database/model credentials or Docker socket is mounted inside them.
"""
from __future__ import annotations
import hashlib
import base64
import io
import json
import logging
import os
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from .config import settings
from .environment_preparation import EXTENSIONS, environment_spec

MAX_WHEEL_BYTES = 128 * 1024 * 1024
MAX_WHEEL_BATCH_BYTES = 192 * 1024 * 1024
MAX_WHEEL_CACHE_BYTES = 4 * 1024 * 1024 * 1024
MAX_EXTENSION_BYTES = 512 * 1024 * 1024
INSTALL_MARKER = '/opt/skillgo-build/install-ready'


def wheel_cache_path(cache, wheel):
    digest = wheel['sha256']
    filename = wheel['filename']
    if (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
            or not filename.endswith('.whl') or '/' in filename or '\\' in filename):
        raise ValueError('Invalid wheel lock entry')
    return cache / digest if cache else None


def verified_wheel(wheel, data):
    return len(data) <= MAX_WHEEL_BYTES and hashlib.sha256(data).hexdigest() == wheel['sha256']


def read_cached_wheel(path, wheel):
    if path is None:
        return None
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_WHEEL_BYTES + 1)
        if verified_wheel(wheel, data):
            path.touch()
            return data
    except OSError:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning('Could not discard an invalid cached wheel')
    return None


def cache_wheel(path, data):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    entries = []
    for entry in path.parent.iterdir():
        if len(entry.name) == 64 and all(c in '0123456789abcdef' for c in entry.name):
            try:
                info = entry.stat()
            except FileNotFoundError:
                continue
            entries.append((info.st_mtime, info.st_size, entry))
    entries.sort()
    size = sum(entry[1] for entry in entries)
    for _, length, entry in entries:
        if size <= MAX_WHEEL_CACHE_BYTES:
            break
        entry.unlink(missing_ok=True)
        size -= length


def fetch_missing_wheels(client, base, missing, cache, files, attempt, deadline, containers, generic):
    downloader = Path(__file__).with_name('dependency_download.py').read_text(encoding='utf-8')
    code = ('WHEELS=' + repr(missing) + '\nWORKERS=' + str(4 if generic else 1)
            + '\nBATCH_LIMIT=' + str(MAX_WHEEL_BATCH_BYTES) + '\n' + downloader + """
import base64,json
from concurrent.futures import ThreadPoolExecutor,as_completed
total=0
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
 futures={pool.submit(fetch_wheel,w):w for w in WHEELS}
 failed=False
 for future in as_completed(futures):
  try:
   data=future.result()
  except Exception as exc:
   print(str(exc),file=sys.stderr,flush=True)
   failed=True
   continue
  total+=len(data)
  if total>BATCH_LIMIT:
   raise ValueError('Downloaded wheels exceed 192 MiB')
  print(json.dumps([futures[future]['filename'],base64.b64encode(data).decode()]),flush=True)
 if failed:
  raise SystemExit(1)
""")
    fetch = run_container(client, image=base.id, argv=['python3', '-I', '-c', code],
                          attempt=attempt, network=True, large=generic)
    containers.append(fetch)
    fetch.start()
    expected = {wheel['filename']: wheel for wheel in missing}
    try:
        wait_container(fetch, deadline)
    finally:
        # Keep successfully downloaded wheels even when another wheel failed.
        for line in io.BytesIO(fetch.logs(stdout=True, stderr=False)):
            try:
                filename, encoded = json.loads(line)
                wheel = expected[filename]
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, KeyError, TypeError):
                continue
            if verified_wheel(wheel, data):
                try:
                    cache_wheel(wheel_cache_path(cache, wheel), data)
                except OSError:
                    logging.getLogger(__name__).warning('Could not store a verified wheel in the build cache')
                files['skillgo-build/wheels/' + filename] = data
    if any('skillgo-build/wheels/' + name not in files for name in expected):
        raise ValueError('Wheel download did not produce every verified lock entry')
    fetch.remove(force=True)
    containers.remove(fetch)


@contextmanager
def export_extension(installer, extra_files):
    """Stream the archive from inside gVisor's writable filesystem."""
    api = installer.client.api
    execution = api.exec_create(
        installer.id, ['tar', '-C', '/opt/skillgo-build', '-cf', '-', 'extension'],
        stdout=True, stderr=False,
    )['Id']
    chunks = api.exec_start(execution, stream=True)
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as exported:
        size = 0
        try:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_EXTENSION_BYTES:
                    raise ValueError(f'Installed dependencies exceed the {MAX_EXTENSION_BYTES // (1024 * 1024)} MiB environment archive limit')
                exported.write(chunk)
        finally:
            chunks.close()
        if api.exec_inspect(execution)['ExitCode'] != 0:
            raise RuntimeError('Could not export installed dependencies from the build container')
        exported.seek(0)
        with tarfile.open(fileobj=exported) as tar:
            members = tar.getmembers()
            if len(members) > 10000 or sum(m.size for m in members) > MAX_EXTENSION_BYTES:
                raise ValueError('Installed dependency archive exceeds file count or size limit')
            with tempfile.TemporaryFile() as context:
                with tarfile.open(fileobj=context, mode='w') as output:
                    for member in members:
                        from pathlib import PurePosixPath
                        path = PurePosixPath(member.name)
                        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'extension':
                            raise ValueError('Invalid extension path')
                        if member.isdir():
                            continue
                        if not member.isfile():
                            raise ValueError('Extension must contain only regular files')
                        with tar.extractfile(member) as contents:
                            output.addfile(member, contents)
                    for name, data in extra_files.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        output.addfile(info, io.BytesIO(data))
                context.seek(0)
                yield context


def archive_files(files):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        for directory in sorted({str(Path(name).parent).replace('\\','/') for name in files if str(Path(name).parent) != '.'}):
            parts = directory.split('/')
            for count in range(1, len(parts)+1):
                info = tarfile.TarInfo('/'.join(parts[:count]))
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def run_container(client, *, image, argv, attempt, network=False, user='10001:10001', writable=False, large=False):
    return client.containers.create(image=image, command=argv, user=user, runtime=settings.sandbox_runtime or 'runsc',
        network_mode='bridge' if network else 'none', read_only=not writable,
        tmpfs={'/tmp': 'rw,nosuid,nodev,size='+('512m' if large else '128m')+',mode=1777'}, cap_drop=['ALL'],
        security_opt=['no-new-privileges:true'], mem_limit='1536m' if large else '768m', nano_cpus=1000000000,
        pids_limit=64, labels={'skillgo.environment_build': 'true', 'skillgo.build_attempt': attempt},
        environment={'HOME': '/tmp', 'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'})


def wait_container(container, deadline):
    while time.monotonic() < deadline:
        container.reload()
        if container.status in {'exited', 'dead'}:
            if container.attrs['State'].get('ExitCode') != 0:
                raise RuntimeError(container.logs(tail=25).decode('utf-8', errors='replace')[-4000:])
            return
        time.sleep(0.2)
    raise TimeoutError('Environment preparation exceeded its total budget')


def wait_for_install(installer, deadline):
    while time.monotonic() < deadline:
        installer.reload()
        if installer.status in {'exited', 'dead'}:
            wait_container(installer, deadline)
            raise RuntimeError('Dependency installer exited before exporting its files')
        if installer.exec_run(['test', '-f', INSTALL_MARKER]).exit_code == 0:
            return
        time.sleep(1)
    raise TimeoutError('Environment preparation exceeded its total budget during dependency installation')


def build_environment(client, spec, attempt, on_probing=lambda: None):
    # Reject tampered DB specs and stale catalog policy; DB is not executable authority.
    if spec != environment_spec(spec['capabilities'], spec['base_image'], python_requirements=spec.get('python_requirements'), python_imports=spec.get('python_imports'), layout=spec.get('layout')):
        raise ValueError('Environment specification no longer matches the platform catalog')
    base = client.images.get(spec['base_image'])
    if base.id != spec['base_image'] or base.attrs.get('Architecture') != 'amd64' or base.attrs.get('Os') != 'linux':
        raise ValueError('Base image identity/platform mismatch')
    deadline = time.monotonic() + settings.environment_build_seconds
    containers = []
    candidate = None
    try:
        system_packages = spec.get('system_packages', [])
        if 'office.convert' in spec['capabilities'] or system_packages:
            # Platform-owned packages only; no Skill files enter this build.
            packages = system_packages or ['libreoffice-writer', 'libreoffice-calc', 'libreoffice-impress']
            office_dockerfile = (
                f'FROM {base.id}\nUSER 0:0\n'
                "RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then "
                "sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; "
                "s|https://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' "
                "/etc/apt/sources.list.d/debian.sources; fi && "
                'apt-get -o Acquire::Retries=3 update && '
                'DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends '
                + ' '.join(packages) + ' && rm -rf /var/lib/apt/lists/*\n'
                'USER 10001:10001\n'
            )
            base, _ = client.images.build(
                fileobj=io.BytesIO(archive_files({'Dockerfile': office_dockerfile.encode()})),
                custom_context=True, rm=True, forcerm=True, pull=False, network_mode='bridge',
                timeout=max(1, int(deadline-time.monotonic())),
                labels={'skillgo.platform_office': 'true'},
            )
        files = {}
        generic = bool(spec.get('python_requirements') or spec.get('python_imports')
                       or spec.get('capability_packages') or 'data.tabular' in spec['capabilities'])
        wheels = spec['wheels']
        if generic:
            resolver = Path(__file__).with_name('python_dependency_resolver.py').read_text(encoding='utf-8-sig')
            requests = spec.get('python_requirements',[]) + [w['name']+'=='+w['version'] for w in wheels]
            from packaging.requirements import Requirement
            from packaging.utils import canonicalize_name
            requested_names = {canonicalize_name(Requirement(r).name) for r in requests}
            requests += [r for r in spec.get('capability_packages', [])
                         if canonicalize_name(Requirement(r).name) not in requested_names]
            if 'data.tabular' in spec['capabilities'] and not spec.get('layout'):
                requests.append('pandas')  # Required by the platform's functional capability probe.
            resolver_code = 'SPEC=' + repr({'requirements':requests}) + '\n' + resolver
            resolving = run_container(client, image=base.id, argv=['python3','-I','-c',resolver_code],
                                      attempt=attempt, network=True, large=True)
            containers.append(resolving)
            resolving.start()
            wait_container(resolving, deadline)
            resolved = json.loads(resolving.logs(stdout=True,stderr=False).decode())
            wheels = resolved['wheels']
            if len(wheels)>128: raise ValueError('Too many resolved dependencies')
            resolving.remove(force=True)
            containers.remove(resolving)
        if wheels:
            cache_root = os.environ.get('SKILLGO_WHEEL_CACHE_DIR')
            cache = Path(cache_root) if cache_root else None
            missing = []
            for wheel in wheels:
                data = read_cached_wheel(wheel_cache_path(cache, wheel), wheel)
                if data is None:
                    missing.append(wheel)
                else:
                    files['skillgo-build/wheels/' + wheel['filename']] = data
            if missing:
                fetch_missing_wheels(client, base, missing, cache, files, attempt, deadline, containers, generic)
            if sum(map(len, files.values())) > MAX_WHEEL_BATCH_BYTES:
                raise ValueError('Locked wheels exceed 192 MiB')
            locks = '\n'.join(w['name']+'=='+w['version']+' --hash=sha256:'+w['sha256'] for w in wheels)
            files['skillgo-build/requirements.lock'] = locks.encode()
            install_code = """import subprocess,sys,pathlib
assert sys.version_info[:2]==(3,12), 'Unsupported Python ABI'
target='/opt/skillgo-build/extension'
subprocess.run(['python3','-I','-m','pip','install','--no-compile','--no-index','--no-deps','--only-binary=:all:','--require-hashes','--target='+target,'--find-links=/opt/skillgo-build/wheels','-r','/opt/skillgo-build/requirements.lock'],check=True,stdout=sys.stderr)
subprocess.run(['python3','-c',"import sys;sys.path.insert(0,sys.argv[1]);from pip._internal.cli.main import main;sys.exit(main(['check']))",target],check=True,stdout=sys.stderr)
for file in pathlib.Path(target).rglob('*'):
 assert not file.is_symlink(), 'Symlink in extension'
pathlib.Path('/opt/skillgo-build/install-ready').touch()
subprocess.run(['sleep','infinity'],check=True)
"""
            installer = run_container(client, image=base.id, argv=['python3', '-I', '-c', install_code], attempt=attempt, user='0:0', writable=True, large=generic)
            containers.append(installer)
            installer.put_archive('/opt', archive_files(files))
            files.clear()
            installer.start()
            wait_for_install(installer, deadline)
            # A platform-owned site hook also works with Python -I, which ignores PYTHONPATH.
            # Wheel-provided .pth files in the private directory are not executed.
            context_files = {'skillgo_environment.pth': b"import sys; sys.path.insert(0, '/opt/skillgo/python')\n"}
            context_files['Dockerfile'] = (f'FROM {base.id}\n'
                'COPY extension/ /opt/skillgo/python/\n'
                'COPY skillgo_environment.pth /usr/local/lib/python3.12/site-packages/00_skillgo_environment.pth\n'
                'ENV PATH="/opt/skillgo/python/bin:${PATH}"\n'
                'USER 10001:10001\nWORKDIR /workspace\nCMD ["sleep", "infinity"]\n').encode()
            with export_extension(installer, context_files) as context:
                installer.remove(force=True)
                containers.remove(installer)
                candidate, _ = client.images.build(fileobj=context, custom_context=True,
                    rm=True, forcerm=True, pull=False, network_mode='none', timeout=max(1, int(deadline-time.monotonic())),
                    labels={'skillgo.prepared_environment': 'true', 'skillgo.build_attempt': attempt})
            image_id = candidate.id
        else:
            image_id = base.id
        on_probing()
        probe = Path(__file__).with_name('environment_probe.py').read_text(encoding='utf-8')
        checks = '\n'.join(EXTENSIONS[cap]['probe'] for cap in spec['capabilities'] if cap in EXTENSIONS)
        import_checks = 'import importlib\n' + '\n'.join('importlib.import_module('+repr(n)+')' for n in spec.get('python_imports',[]))
        if any(r.lower().startswith(('opencv-python', 'opencv_python')) for r in spec.get('python_requirements', [])):
            import_checks += "\nimport cv2,numpy as np; assert cv2.cvtColor(np.zeros((2,2,3),dtype=np.uint8),cv2.COLOR_BGR2GRAY).shape==(2,2)"
        if any(r.lower().startswith(('opencv-python', 'opencv_python')) for r in spec.get('python_requirements', [])):
            import_checks += "\nimport cv2,numpy as np; assert cv2.cvtColor(np.zeros((2,2,3),dtype=np.uint8),cv2.COLOR_BGR2GRAY).shape==(2,2)"
        probe_container = run_container(client, image=image_id, argv=['python3', '-I', '-c', checks+'\n'+import_checks+'\n'+probe], attempt=attempt)
        containers.append(probe_container)
        probe_container.start()
        wait_container(probe_container, deadline)
        inventory = json.loads(probe_container.logs().decode())
        from .environment_capabilities import resolve_capabilities
        supported = set(resolve_capabilities(inventory['providers']))
        missing = set(spec['capabilities']) - supported
        if missing:
            raise ValueError('Capability probes failed: ' + ', '.join(sorted(missing)))
        inventory['capabilities'] = sorted(supported)
        inventory['image_id'] = image_id
        inventory['environment_layout'] = spec.get('layout', 'legacy')
        inventory['requested_capabilities'] = spec['capabilities']
        if generic:
            inventory['python_lock'] = wheels
            inventory['python_requirements'] = spec.get('python_requirements',[])
            inventory['python_imports'] = spec.get('python_imports',[])
            inventory['python_imports_verified'] = True
            inventory['python_dependency_layout'] = 'private-site-v1'
        return image_id, inventory
    except BaseException:
        if candidate is not None:
            try: client.images.remove(candidate.id)
            except Exception: pass
        raise
    finally:
        import logging
        for container in containers:
            try: container.remove(force=True)
            except Exception: logging.getLogger(__name__).exception('Build container cleanup failed attempt=%s', attempt)
