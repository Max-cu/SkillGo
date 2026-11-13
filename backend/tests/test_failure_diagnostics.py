import asyncio
import json
from types import SimpleNamespace

import pytest

from app.failure_diagnostics import attach, redact, stream_evidence, durable_evidence, STREAM_LIMIT, STAGE_LIMIT
from app.sandbox_agent_loop import _finish_tool_event


def test_real_stderr_is_preserved_even_when_generic_message_exists():
    event=SimpleNamespace(status='running',detail='',data={'tool':'command'})
    _finish_tool_event(event,{'ok':False,'exit_code':2,'message':'Command exited with code 2',
        'stderr':'ValueError: body section missing required field title','stdout':''})
    assert 'body section missing required field title' in event.data['diagnostic']
    assert 'Command exited with code 2' in event.data['diagnostic']
    assert 'ValueError' not in event.detail


def test_small_failed_command_keeps_only_platform_log_paths_in_durable_evidence():
    from app.sandbox_agent_loop import _command_result_payload
    from app.sandbox_runtime import SandboxCommandResult

    path = '/workspace/work/command-output/' + 'a' * 32 + '/stderr.log'
    result = SandboxCommandResult(1, '', 'missing input', elapsed_seconds=7.5, output_files={
        'stdout': '/workspace/input/private.log', 'stderr': path,
    })
    event = SimpleNamespace(status='running', detail='', data={'tool': 'command'})
    _finish_tool_event(event, _command_result_payload(result, timeout_seconds=0))
    assert event.data['elapsed_seconds'] == 7.5
    assert event.data['failure_diagnostics']['command_logs'] == {'stderr': path}
    assert event.data['failure_diagnostics']['stderr_tail'] == 'missing input'


def test_stream_tail_keeps_failure_at_end_and_is_bounded():
    output='x'*30000+'\nERROR: required slot absent'
    evidence=stream_evidence({'stdout':output,'stderr':''})
    assert len(evidence['stdout_tail'])<=STREAM_LIMIT
    assert evidence['stdout_tail'].endswith('ERROR: required slot absent')
    assert evidence['stdout_truncated'] is True


@pytest.mark.parametrize('text,secret',[
    ('Authorization: Bearer abcdefghijklmn','abcdefghijklmn'),
    ('OPENAI_API_KEY="custom-secret-123"','custom-secret-123'),
    ('{"password":"database-secret"}','database-secret'),
    ('https://user:db-pass@example.test/path','db-pass'),
    ('postgresql+psycopg://user:db-pass@database/db','db-pass'),
    ('https://example.test/?token=secret-query&other=yes','secret-query'),
    ('token=secret-token','secret-token'),
    ('-----BEGIN PRIVATE KEY-----\nPRIVATE-DATA\n-----END PRIVATE KEY-----','PRIVATE-DATA'),
    ('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abcdefghijklmnop','eyJhbGciOiJIUzI1NiJ9'),
])
def test_common_credentials_are_redacted(text,secret):
    assert secret not in redact(text)


def test_known_provider_key_redacted_before_clipping_and_ansi_removed():
    key='arbitrary-provider-key-1234'
    evidence=stream_evidence({'stderr':'\x1b[31m'+'a'*4000+key+' missing title\x1b[0m'},(key,))
    assert key not in evidence['stderr_tail']
    assert '\x1b' not in evidence['stderr_tail']
    assert '[REDACTED]' in evidence['stderr_tail']


def test_short_placeholder_credentials_do_not_corrupt_diagnostics():
    text='exit 1, 120s, page01.svg, C1, line 273 column 17; HTTP 401'
    assert redact(text,('1','abc'))==text
    for text in ['api_key=1','Authorization: Bearer 1','{"token":"abc"}','https://user:1@example.org']:
        result=redact(text,('1','abc'))
        assert '[REDACTED]' in result
        assert result!=text


def test_short_value_is_masked_in_query_and_prefixed_sensitive_field():
    result=redact('http://example.org/?token=1&pages=15 OPENAI_API_KEY=abc',('1','abc'))
    assert 'token=1' not in result and 'KEY=abc' not in result
    assert 'pages=15' in result


class Sandbox:
    def __init__(self,files):self.files=files;self.reads=[]
    async def read_text_window(self,path,*,offset,limit):
        self.reads.append(path)
        text=self.files[path]
        return {'content':text[offset:offset+limit],'total_chars':len(text),'eof':offset+limit>=len(text)}


def test_pipeline_nested_error_and_stage_log_survive_sandbox_reclamation():
    root='/workspace/skills/01-ppt';report=root+'/assets/.workflow/all.json';log=root+'/assets/.workflow/all-01-gen_body.log'
    sandbox=Sandbox({report:json.dumps({'ok':False,'error':'invalid body','steps':[{'status':'failed','log':log}]}),
        log:'old output\n'*600+'ValueError: blueprint requires 4 items\napi_key=private-stage-key'})
    payload={'ok':False,'exit_code':2,'message':'Command exited with code 2','stdout':json.dumps({'ok':False,'report':report,'failed_logs':[log]}),'stderr':''}
    captured=asyncio.run(attach(payload,sandbox=sandbox,cwd=root))
    evidence=durable_evidence(captured)
    assert evidence['capture_status']=='captured'
    assert any('blueprint requires 4 items' in stage['error_tail'] for stage in evidence['stages'])
    assert 'private-stage-key' not in json.dumps(evidence)
    assert all(len(stage['error_tail'])<=STAGE_LIMIT for stage in evidence['stages'])
    event=SimpleNamespace(status='running',detail='',data={})
    _finish_tool_event(event,captured)
    assert 'blueprint requires 4 items' in event.data['diagnostic']


def test_arbitrary_and_traversal_paths_are_never_read():
    root='/workspace/skills/01-ppt';sandbox=Sandbox({})
    payload={'ok':False,'stdout':json.dumps({'report':'/workspace/input/secret.json','failed_logs':[
        '/workspace/input/customer.log',root+'/assets/.workflow/../../../input/secret.log','/etc/shadow']})}
    captured=asyncio.run(attach(payload,sandbox=sandbox,cwd=root))
    assert sandbox.reads==[]
    assert captured['failure_diagnostics']['stages']==[]


def test_diagnostic_read_failure_does_not_change_original_failure():
    root='/workspace/skills/01-ppt';sandbox=Sandbox({})
    payload={'ok':False,'exit_code':2,'stderr':'original problem',
        'stdout':json.dumps({'failed_logs':[root+'/assets/.workflow/missing.log']})}
    captured=asyncio.run(attach(payload,sandbox=sandbox,cwd=root))
    assert captured['stderr']=='original problem'
    assert captured['exit_code']==2
    assert captured['failure_diagnostics']['capture_status']=='stage_read_unavailable'


def test_transient_stage_read_retries_once_within_capture_budget():
    from app.sandbox_runtime import SandboxRuntimeError
    root='/workspace/skills/ppt';log=root+'/assets/.workflow/body.log'
    class TransientSandbox(Sandbox):
        async def read_text_window(self,path,*,offset,limit):
            if not self.reads:
                self.reads.append(path)
                raise SandboxRuntimeError('SANDBOX_READ_FAILED','temporary read failure')
            return await super().read_text_window(path,offset=offset,limit=limit)
    sandbox=TransientSandbox({log:'line 17: invalid body'})
    captured=asyncio.run(attach({'ok':False,'stdout':json.dumps({'failed_logs':[log]})},sandbox=sandbox,cwd=root))
    assert captured['failure_diagnostics']['capture_status']=='captured'
    assert captured['failure_diagnostics']['stages'][0]['error_tail']=='line 17: invalid body'
    assert len(sandbox.reads)==3


@pytest.mark.parametrize('code,expected_reads',[
    ('SANDBOX_READ_FAILED',2),('SANDBOX_PATH_DENIED',1),
])
def test_stage_read_retry_is_bounded_and_does_not_retry_path_denial(code,expected_reads):
    from app.sandbox_runtime import SandboxRuntimeError
    class UnavailableSandbox:
        reads=0
        async def read_text_window(self,*args,**kwargs):
            self.reads+=1
            raise SandboxRuntimeError(code,'unavailable')
    sandbox=UnavailableSandbox();root='/workspace/skills/ppt'
    captured=asyncio.run(attach({'ok':False,'exit_code':2,'stdout':json.dumps({'failed_logs':[root+'/assets/.workflow/body.log']})},sandbox=sandbox,cwd=root))
    assert sandbox.reads==expected_reads
    assert captured['exit_code']==2
    assert captured['failure_diagnostics']['capture_status']=='stage_read_unavailable'


def test_success_has_no_extra_reads_or_stored_output():
    payload={'exit_code':0,'stdout':'normal result'};sandbox=Sandbox({})
    assert asyncio.run(attach(payload,sandbox=sandbox,cwd='/workspace/skills/ppt')) is payload
    assert sandbox.reads==[]
    event=SimpleNamespace(status='running',detail='',data={})
    _finish_tool_event(event,payload)
    assert 'failure_diagnostics' not in event.data


def test_durable_data_uses_allowlist_not_arbitrary_payload_or_reasoning():
    payload={'ok':False,'failure_diagnostics':{'stdout_tail':'a','stderr_tail':'b',
        'stages':{'malformed':'ignored'},'reasoning_content':'never store','api_key':'never store'},
        'reasoning_content':'never store','code':'never store'}
    result=durable_evidence(payload)
    assert result['stages']==[]
    assert 'never store' not in json.dumps(result)


def test_parent_cancellation_is_not_suppressed():
    class CancelledSandbox:
        async def read_text_window(self,*args,**kwargs):raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(attach({'ok':False,'stdout':json.dumps({'failed_logs':['/workspace/skill/assets/.workflow/a.log']})},
            sandbox=CancelledSandbox(),cwd='/workspace/skill'))
