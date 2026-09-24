import asyncio
from types import SimpleNamespace
import pytest
from app.visual_inspection import VisualInspectionCache

class Gateway:
    model_name = 'vision'
    calls = 0
    fail = False
    def for_capability(self, capability):
        return self
    async def analyze_image(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError('transport failed')
        return SimpleNamespace(output={'text': 'observation'}, model_name=self.model_name)


def test_exact_content_reuse_and_changes():
    async def scenario():
        files = {'a.png': b'one', 'b.png': b'one'}
        sandbox = SimpleNamespace(read_workspace_file=files.__getitem__)
        gateway = Gateway()
        cache = VisualInspectionCache()
        first = await cache.inspect(sandbox, gateway, path='a.png', question='read')
        first['observation']['text'] = 'tampered'
        second = await cache.inspect(sandbox, gateway, path='b.png', question='read')
        assert second['cached'] and second['path'] == 'b.png'
        assert second['observation']['text'] == 'observation'
        assert gateway.calls == 1
        files['b.png'] = b'two'
        assert not (await cache.inspect(sandbox, gateway, path='b.png', question='read'))['cached']
        assert not (await cache.inspect(sandbox, gateway, path='b.png', question='layout'))['cached']
        gateway.model_name = 'other'
        assert not (await cache.inspect(sandbox, gateway, path='b.png', question='layout'))['cached']
        assert gateway.calls == 4
        await VisualInspectionCache().inspect(sandbox, gateway, path='b.png', question='layout')
        assert gateway.calls == 5
    asyncio.run(scenario())


def test_failures_not_cached_and_cache_bounded():
    async def scenario():
        sandbox = SimpleNamespace(read_workspace_file=lambda path: b'image')
        gateway = Gateway()
        cache = VisualInspectionCache(max_entries=1)
        gateway.fail = True
        with pytest.raises(RuntimeError):
            await cache.inspect(sandbox, gateway, path='a.png', question='read')
        assert not cache.entries
        gateway.fail = False
        await cache.inspect(sandbox, gateway, path='a.png', question='read')
        await cache.inspect(sandbox, gateway, path='a.png', question='layout')
        assert len(cache.entries) == 1
        assert not (await cache.inspect(sandbox, gateway, path='a.png', question='read'))['cached']
    asyncio.run(scenario())


class DocumentGateway:
    """Gateway stub exposing separate vision/ocr connections."""
    def __init__(self):
        self.ocr_calls = 0
        self.written = {}
        self.ocr = SimpleNamespace(
            connection=SimpleNamespace(api_format='mineru'),
            parse_document_mineru=self._parse_document,
            model_name='MinerU OCR',
        )
        self.vision = SimpleNamespace(
            connection=SimpleNamespace(api_format='openai'),
            analyze_image=self._analyze_image,
            model_name='qwen-vl',
        )
    def for_capability(self, capability):
        return self.ocr if capability == 'ocr' else self.vision
    async def _parse_document(self, *, data, media_type, pages):
        self.ocr_calls += 1
        blocks = [
            {'type': 'text', 'text': '扫描标题', 'bbox': [10, 20, 100, 50], 'page_idx': 0},
        ]
        return SimpleNamespace(output={
            'blocks': blocks, 'page_count': 1, 'block_count': 1,
            'block_types': {'text': 1}, 'markdown': '扫描标题', 'provider': 'mineru',
        }, model_name='MinerU OCR')
    async def _analyze_image(self, *, data, media_type, prompt, purpose):
        return SimpleNamespace(output={'text': 'page looks fine'}, model_name='qwen-vl')


def _doc_sandbox(files):
    written = {}
    async def write_text(path, content):
        written[path] = content
    return SimpleNamespace(
        read_workspace_file=files.__getitem__,
        write_text=write_text,
        _written=written,
    )


def test_inspect_document_structure_persists_blocks_and_caches():
    async def scenario():
        files = {'/workspace/input/scan.pdf': b'%PDF-1.4'}
        sandbox = _doc_sandbox(files)
        gateway = DocumentGateway()
        cache = VisualInspectionCache()
        result = await cache.inspect_document(
            sandbox, gateway, path='/workspace/input/scan.pdf', intent='structure')
        assert result['ok'] and result['mode'] == 'document_structure'
        assert result['page_count'] == 1 and result['block_count'] == 1
        assert result['blocks'][0]['bbox'] == [10, 20, 100, 50]
        assert result['content_path'].startswith('/workspace/work/document_inspection/')
        import json
        persisted = json.loads(sandbox._written[result['content_path']])
        assert persisted['blocks'][0]['text'] == '扫描标题'
        # Second call for same file/intent is cached: no second MinerU call.
        again = await cache.inspect_document(
            sandbox, gateway, path='/workspace/input/scan.pdf', intent='auto')
        assert again['cached'] is True and gateway.ocr_calls == 1
    asyncio.run(scenario())


def test_inspect_document_auto_routes_images_to_vision():
    async def scenario():
        files = {'/workspace/work/page_01.png': b'\x89PNG'}
        sandbox = _doc_sandbox(files)
        gateway = DocumentGateway()
        cache = VisualInspectionCache()
        result = await cache.inspect_document(
            sandbox, gateway, path='/workspace/work/page_01.png', intent='auto', question='版面如何')
        assert result['mode'] == 'vision'
        assert result['observation']['text'] == 'page looks fine'
        assert gateway.ocr_calls == 0
    asyncio.run(scenario())


def test_inspect_document_understand_rejects_pdf():
    async def scenario():
        from app.sandbox_runtime import SandboxRuntimeError
        files = {'/workspace/input/d.pdf': b'%PDF'}
        sandbox = _doc_sandbox(files)
        gateway = DocumentGateway()
        with pytest.raises(SandboxRuntimeError, match='Vision understanding needs an image'):
            await VisualInspectionCache().inspect_document(
                sandbox, gateway, path='/workspace/input/d.pdf', intent='understand')
    asyncio.run(scenario())


def test_inspect_document_rejects_unsupported_type():
    async def scenario():
        from app.sandbox_runtime import SandboxRuntimeError
        files = {'/workspace/work/x.docx': b'zip'}
        sandbox = _doc_sandbox(files)
        gateway = DocumentGateway()
        with pytest.raises(SandboxRuntimeError, match='supports PDF, PNG, JPEG and WebP'):
            await VisualInspectionCache().inspect_document(
                sandbox, gateway, path='/workspace/work/x.docx')
    asyncio.run(scenario())


def test_inspect_document_action_validation():
    from app.sandbox_tool_registry import validate_agent_action
    base = {'action': 'inspect_document', 'path': '/workspace/input/a.pdf'}
    assert validate_agent_action(dict(base)) is None
    assert validate_agent_action({**base, 'intent': 'structure', 'pages': [0, 2]}) is None
    assert validate_agent_action({**base, 'intent': 'bad'})
    assert validate_agent_action({**base, 'pages': [2, 1]})
    assert validate_agent_action({**base, 'pages': [0]})
    assert validate_agent_action({'action': 'inspect_document'})

