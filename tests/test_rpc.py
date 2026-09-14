import asyncio
import time

import pytest

from trader.rpc import RPC, RPCResponseError


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.starts = []

    def post(self, *_, **__):
        self.starts.append(time.monotonic())
        return self.responses.pop(0)


async def test_concurrent_calls_respect_the_configured_start_rate():
    session = FakeSession([FakeResponse(200, {'result': '0x1'}) for _ in range(3)])
    rpc = RPC(session, 'https://rpc.invalid', requests_per_second=50)

    assert await asyncio.gather(*(rpc.call('eth_chainId') for _ in range(3))) == ['0x1'] * 3

    gaps = [right - left for left, right in zip(
        session.starts[:-1], session.starts[1:], strict=True)]
    assert all(gap >= 0.015 for gap in gaps)


async def test_deterministic_json_rpc_rejection_is_not_retried():
    session = FakeSession([FakeResponse(200, {
        'error': {'code': 3, 'message': 'execution reverted'},
    })])
    rpc = RPC(session, 'https://rpc.invalid', requests_per_second=1000)

    with pytest.raises(RPCResponseError, match='eth_call_REJECTED') as raised:
        await rpc.call('eth_call')

    assert raised.value.code == 3
    assert len(session.starts) == 1


async def test_rate_limit_response_is_retried():
    session = FakeSession([
        FakeResponse(429, {'error': {'code': 429, 'message': 'Too Many Requests'}}),
        FakeResponse(200, {'result': '0x123'}),
    ])
    rpc = RPC(session, 'https://rpc.invalid', requests_per_second=1000)

    assert await rpc.call('eth_call', retries=1) == '0x123'
    assert len(session.starts) == 2


async def test_generic_upstream_server_error_is_retried_but_revert_is_not():
    transient = FakeSession([
        FakeResponse(200, {'error': {'code': -32000, 'message': 'upstream request failed'}}),
        FakeResponse(200, {'result': '0x123'}),
    ])
    assert await RPC(transient, 'https://rpc.invalid', 1000).call(
        'eth_call', retries=1) == '0x123'

    reverted = FakeSession([
        FakeResponse(200, {'error': {'code': -32000, 'message': 'execution reverted'}}),
    ])
    with pytest.raises(RPCResponseError):
        await RPC(reverted, 'https://rpc.invalid', 1000).call('eth_call', retries=1)
    assert len(reverted.starts) == 1
