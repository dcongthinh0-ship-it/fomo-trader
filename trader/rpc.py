import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)


class RPCError(RuntimeError):
    pass


class RPCResponseError(RPCError):
    def __init__(self, method, code=None, message='', retryable=False):
        super().__init__(f'{method}_REJECTED')
        self.method, self.code, self.message = method, code, str(message)
        self.retryable = bool(retryable)


class RPC:
    def __init__(self, session, url, requests_per_second=5, timeout=8, max_in_flight=2,
                 send_url=None):
        self.session, self.url = session, url
        self.send_url = send_url or url
        self.interval = 1 / max(float(requests_per_second), 0.1)
        self.timeout, self.next_request, self.counter = timeout, 0.0, 0
        self._rate_lock = asyncio.Lock()
        self._in_flight = asyncio.Semaphore(max(1, int(max_in_flight)))
        self.status = 'not_checked'

    async def _throttle(self):
        async with self._rate_lock:
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval

    @staticmethod
    def _retryable_response(status, error):
        message = str((error or {}).get('message', '')).lower()
        code = (error or {}).get('code')
        deterministic = any(phrase in message for phrase in (
            'execution reverted', 'invalid argument', 'method not found',
            'insufficient funds', 'nonce too low', 'replacement transaction underpriced'))
        return not deterministic and (
            status == 429 or status >= 500 or code in (429, -32000, -32005, -32603) or any(
                phrase in message for phrase in (
                    'rate limit', 'too many requests', 'temporarily unavailable', 'timeout')))

    async def call(self, method, params=None, retries=2, endpoint=None):
        for attempt in range(retries + 1):
            await self._throttle()
            self.counter += 1
            try:
                async with self._in_flight:
                    async with self.session.post(
                            endpoint or self.url,
                            json={'jsonrpc': '2.0', 'id': self.counter,
                                            'method': method, 'params': params or []},
                            timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                        body = await response.json(content_type=None)
                        error = body.get('error')
                        if response.status != 200 or error:
                            if not self._retryable_response(response.status, error):
                                self.status = 'degraded'
                                raise RPCResponseError(
                                    method, (error or {}).get('code'), (error or {}).get('message'))
                            if attempt == retries and error:
                                self.status = 'degraded'
                                raise RPCResponseError(
                                    method, error.get('code'), error.get('message'), retryable=True)
                            raise RPCError(f'{method}_FAILED')
                        self.status = 'ok'
                        return body['result']
            except RPCResponseError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, RPCError):
                self.status = 'degraded'
                if attempt == retries:
                    raise RPCError(f'{method}_UNAVAILABLE') from None
                await asyncio.sleep(0.25 * 2 ** attempt)

    async def eth_call(self, to, data, block='latest'):
        return await self.call('eth_call', [{'to': to, 'data': data}, block])

    async def get_code(self, address):
        return await self.call('eth_getCode', [address, 'latest'])

    async def receipt(self, tx_hash):
        normalized = '0x' + str(tx_hash).removeprefix('0x')
        return await self.call('eth_getTransactionReceipt', [normalized])

    async def transaction(self, tx_hash):
        normalized = '0x' + str(tx_hash).removeprefix('0x')
        return await self.call('eth_getTransactionByHash', [normalized])

    async def transaction_count(self, address, block='pending'):
        return int(await self.call('eth_getTransactionCount', [address, block]), 16)

    async def send_raw_transaction(self, raw):
        return await self.call(
            'eth_sendRawTransaction', [raw], retries=0, endpoint=self.send_url)

    async def wait_receipt(self, tx_hash, receipt_timeout=120, poll=1):
        deadline = time.monotonic() + receipt_timeout
        while time.monotonic() < deadline:
            receipt = await self.receipt(tx_hash)
            if receipt:
                return receipt
            await asyncio.sleep(poll)
        raise TimeoutError('RECEIPT_TIMEOUT')
