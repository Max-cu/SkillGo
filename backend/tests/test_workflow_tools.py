import asyncio
import hashlib

import pytest

from app.sandbox_runtime import SandboxRuntimeError
from app.workflow_tools import snapshot_plan_refs, snapshot_step_files


class TreeSandbox:
    """Minimal sandbox with files and directories, like DockerSandbox tree."""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)

    async def list_files(self, path):
        base = path.rstrip('/')
        items = []
        for name in sorted(self.files):
            if not name.startswith(base + '/'):
                continue
            rel = name[len(base) + 1:]
            # Walk all path components so directories are represented too.
            parts = rel.split('/')
            for depth in range(1, len(parts)):
                d = base + '/' + '/'.join(parts[:depth])
                items.append({'path': d, 'type': 'dir'})
            items.append({'path': name, 'type': 'file', 'size': len(self.files[name])})
        # Deduplicate while preserving order.
        seen = set()
        unique = []
        for item in items:
            key = (item['path'], item['type'])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def read_workspace_file(self, path):
        if path not in self.files:
            raise SandboxRuntimeError('SANDBOX_ARTIFACT_MISSING', path)
        return self.files[path]


def test_regular_file_ref_hashes_content():
    sb = TreeSandbox({'/workspace/work/a.json': b'{"x":1}'})
    files, missing = asyncio.run(snapshot_plan_refs(sb, ['/workspace/work/a.json']))
    assert missing == []
    assert files == {'/workspace/work/a.json': hashlib.sha256(b'{"x":1}').hexdigest()}


def test_directory_ref_binds_aggregate_and_missing_path_is_reported():
    sb = TreeSandbox({
        '/workspace/assets/sources/文本.md': b'hello',
        '/workspace/assets/sources/img_1.png': b'\x89PNG',
    })
    files, missing = asyncio.run(
        snapshot_plan_refs(sb, ['/workspace/assets/sources', '/workspace/assets/nope'])
    )
    assert set(missing) == {'/workspace/assets/nope'}
    digest = files['/workspace/assets/sources']
    # Aggregate over relative path + per-member content hash.
    agg = hashlib.sha256()
    members = {'img_1.png': b'\x89PNG', '文本.md': b'hello'}
    for rel in sorted(members):
        agg.update(f'{rel}:{hashlib.sha256(members[rel]).hexdigest()}\n'.encode())
    assert digest == agg.hexdigest()


def test_directory_hash_changes_when_a_member_changes():
    before = TreeSandbox({'/workspace/out/svg/a.svg': b'<svg/>'})
    first, missing = asyncio.run(snapshot_plan_refs(before, ['/workspace/out/svg']))
    assert missing == []
    after = TreeSandbox({'/workspace/out/svg/a.svg': b'<svg>changed</svg>'})
    second, _ = asyncio.run(snapshot_plan_refs(after, ['/workspace/out/svg']))
    assert first['/workspace/out/svg'] != second['/workspace/out/svg']


def test_missing_path_is_warning_input_not_an_exception():
    sb = TreeSandbox({})
    files, missing = asyncio.run(snapshot_plan_refs(sb, ['/workspace/output/report.docx']))
    assert files == {} and missing == ['/workspace/output/report.docx']
    # Backward-compatible wrapper returns just the map.
    assert asyncio.run(snapshot_step_files(sb, ['/workspace/output/report.docx'])) == {}


def test_path_traversal_still_rejected():
    sb = TreeSandbox({'/workspace/work/a': b'a'})
    try:
        asyncio.run(snapshot_plan_refs(sb, ['/workspace/../etc/passwd']))
    except SandboxRuntimeError as exc:
        assert exc.code == 'PLAN_PATH_INVALID'
    else:
        raise AssertionError('traversal path was accepted')
