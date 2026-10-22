"""Format-independent completion of a model-declared final operation group."""
from __future__ import annotations

import json

from .artifact_collection import artifact_source_paths
from .config import settings
from .sandbox_runtime import SandboxRuntimeError
from .workflow_delivery import receive_workflow_delivery

DELIVERY_TOOLS = frozenset({'command', 'run_python', 'run_commands', 'run_skill_workflow',
                            'write_file', 'write_json'})

# Runs inside the task sandbox, not on the host. Missing files are legitimate
# before generation; unreadable files or links never count as missing/fresh.
FINGERPRINT_PROGRAM = r'''
import hashlib,json,os,stat,sys
paths=json.loads(sys.argv[1]);limit=int(sys.argv[2])
root=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
result={}
try:
 for path in paths:
  current=os.dup(root)
  try:
   parts=path.removeprefix('/workspace/').split('/')
   for part in parts[:-1]:
    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
    os.close(current);current=child
   fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
   with os.fdopen(fd,'rb') as f:
    before=os.fstat(f.fileno())
    if not stat.S_ISREG(before.st_mode):raise ValueError('Expected a regular file')
    if before.st_size>limit:raise ValueError('File exceeds artifact size limit')
    digest=hashlib.sha256();size=0
    while True:
     chunk=f.read(1024*1024)
     if not chunk:break
     size+=len(chunk)
     if size>limit:raise ValueError('File exceeds artifact size limit')
     digest.update(chunk)
    after=os.fstat(f.fileno())
    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
     raise ValueError('File changed while fingerprinting')
    result[path]={'size_bytes':size,'sha256':digest.hexdigest()}
  except FileNotFoundError:
   result[path]=None
  finally:
   os.close(current)
 print(json.dumps({'ok':True,'files':result}))
except (OSError,ValueError):
 print(json.dumps({'ok':False,'error_code':'FINAL_DELIVERY_BASELINE_UNAVAILABLE',
                   'message':'Could not safely fingerprint exact final files.'}))
finally:
 os.close(root)
'''


async def delivery_fingerprints(sandbox, paths: list[str], *, cwd: str) -> dict:
    try:
        sources = artifact_source_paths(paths, cwd=cwd)
        if len(sources) != len(paths):
            raise ValueError('Final artifacts must have distinct resolved paths')
        result = await sandbox.command(
            ['python3', '-c', FINGERPRINT_PROGRAM, json.dumps(sources),
             str(settings.sandbox_max_artifact_bytes)],
            cwd='/workspace', timeout_seconds=60, allow_large_arguments=True,
        )
        if result.exit_code != 0 or getattr(result, 'timed_out', False):
            raise ValueError('Fingerprint operation did not complete')
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or report.get('ok') is not True:
            raise ValueError('Fingerprint unavailable')
        files = report.get('files')
        if not isinstance(files, dict) or set(files) != set(sources):
            raise ValueError('Fingerprint paths do not match declaration')
        for item in files.values():
            if item is not None and (not isinstance(item, dict)
                    or type(item.get('size_bytes')) is not int or item['size_bytes'] < 0
                    or not isinstance(item.get('sha256'), str) or len(item['sha256']) != 64):
                raise ValueError('Invalid fingerprint')
        return {'ok': True, 'files': files}
    except (SandboxRuntimeError, ValueError, TypeError, KeyError):
        return {'ok': False, 'error_code': 'FINAL_DELIVERY_BASELINE_UNAVAILABLE',
                'message': 'Final operations may run, but freshness cannot be proven. '
                           'Inspect their results and use ordinary finish; do not blindly repeat exports.'}


async def receive_operation_delivery(sandbox, request: dict, baseline: dict, *, cwd: str) -> dict:
    if not baseline.get('ok'):
        return baseline
    after = await delivery_fingerprints(sandbox, request['artifacts'], cwd=cwd)
    if not after.get('ok'):
        return after
    declared = []
    for path, fingerprint in after['files'].items():
        if (not fingerprint or not fingerprint['size_bytes']
                or fingerprint == baseline['files'].get(path)):
            return {'ok': False, 'error_code': 'FINAL_DELIVERY_NOT_FRESH',
                    'message': 'Every declared file must be nonempty and created or changed by '
                               'this final operation group. Use ordinary finish for unchanged files; '
                               'do not repeat a successful export only to change its bytes.'}
        declared.append({'path': path, **fingerprint})
    return await receive_workflow_delivery(sandbox, declared, cwd=cwd)
