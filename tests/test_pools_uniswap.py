from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_abi import encode

from trader.execution import ExecutionFailure
from trader.models import TradeSignal
from trader.pools import PoolResolver, selector
from trader.uniswap import (
    TRANSFER_TOPIC,
    UniswapRobinhoodExecutionAdapter,
    address_topic,
    raw_amount,
)

V2_FACTORY = '0x' + '1' * 40
V3_FACTORY = '0x' + '2' * 40
V2_ROUTER = '0x' + '3' * 40
TOKEN = '0x' + '4' * 40
INPUT = '0x' + '5' * 40
POOL = '0x' + '6' * 40
WALLET = '0x' + '7' * 40


def encoded(types, values):
    return '0x' + encode(types, values).hex()


def contracts():
    return {'v2_factory': V2_FACTORY, 'v2_router': V2_ROUTER, 'v3_factory': V3_FACTORY,
            'v3_router': '0x' + '8' * 40, 'v3_quoter': '0x' + '9' * 40,
            'v4_pool_manager': '0x' + 'a' * 40, 'v4_state_view': '0x' + 'b' * 40,
            'universal_router': '0x' + 'c' * 40, 'weth': INPUT}


class FakeRPC:
    def __init__(self, factory=V2_FACTORY):
        self.factory = factory
        self.code_calls = []

    async def get_code(self, address):
        self.code_calls.append(address)
        return '0x1234'

    async def eth_call(self, address, data):
        method = data[:10]
        if method == '0x' + selector('factory()').hex():
            return encoded(['address'], [self.factory])
        if method == '0x' + selector('token0()').hex():
            return encoded(['address'], [TOKEN])
        if method == '0x' + selector('token1()').hex():
            return encoded(['address'], [INPUT])
        if method == '0x' + selector('getReserves()').hex():
            return encoded(['uint112', 'uint112', 'uint32'], [100, 200, 1])
        if method == '0x' + selector('fee()').hex():
            return encoded(['uint24'], [3000])
        if method == '0x' + selector('tickSpacing()').hex():
            return encoded(['int24'], [60])
        raise AssertionError(method)


def signal(pool=POOL):
    payload = {'version': 'trade_signal_v1', 'signal_id': 's', 'event_id': 'e', 'chain_id': 4663,
               'token_address': TOKEN, 'expires_at': 1000, 'eligibility': {'eligible': True},
               'market_snapshot': {'pool_address': pool}}
    return TradeSignal.parse(payload, now=1)


async def test_v2_pool_is_verified_by_factory_tokens_and_reserves():
    result = await PoolResolver(FakeRPC(), contracts(), INPUT).resolve_pool(signal())
    assert result['version'] == 'v2'
    assert result['router'] == V2_ROUTER
    assert result['reserves'] == (100, 200)


async def test_v3_pool_reads_fee_and_tick_spacing():
    result = await PoolResolver(FakeRPC(V3_FACTORY), contracts(), INPUT).resolve_pool(signal())
    assert result['version'] == 'v3'
    assert result['fee'] == 3000 and result['tick_spacing'] == 60


async def test_unverified_factory_is_rejected():
    with pytest.raises(ExecutionFailure, match='UNVERIFIED_POOL_FACTORY'):
        await PoolResolver(FakeRPC('0x' + 'f' * 40), contracts(), INPUT).resolve_pool(signal())


async def test_v4_pool_id_is_never_called_as_a_contract():
    rpc = FakeRPC()
    with pytest.raises(ExecutionFailure, match='V4_POOL_KEY_REQUIRED'):
        await PoolResolver(rpc, contracts(), INPUT).resolve_pool(signal('0x' + 'd' * 64))
    assert rpc.code_calls == []


@pytest.mark.parametrize(('amount', 'decimals', 'expected'), [('6', 6, 6_000_000), ('0.002', 18, 2 * 10**15)])
def test_raw_amount_is_exact(amount, decimals, expected):
    assert raw_amount(amount, decimals) == expected


def test_raw_amount_never_rounds_or_allows_zero():
    with pytest.raises(ExecutionFailure, match='INVALID_ASSET_AMOUNT'):
        raw_amount('0.0000001', 6)
    with pytest.raises(ExecutionFailure, match='INVALID_ASSET_AMOUNT'):
        raw_amount('0', 18)


def adapter(db, mode='ETH'):
    settings = SimpleNamespace(config={'contracts': contracts()}, amount_mode=mode,
                               buy_asset_address=INPUT, buy_asset_decimals=6, wallet_address=WALLET,
                               deadline_seconds=60)
    return UniswapRobinhoodExecutionAdapter(db, AsyncMock(), AsyncMock(), settings)


def test_receipt_parser_uses_only_target_token_transfers_to_wallet(db):
    receipt = {'logs': [
        {'address': TOKEN, 'topics': [TRANSFER_TOPIC, address_topic(POOL), address_topic(WALLET)],
         'data': hex(123)},
        {'address': TOKEN, 'topics': [TRANSFER_TOPIC, address_topic(POOL), address_topic(INPUT)],
         'data': hex(999)},
    ]}
    assert adapter(db).parse_actual_token_received(receipt, TOKEN, WALLET) == Decimal(123)


async def test_v3_execution_fails_closed_until_fork_validated(db):
    instance = adapter(db)
    with pytest.raises(ExecutionFailure, match='V3_EXECUTION_NOT_FORK_VALIDATED'):
        await instance.quote_buy(signal(), {'version': 'v3'}, Decimal('1'))
