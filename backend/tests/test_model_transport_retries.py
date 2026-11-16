import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from app import model_adapter as adapter


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, timeout=0):
        return SimpleNamespace(timeout_seconds=timeout,connect_timeout_seconds=15,tls_verify=True)

    async def invoke(self, attempt, *, timeout=0):
        with patch.object(adapter.httpx, 'AsyncClient', return_value=AsyncMock()), \
             patch.object(adapter,'_streamed_attempt',attempt), \
             patch.object(adapter.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            result=await adapter.post_json(self.connection(timeout),'https://example.test/chat',headers={},body={})
            return result,sleep.await_count

    async def test_ninth_failure_then_tenth_success(self):
        response=httpx.Response(200,json={'ok':True})
        attempt=AsyncMock(side_effect=[httpx.ConnectTimeout('unavailable') for _ in range(9)]+[response])
        result,sleeps=await self.invoke(attempt)
        self.assertEqual(attempt.await_count,10)
        self.assertEqual(result.extensions['model_transport']['attempts'],10)
        self.assertEqual(sleeps,9)

    async def test_connection_failure_stops_at_ten(self):
        attempt=AsyncMock(side_effect=httpx.ConnectTimeout('unavailable'))
        with self.assertRaises(httpx.ConnectTimeout) as caught:
            await self.invoke(attempt)
        self.assertEqual(attempt.await_count,10)
        self.assertEqual(caught.exception.diagnostics['attempts'],10)

    async def test_retryable_status_does_not_sleep_after_last_attempt(self):
        attempt=AsyncMock(return_value=None)
        with patch.object(adapter,'_streamed_attempt',attempt), patch.object(adapter.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            with self.assertRaises(httpx.ReadTimeout):
                await adapter.post_json(self.connection(),'https://example.test/chat',headers={},body={})
            self.assertEqual(attempt.await_count,10)
            self.assertEqual(sleep.await_count,9)

    async def test_partial_generation_is_not_replayed(self):
        async def fail(*args,**kwargs):
            kwargs['stats']['progress_events']=1
            raise httpx.ReadError('stream lost')
        attempt=AsyncMock(side_effect=fail)
        with self.assertRaises(httpx.ReadError):await self.invoke(attempt)
        self.assertEqual(attempt.await_count,1)

    async def test_authentication_error_is_not_retried(self):
        request=httpx.Request('POST','https://example.test/chat')
        error=httpx.HTTPStatusError('unauthorized',request=request,response=httpx.Response(401,request=request))
        attempt=AsyncMock(side_effect=error)
        with self.assertRaises(httpx.HTTPStatusError):await self.invoke(attempt)
        self.assertEqual(attempt.await_count,1)

    async def test_cancellation_stops_retries(self):
        attempt=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):await self.invoke(attempt)
        self.assertEqual(attempt.await_count,1)

    async def test_overall_deadline_still_applies(self):
        async def hang(*args,**kwargs):await asyncio.Event().wait()
        attempt=AsyncMock(side_effect=hang)
        with self.assertRaises(httpx.ReadTimeout):await self.invoke(attempt,timeout=.2)
        self.assertEqual(attempt.await_count,1)

if __name__=='__main__':unittest.main(verbosity=2)
