from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

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
ZERO_ADDRESS = '0x' + '0' * 40


def encoded(types, values):
    return '0x' + encode(types, values).hex()


def contracts():
    return {'v2_factory': V2_FACTORY, 'v2_router': V2_ROUTER, 'v3_factory': V3_FACTORY,
            'v3_router': '0x' + '8' * 40, 'v3_quoter': '0x' + '9' * 40,
            'v4_pool_manager': '0x' + 'a' * 40, 'v4_state_view': '0x' + 'b' * 40,
            'universal_router': '0x' + 'c' * 40, 'v4_quoter': '0x' + 'd' * 40,
            'permit2': '0x' + 'e' * 40, 'weth': INPUT}


def pool_id(key):
    packed = encode(['address', 'address', 'uint24', 'int24', 'address'],
                    [key['currency0'], key['currency1'], key['fee'],
                     key['tick_spacing'], key['hooks']])
    return '0x' + keccak(packed).hex()


def initialize_log(key):
    return {
        'topics': [
            '0x' + keccak(text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)').hex(),
            pool_id(key),
            address_topic(key['currency0']),
            address_topic(key['currency1']),
        ],
        'data': encoded(['uint24', 'int24', 'address', 'uint160', 'int24'],
                        [key['fee'], key['tick_spacing'], key['hooks'], 1, 0]),
    }


class FakeRPC:
    def __init__(self, factory=V2_FACTORY, token1=INPUT, logs=None):
        self.factory = factory
        self.token1 = token1
        self.code_calls = []
        self.logs = logs or []

    async def call(self, method, params=None):
        if method == 'eth_getLogs':
            return self.logs
        raise AssertionError(method)

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
            return encoded(['address'], [self.token1])
        if method == '0x' + selector('getPair(address,address)').hex():
            return encoded(['address'], ['0x' + 'e' * 40])
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


async def test_v2_pool_can_use_verified_single_bridge_route():
    other = '0x' + 'd' * 40
    result = await PoolResolver(FakeRPC(token1=other), contracts(), INPUT).resolve_pool(signal())
    assert result['path_buy'] == [INPUT, other, TOKEN]
    assert result['path_sell'] == [TOKEN, other, INPUT]
    assert result['bridge_pair'] == '0x' + 'e' * 40


async def test_unverified_factory_is_rejected():
    with pytest.raises(ExecutionFailure, match='UNVERIFIED_POOL_FACTORY'):
        await PoolResolver(FakeRPC('0x' + 'f' * 40), contracts(), INPUT).resolve_pool(signal())


async def test_v4_pool_key_is_discovered_without_calling_pool_id_as_contract():
    key = {'currency0': TOKEN, 'currency1': INPUT, 'fee': 3000,
           'tick_spacing': 60, 'hooks': ZERO_ADDRESS}
    identifier = pool_id(key)
    rpc = FakeRPC(logs=[initialize_log(key)])
    result = await PoolResolver(rpc, contracts(), INPUT).resolve_pool(signal(identifier))
    assert result['version'] == 'v4'
    assert result['pool_key'] == key
    assert identifier not in rpc.code_calls


async def test_v4_pool_key_discovery_fails_closed_when_initialize_log_is_missing():
    with pytest.raises(ExecutionFailure, match='V4_POOL_KEY_NOT_FOUND'):
        await PoolResolver(FakeRPC(), contracts(), INPUT).resolve_pool(signal('0x' + 'd' * 64))


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


def v4_pool(mode='ETH'):
    input_currency = ZERO_ADDRESS if mode == 'ETH' else INPUT
    currency0, currency1 = sorted((input_currency, TOKEN))
    key = {'currency0': currency0, 'currency1': currency1, 'fee': 3000,
           'tick_spacing': 60, 'hooks': ZERO_ADDRESS}
    return {'version': 'v4', 'pool_id': pool_id(key), 'pool_key': key,
            'pool_manager': contracts()['v4_pool_manager'],
            'quoter': contracts()['v4_quoter'], 'router': contracts()['universal_router']}


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


async def test_v4_buy_quote_uses_exact_input_single(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(return_value=encoded(['uint256', 'uint256'], [123, 456]))
    result = await instance.quote_buy(signal(), v4_pool(), Decimal('0.002'))
    assert result == Decimal(123)
    address, data = instance.rpc.eth_call.await_args.args
    assert address == contracts()['v4_quoter']
    assert data.startswith('0x' + selector(
        'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))').hex())


async def test_v4_native_buy_builds_universal_router_v211_plan(db):
    instance = adapter(db)
    instance._base_transaction = AsyncMock(return_value={'nonce': 1})
    pool = v4_pool()
    tx = await instance.build_buy_transaction(signal(), pool, Decimal('0.002'), Decimal(123))
    assert tx['_side'] == 'BUY'
    to, data, value = instance._base_transaction.await_args.args
    assert to == contracts()['universal_router'] and value == 2 * 10**15
    assert data.startswith('0x' + selector('execute(bytes,bytes[],uint256)').hex())
    commands, inputs, _ = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(data[10:]))
    actions, params = decode(['bytes', 'bytes[]'], inputs[0])
    assert commands == b'\x10' and actions == b'\x06\x0c\x0f'
    swap = decode(['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
                  params[0])[0]
    assert swap[1:] == (True, 2 * 10**15, 123, 0, b'')
    assert decode(['address', 'uint256'], params[1]) == (ZERO_ADDRESS, 2 ** 256 - 1)
    assert decode(['address', 'uint256'], params[2]) == (TOKEN, 123)


async def test_v4_usd_buy_uses_permit2_approval_and_zero_value(db):
    instance = adapter(db, mode='USD')
    instance._ensure_v4_approval = AsyncMock()
    instance._base_transaction = AsyncMock(return_value={'nonce': 1})
    pool = v4_pool('USD')
    await instance.build_buy_transaction(signal(), pool, Decimal('6'), Decimal(123))
    instance._ensure_v4_approval.assert_awaited_once_with('e', INPUT, 6_000_000)
    assert instance._base_transaction.await_args.args[2] == 0


async def test_v4_sell_builds_reverse_swap_and_uses_permit2(db):
    instance = adapter(db)
    pool = v4_pool()
    position = {'event_id': 'e', 'token_address': TOKEN, 'token_quantity': '42'}
    instance.pools['e'] = pool
    instance._ensure_v4_approval = AsyncMock()
    instance._base_transaction = AsyncMock(return_value={'nonce': 2})
    await instance.ensure_token_approval(position)
    instance._ensure_v4_approval.assert_awaited_once_with('e', TOKEN, 42)
    tx = await instance.build_sell_transaction(position, Decimal('0.000001'))
    assert tx['_side'] == 'SELL'
    _, data, value = instance._base_transaction.await_args.args
    commands, inputs, _ = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(data[10:]))
    _, params = decode(['bytes', 'bytes[]'], inputs[0])
    swap = decode(['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
                  params[0])[0]
    assert commands == b'\x10' and value == 0
    assert swap[1:] == (False, 42, 10**12, 0, b'')


async def test_v4_approval_covers_erc20_and_permit2_layers(db):
    instance = adapter(db)
    instance._allowance = AsyncMock(return_value=0)
    instance._approve_exact = AsyncMock()
    instance._permit2_allowance = AsyncMock(return_value=(0, 0, 0))
    instance._approve_permit2 = AsyncMock()
    await instance._ensure_v4_approval('e', TOKEN, 42)
    instance._approve_exact.assert_awaited_once_with('e', TOKEN, contracts()['permit2'], 42)
    instance._approve_permit2.assert_awaited_once_with('e', TOKEN, 42)


async def test_permit2_allowance_uses_owner_token_and_router(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(
        return_value=encoded(['uint160', 'uint48', 'uint48'], [42, 1234, 9]))
    assert await instance._permit2_allowance(TOKEN) == (42, 1234, 9)
    address, data = instance.rpc.eth_call.await_args.args
    assert address == contracts()['permit2']
    assert data.startswith('0x' + selector('allowance(address,address,address)').hex())
    owner, token, router = decode(['address', 'address', 'address'], bytes.fromhex(data[10:]))
    assert (owner, token, router) == (WALLET, TOKEN, contracts()['universal_router'])


async def test_usd_buy_approves_only_exact_input_amount(db):
    instance = adapter(db, mode='USD')
    instance._allowance = AsyncMock(return_value=0)
    instance._approve_exact = AsyncMock()
    instance._base_transaction = AsyncMock(return_value={'nonce': 1})
    await instance.build_buy_transaction(signal(), {'version': 'v2', 'router': V2_ROUTER,
                                                    'path_buy': [INPUT, TOKEN]},
                                         Decimal('6'), Decimal('123'))
    instance._approve_exact.assert_awaited_once_with('e', INPUT, V2_ROUTER, 6_000_000)


async def test_v4_native_sell_proceeds_are_read_from_pool_manager_swap(db):
    instance = adapter(db)
    pool = v4_pool()
    instance.pools['e'] = pool
    position = {'event_id': 'e', 'token_address': TOKEN, 'token_quantity': '42'}
    swap_topic = '0x' + keccak(
        text='Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)').hex()
    receipt = {'logs': [{
        'address': pool['pool_manager'],
        'topics': [swap_topic, pool['pool_id'], address_topic(WALLET)],
        'data': encoded(['int128', 'int128', 'uint160', 'uint128', 'int24', 'uint24'],
                        [-10**15, 42, 1, 1, 0, 3000]),
    }]}
    assert await instance.parse_actual_sell_proceeds(receipt, position) == Decimal('0.001')
