import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)


class RPCError(RuntimeError):
    pass


class RPC:
    def __init__(self, session, url, requests_per_second=5, timeout=8):
        self.session, self.url = session, url
        self.interval = 1 / max(float(requests_per_second), 0.1)
        self.timeout, self.next_request, self.counter = timeout, 0.0, 0
        self.status = 'not_checked'

    async def call(self, method, params=None, retries=2):
        for attempt in range(retries + 1):
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval
            self.counter += 1
            try:
                async with self.session.post(
                        self.url, json={'jsonrpc': '2.0', 'id': self.counter,
                                        'method': method, 'params': params or []},
                        timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    body = await response.json(content_type=None)
                    if response.status != 200 or body.get('error'):
                        raise RPCError(f'{method}_FAILED')
                    self.status = 'ok'
                    return body['result']
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
        return await self.call('eth_getTransactionReceipt', [tx_hash])

    async def transaction_count(self, address, block='pending'):
        return int(await self.call('eth_getTransactionCount', [address, block]), 16)

    async def send_raw_transaction(self, raw):
        return await self.call('eth_sendRawTransaction', [raw], retries=0)

    async def wait_receipt(self, tx_hash, receipt_timeout=120, poll=1):
        deadline = time.monotonic() + receipt_timeout
        while time.monotonic() < deadline:
            receipt = await self.receipt(tx_hash)
            if receipt:
                return receipt
            await asyncio.sleep(poll)
        raise TimeoutError('RECEIPT_TIMEOUT')
