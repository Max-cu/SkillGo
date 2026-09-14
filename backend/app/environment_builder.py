"""Isolated wheel fetch, offline installation and non-root functional probes.
Only platform-generated specifications enter these containers. No Skill ZIP,
workspace, database/model credentials or Docker socket is mounted inside them.
"""
from __future__ import annotations
import hashlib
import base64
import io
import json
import tarfile
import time
from pathlib import Path
from .config import settings
from .environment_preparation import EXTENSIONS, environment_spec

MAX_WHEEL_BYTES = 32 * 1024 * 1024


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


def run_container(client, *, image, argv, attempt, network=False, user='10001:10001', writable=False):
    return client.containers.create(image=image, command=argv, user=user, runtime=settings.sandbox_runtime or 'runsc',
        network_mode='bridge' if network else 'none', read_only=not writable,
        tmpfs={'/tmp': 'rw,nosuid,nodev,size=128m,mode=1777'}, cap_drop=['ALL'],
        security_opt=['no-new-privileges:true'], mem_limit='768m', nano_cpus=1000000000,
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


def build_environment(client, spec, attempt, on_probing=lambda: None):
    # Reject tampered DB specs and stale catalog policy; DB is not executable authority.
    if spec != environment_spec(spec['capabilities'], spec['base_image']):
        raise ValueError('Environment specification no longer matches the platform catalog')
    base = client.images.get(spec['base_image'])
    if base.id != spec['base_image'] or base.attrs.get('Architecture') != 'amd64' or base.attrs.get('Os') != 'linux':
        raise ValueError('Base image identity/platform mismatch')
    deadline = time.monotonic() + settings.environment_build_seconds
    containers = []
    candidate = None
    try:
        files = {}
        if spec['wheels']:
            # Fixed HTTPS host, no redirects, no pip, no import/install of downloaded content.
            downloader = """import hashlib,json,urllib.request,urllib.parse,base64
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): raise ValueError('Redirects forbidden')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
output={}
for w in WHEELS:
 u=urllib.parse.urlsplit(w['url'])
 assert u.scheme=='https' and u.netloc=='files.pythonhosted.org' and not u.query and not u.fragment
 assert '/' not in w['filename'] and w['filename'].endswith('.whl')
 with opener.open(w['url'],timeout=30) as r: data=r.read(33554433)
 assert len(data)<=33554432 and hashlib.sha256(data).hexdigest()==w['sha256']
 output[w['filename']]=base64.b64encode(data).decode()
print(json.dumps(output))
"""
            code = 'WHEELS=' + repr(spec['wheels']) + '\n' + downloader
            fetch = run_container(client, image=base.id, argv=['python3', '-I', '-c', code], attempt=attempt, network=True)
            containers.append(fetch)
            fetch.start()
            wait_container(fetch, deadline)
            downloaded = json.loads(fetch.logs().decode())
            for wheel in spec['wheels']:
                data = base64.b64decode(downloaded[wheel['filename']], validate=True)
                if len(data) > MAX_WHEEL_BYTES or hashlib.sha256(data).hexdigest() != wheel['sha256']:
                    raise ValueError('Wheel digest/size mismatch')
                files['skillgo-build/wheels/' + wheel['filename']] = data
            fetch.remove(force=True)
            containers.remove(fetch)
            locks = '\n'.join(w['name']+'=='+w['version']+' --hash=sha256:'+w['sha256'] for w in spec['wheels'])
            files['skillgo-build/requirements.lock'] = locks.encode()
            install_code = """import importlib.metadata as m,json,subprocess,sys,os,pathlib,tarfile,io,base64
assert sys.version_info[:2]==(3,12), 'Unsupported Python ABI'
target='/tmp/extension'
subprocess.run(['python3','-I','-m','pip','install','--no-compile','--no-index','--no-deps','--only-binary=:all:','--require-hashes','--target='+target,'--find-links=/opt/skillgo-build/wheels','-r','/opt/skillgo-build/requirements.lock'],check=True,stdout=sys.stderr)
subprocess.run(['python3','-c',"import sys;sys.path.insert(0,'/tmp/extension');from pip._internal.cli.main import main;sys.exit(main(['check']))"],check=True,stdout=sys.stderr)
for file in pathlib.Path(target).rglob('*'):
 assert not file.is_symlink(), 'Symlink in extension'
 if file.is_file():
  relative=file.relative_to(target)
  assert not (pathlib.Path('/usr/local/lib/python3.12/site-packages')/relative).exists(), 'Extension overwrites base file'
b=io.BytesIO()
with tarfile.open(fileobj=b,mode='w') as t:
 t.add(target,arcname='extension')
assert len(b.getvalue())<=67108864
print(base64.b64encode(b.getvalue()).decode())
"""
            installer = run_container(client, image=base.id, argv=['python3', '-I', '-c', install_code], attempt=attempt, user='0:0', writable=True)
            containers.append(installer)
            installer.put_archive('/opt', archive_files(files))
            installer.start()
            wait_container(installer, deadline)
            exported = base64.b64decode(installer.logs(stdout=True, stderr=False), validate=False)
            if len(exported) > 64 * 1024 * 1024:
                raise ValueError('Extension export too large')
            # No RUN instructions: only validated regular files from the isolated installer.
            context_files = {}
            with tarfile.open(fileobj=io.BytesIO(exported)) as tar:
                members = tar.getmembers()
                if len(members) > 10000 or sum(m.size for m in members) > 64 * 1024 * 1024:
                    raise ValueError('Extension contents exceed limits')
                for member in members:
                    from pathlib import PurePosixPath
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or '..' in path.parts or path.parts[0] != 'extension':
                        raise ValueError('Invalid extension path')
                    if member.isdir(): continue
                    if not member.isfile(): raise ValueError('Extension must contain only regular files')
                    context_files[member.name] = tar.extractfile(member).read()
            context_files['Dockerfile'] = (f'FROM {base.id}\nCOPY extension/ /usr/local/lib/python3.12/site-packages/\nUSER 10001:10001\nWORKDIR /workspace\nCMD ["sleep", "infinity"]\n').encode()
            candidate, _ = client.images.build(fileobj=io.BytesIO(archive_files(context_files)), custom_context=True,
                rm=True, forcerm=True, pull=False, network_mode='none', timeout=max(1, int(deadline-time.monotonic())),
                labels={'skillgo.prepared_environment': 'true', 'skillgo.build_attempt': attempt})
            image_id = candidate.id
        else:
            image_id = base.id
        on_probing()
        probe = Path(__file__).with_name('environment_probe.py').read_text(encoding='utf-8')
        checks = '\n'.join(EXTENSIONS[cap]['probe'] for cap in spec['capabilities'] if cap in EXTENSIONS)
        probe_container = run_container(client, image=image_id, argv=['python3', '-I', '-c', checks+'\n'+probe], attempt=attempt)
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
