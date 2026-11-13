"""Runs only in the networked metadata resolver, never with user files or secrets.
SPEC is injected as bounded data by the trusted controller. No downloaded code runs.
"""
import importlib.metadata as metadata
import json
import re
from pathlib import Path
from urllib.parse import urlsplit,unquote
from pip._vendor.packaging.utils import canonicalize_name
from pip._internal.network.session import PipSession
from pip._internal.cli.main import main

MIRROR_INDEX='https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple'
def trusted_source(u):
    return (u.scheme=='https' and u.hostname in {'pypi.org','files.pythonhosted.org','pypi.tuna.tsinghua.edu.cn','mirrors.tuna.tsinghua.edu.cn'}
            and not u.username and not u.password and u.port in (None,443) and not u.query and not u.fragment
            and (u.hostname!='mirrors.tuna.tsinghua.edu.cn' or u.path.startswith('/pypi/web/')))
original_send=PipSession.send
def guarded_send(self,request,**kwargs):
    u=urlsplit(request.url)
    if not trusted_source(u):
        raise ValueError('Dependency source must be public PyPI over HTTPS')
    return original_send(self,request,**kwargs)
PipSession.send=guarded_send
installed={canonicalize_name(d.metadata['Name']):d.version for d in metadata.distributions() if d.metadata['Name']}
# Sandboxes have no display server. Preserve version constraints while using
# the compatible cv2 build without GUI shared-library requirements.
requirements=[re.sub(r'^opencv[-_]python(?=\s|[<=>!~;\[]|$)', 'opencv-python-headless', r, flags=re.I) for r in SPEC['requirements']]
args=['--isolated','install','--dry-run','--report','/tmp/report.json','--only-binary=:all:',
      '--index-url',MIRROR_INDEX,'--no-cache-dir','--disable-pip-version-check',
      '--timeout','30','--retries','2',*requirements]
# Keep stdout machine-readable; pip diagnostics go to stderr.
import contextlib,sys
with contextlib.redirect_stdout(sys.stderr):
    code=main(args)
    if code:
        args[args.index('--index-url')+1]='https://pypi.org/simple'
        code=main(args)
if code: raise SystemExit(code)
report=json.loads(Path('/tmp/report.json').read_text())
locks=[]
for item in report['install']:
    info=item['download_info'];u=urlsplit(info['url']);name=unquote(u.path.rsplit('/',1)[-1])
    assert trusted_source(u)
    assert name.endswith('.whl') and '/' not in name and '\\' not in name
    digest=info['archive_info'].get('hashes',{}).get('sha256','')
    assert len(digest)==64 and all(c in '0123456789abcdef' for c in digest)
    locks.append({'name':canonicalize_name(item['metadata']['name']),'version':item['metadata']['version'],
        'filename':name,'url':info['url'],'sha256':digest})
assert len(locks)<=128,'Too many transitive dependencies'
print(json.dumps({'wheels':locks,'base_distributions':installed}))
