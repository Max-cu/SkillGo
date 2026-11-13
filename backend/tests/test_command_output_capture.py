import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.command_output import CAPTURE_PROGRAM, INLINE_OUTPUT_BYTES, decode_capture
from app.sandbox_runtime import SandboxCommandResult
from app.sandbox_agent_loop import _command_result_payload


def test_capture_preserves_binary_bytes():
    original = b'\x00\xff\n'
    payload = dict(stdout=base64.b64encode(original).decode(), stderr='', exit_code=7)
    assert decode_capture(json.dumps(payload))['stdout'] == original


@pytest.mark.parametrize('exit_code,timed_out', [(0, False), (7, False), (124, True)])
def test_excerpt_keeps_real_outcome_and_full_log_references(exit_code, timed_out):
    paths = {'stdout': '/workspace/work/command-output/test/stdout.log',
             'stderr': '/workspace/work/command-output/test/stderr.log'}
    result = SandboxCommandResult(exit_code, 'head and tail', '', timed_out=timed_out,
                                 output_files=paths, output_bytes={'stdout': 20_000_000, 'stderr': 0},
                                 output_excerpt=True)
    payload = _command_result_payload(result, timeout_seconds=60)
    assert payload['exit_code'] == exit_code
    assert payload['output_files'] == paths
    assert payload['output_bytes']['stdout'] == 20_000_000
    assert payload['output_excerpt'] is True
    assert (payload.get('ok') is False) == (exit_code != 0)


@pytest.mark.skipif(os.name == 'nt', reason='The command capture bridge uses Linux directory descriptors.')
@pytest.mark.parametrize('large,exit_code', [(False, 0), (False, 7), (True, 0), (True, 7)])
def test_real_capture_preserves_complete_large_output(tmp_path, large, exit_code):
    source = CAPTURE_PROGRAM.replace("'/workspace'", repr(str(tmp_path))).replace(
        "'/workspace/work/command-output/'", repr(str(tmp_path / 'work' / 'command-output') + '/'))
    count = INLINE_OUTPUT_BYTES + 17 if large else 12
    command = f"import os;os.write(1,b'x'*{count});os.write(2,b'error');raise SystemExit({exit_code})"
    run = subprocess.run([sys.executable, '-I', '-c', source, str(INLINE_OUTPUT_BYTES),
                          sys.executable, '-c', command], capture_output=True, check=True)
    payload = decode_capture(run.stdout)
    assert payload['exit_code'] == exit_code
    assert payload['output_bytes'] == {'stdout': count, 'stderr': 5}
    if large:
        assert len(payload['stdout']) < 20000
        assert Path(payload['output_files']['stdout']).read_bytes() == b'x' * count
        assert Path(payload['output_files']['stderr']).read_bytes() == b'error'
    else:
        assert payload['stdout'] == b'x' * count and payload['stderr'] == b'error'
        if exit_code:
            assert Path(payload['output_files']['stdout']).read_bytes() == b'x' * count
            assert Path(payload['output_files']['stderr']).read_bytes() == b'error'
        else:
            assert payload['output_files'] is None
            assert not list((tmp_path / 'work' / 'command-output').iterdir())
