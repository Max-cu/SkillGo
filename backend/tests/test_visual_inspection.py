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
