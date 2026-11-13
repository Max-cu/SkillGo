"""Bounded, redacted execution evidence for failed tools, never model reasoning."""
from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
from typing import Any

STREAM_LIMIT = 2400
STAGE_LIMIT = 2400
DISPLAY_LIMIT = 14000
_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')
_PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)', re.S)
_ASSIGNMENT = re.compile(
    r'''(?ix)(["']?\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|authorization|secret|token)\b["']?\s*[:=]\s*)
    (?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}&]+)''')


def gateway_secrets(gateway) -> tuple[str, ...]:
    """Read known provider credentials in memory without persisting them."""
    source = getattr(gateway, 'capability_gateway', None) or gateway
    connections = [getattr(gateway, 'connection', None), getattr(source, 'connection', None)]
    connections.extend((getattr(source, 'connections', None) or {}).values())
    values={key for c in connections if isinstance(key := getattr(c, 'api_key', None), str) and key}
    values.update(value for key,value in os.environ.items() if value and
        re.search(r'(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD)$',key,re.I))
    return tuple(values)


def redact(text: Any, secrets=()) -> str:
    if not isinstance(text, str):
        return ''
    text = _ANSI.sub('', text)
    text = _PRIVATE_KEY.sub('[REDACTED PRIVATE KEY]', text)
    # Placeholder credentials such as "1" must not erase digits in paths,
    # HTTP codes, coordinates or line numbers. Short values are still masked
    # by the sensitive-field, Authorization and credential-URL rules below.
    for secret in sorted((s for s in secrets if isinstance(s, str) and len(s) >= 8), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[^\s"\',;}]+', r'\1 [REDACTED]', text)
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED JWT]', text)
    text = re.sub(r'\b(?:sk|sk-proj|sk-ant)-[A-Za-z0-9_-]{12,}\b', '[REDACTED API KEY]', text)
    text = _ASSIGNMENT.sub(r'\1[REDACTED]', text)
    text = re.sub(r'(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret|password)=)[^\s&#"\']+', r'\1[REDACTED]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)


def tail(text: Any, limit: int, secrets=()) -> tuple[str, bool]:
    clean = redact(text, secrets)
    return clean[-limit:], len(clean) > limit


def failed(payload) -> bool:
    return isinstance(payload, dict) and (payload.get('ok') is False or
        (type(payload.get('exit_code')) is int and payload['exit_code'] != 0))


def stream_evidence(payload: dict, secrets=()) -> dict:
    evidence = {'version':1, 'capture_status':'streams_only', 'stages':[]}
    for name in ('stdout','stderr'):
        excerpt, truncated = tail(payload.get(name,''), STREAM_LIMIT, secrets)
        evidence[name+'_tail'] = excerpt
        evidence[name+'_truncated'] = truncated
    return evidence


def _workflow_path(value, cwd, suffix):
    if not isinstance(value, str) or len(value)>1024 or '\x00' in value:
        return None
    root = posixpath.normpath(cwd)
    if not root.startswith('/workspace/'):
        return None
    base = root+'/assets/.workflow/'
    path = posixpath.normpath(value if value.startswith('/') else posixpath.join(root,value))
    return path if path.startswith(base) and path.endswith(suffix) else None


async def _read_stage_window(sandbox, path, *, offset, limit):
    # A transient sandbox read must not lose all stage evidence. Retry only
    # transport/read failures once, within attach()'s existing total budget.
    for attempt in range(2):
        try:
            return await sandbox.read_text_window(path, offset=offset, limit=limit)
        except Exception as exc:
            if attempt or getattr(exc, 'code', None) not in {
                'SANDBOX_READ_FAILED', 'SANDBOX_COMMAND_FAILED'
            }:
                raise


async def _capture_stages(sandbox, payload, cwd, evidence, secrets):
    # Only follow paths explicitly reported by the failed script, and only
    # inside that Skill's workflow diagnostic directory. Never read source,
    # uploads, arbitrary JSON/config files, or paths outside its sandbox.
    summaries=[]
    for line in str(payload.get('stdout') or '').splitlines()[-8:]:
        try:
            item=json.loads(line)
        except ValueError:
            continue
        if isinstance(item,dict) and ('failed_logs' in item or 'report' in item):
            summaries.append(item)
    log_paths=[]
    if summaries:
        item=summaries[-1]
        declared=item.get('failed_logs')
        log_paths.extend(x for x in declared if isinstance(x,str)) if isinstance(declared,list) else None
        report_path=_workflow_path(item.get('report'),cwd,'.json')
        if report_path:
            window=await _read_stage_window(sandbox,report_path,offset=0,limit=16000)
            if window.get('eof'):
                try:
                    report=json.loads(window['content'])
                except ValueError:
                    report={}
                if isinstance(report,dict):
                    if isinstance(report.get('error'),str):
                        excerpt,truncated=tail(report['error'],STAGE_LIMIT,secrets)
                        evidence['stages'].append({'path':report_path,'error_tail':excerpt,'truncated':truncated})
                    steps=report.get('steps')
                    for step in steps if isinstance(steps,list) else []:
                        if isinstance(step,dict) and step.get('status')=='failed' and isinstance(step.get('log'),str):
                            log_paths.append(step['log'])
    seen=set()
    for value in log_paths:
        path=_workflow_path(value,cwd,'.log')
        if not path or path in seen:
            continue
        seen.add(path)
        if len(seen)>2:
            break
        window=await _read_stage_window(sandbox,path,offset=0,limit=1)
        total=window.get('total_chars',0)
        # Read enough trailing text to redact complete credentials before
        # clipping the durable excerpt; known keys are also redacted verbatim.
        window=await _read_stage_window(sandbox,path,offset=max(0,total-12000),limit=12000)
        excerpt,truncated=tail(window['content'],STAGE_LIMIT,secrets)
        evidence['stages'].append({'path':path,'error_tail':excerpt,'truncated':truncated or total>12000})
    evidence['capture_status']='captured' if evidence['stages'] else 'streams_only'


async def attach(payload, *, sandbox, cwd, secrets=()):
    if not failed(payload):
        return payload
    evidence=stream_evidence(payload,secrets)
    try:
        await asyncio.wait_for(_capture_stages(sandbox,payload,cwd,evidence,secrets),timeout=5)
    except asyncio.TimeoutError:
        evidence['capture_status']='stage_read_timeout'
    except Exception:
        # Diagnostics are best effort and must not change the original tool
        # result or suppress cancellation of the parent job.
        evidence['capture_status']='stage_read_unavailable'
    return {**payload,'failure_diagnostics':evidence}


def durable_evidence(payload, secrets=()):
    # Rebuild a strict allowlist even if a future tool/provider supplies a
    # similarly named field; never persist arbitrary payload keys.
    source=payload.get('failure_diagnostics')
    if not isinstance(source,dict):
        return _with_command_logs(stream_evidence(payload,secrets), payload)
    result=stream_evidence({'stdout':source.get('stdout_tail',''),'stderr':source.get('stderr_tail','')},secrets)
    result['capture_status']=source.get('capture_status') if source.get('capture_status') in {
        'streams_only','captured','stage_read_timeout','stage_read_unavailable'} else 'streams_only'
    for name in ('stdout','stderr'):
        result[name+'_truncated']=result[name+'_truncated'] or source.get(name+'_truncated') is True
    stages=source.get('stages')
    for item in stages[:3] if isinstance(stages,list) else []:
        if not isinstance(item,dict):continue
        path=item.get('path')
        if not isinstance(path,str) or len(path)>1024:
            continue
        path=posixpath.normpath(path)
        if not path.startswith('/workspace/') or '/assets/.workflow/' not in path or not path.endswith(('.json','.log')):
            continue
        excerpt,truncated=tail(item.get('error_tail',''),STAGE_LIMIT,secrets)
        result['stages'].append({'path':redact(path,secrets)[:1024],'error_tail':excerpt,'truncated':truncated or item.get('truncated') is True})
    return _with_command_logs(result, payload)


def _with_command_logs(evidence, payload):
    paths = payload.get('output_files')
    if isinstance(paths, dict):
        logs = {}
        for stream in ('stdout', 'stderr'):
            path = paths.get(stream)
            if isinstance(path, str) and re.fullmatch(
                rf'/workspace/work/command-output/[0-9a-f]{{32}}/{stream}\.log', path
            ):
                logs[stream] = path
        if logs:
            evidence['command_logs'] = logs
    return evidence


def display_diagnostic(summary, evidence):
    parts=[summary]
    if evidence.get('command_logs'):
        parts.append('沙箱日志\n' + '\n'.join(evidence['command_logs'].values()))
    for field,label in (('stderr_tail','stderr 尾部'),('stdout_tail','stdout 尾部')):
        if evidence[field]:parts.append(label+':\n'+evidence[field])
    for item in evidence['stages']:
        if item['error_tail']:parts.append('阶段错误 '+item['path']+':\n'+item['error_tail'])
    return '\n\n'.join(parts)[:DISPLAY_LIMIT]
