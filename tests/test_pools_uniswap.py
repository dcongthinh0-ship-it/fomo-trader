import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from trader.execution import ExecutionFailure
from trader.models import TradeSignal
from trader.pools import PoolResolver, selector
from trader.rpc import RPCError, RPCResponseError
from trader.uniswap import (
    TRANSFER_TOPIC,
    WETH_WITHDRAWAL_TOPIC,
    UniswapRobinhoodExecutionAdapter,
    address_topic,
    raw_amount,
    rpc_error_facts,
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
            'permit2': '0x' + 'e' * 40, 'multicall3': '0x' + 'f' * 40, 'weth': INPUT}


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
        self.log_calls = 0

    async def call(self, method, params=None):
        if method == 'eth_getLogs':
            self.log_calls += 1
            topics = (params or [{}])[0].get('topics') or []
            result = self.logs
            if len(topics) > 1 and topics[1]:
                result = [item for item in result if item['topics'][1].lower() == topics[1].lower()]
            if len(topics) > 3 and topics[2] and topics[3]:
                result = [item for item in result if item['topics'][2].lower() == topics[2].lower()
                          and item['topics'][3].lower() == topics[3].lower()]
            return result
        raise AssertionError(method)

    async def get_code(self, address):
        self.code_calls.append(address)
        return '0x1234'

    async def eth_call(self, address, data):
        method = data[:10]
        if method == '0x' + selector('tryAggregate(bool,(address,bytes)[])').hex():
            _, calls = decode(['bool', '(address,bytes)[]'], bytes.fromhex(data[10:]))
            rows = [(True, encode(['uint128'], [index + 1]))
                    for index, _ in enumerate(calls)]
            return encoded(['(bool,bytes)[]'], [rows])
        if method == '0x' + selector('factory()').hex():
            return encoded(['address'], [self.factory])
        if method == '0x' + selector('token0()').hex():
            return encoded(['address'], [TOKEN])
        if method == '0x' + selector('token1()').hex():
            return encoded(['address'], [self.token1])
        if method == '0x' + selector('getPair(address,address)').hex():
            return encoded(['address'], ['0x' + 'e' * 40])
        if method == '0x' + selector('getPool(address,address,uint24)').hex():
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


async def test_v3_pool_discovers_verified_single_bridge_fee_routes():
    other = '0x' + 'd' * 40
    result = await PoolResolver(FakeRPC(V3_FACTORY, token1=other), contracts(), INPUT).resolve_pool(signal())
    assert result['version'] == 'v3'
    assert result['route_candidates']
    route = result['route_candidates'][0]
    assert route['path_buy'] == [INPUT, other, TOKEN]
    assert route['path_sell'] == [TOKEN, other, INPUT]
    assert route['fees_buy'][-1] == 3000


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


async def test_v4_immutable_key_and_contract_code_checks_are_cached():
    key = {'currency0': TOKEN, 'currency1': INPUT, 'fee': 3000,
           'tick_spacing': 60, 'hooks': ZERO_ADDRESS}
    rpc = FakeRPC(logs=[initialize_log(key)])
    resolver = PoolResolver(rpc, contracts(), INPUT)

    await resolver.resolve_pool(signal(pool_id(key)))
    first_code_calls = len(rpc.code_calls)
    await resolver.resolve_pool(signal(pool_id(key)))

    assert rpc.log_calls == 1
    assert len(rpc.code_calls) == first_code_calls


async def test_v4_pool_discovers_native_single_bridge_route():
    quote = '0x' + '3' * 40
    target_key = {'currency0': TOKEN, 'currency1': quote, 'fee': 3000,
                  'tick_spacing': 60, 'hooks': ZERO_ADDRESS}
    bridge_key = {'currency0': ZERO_ADDRESS, 'currency1': quote, 'fee': 500,
                  'tick_spacing': 10, 'hooks': ZERO_ADDRESS}
    rpc = FakeRPC(logs=[initialize_log(target_key), initialize_log(bridge_key)])

    result = await PoolResolver(rpc, contracts(), INPUT, ZERO_ADDRESS).resolve_pool(signal(pool_id(target_key)))

    assert result['version'] == 'v4'
    assert len(result['route_candidates']) == 1
    assert result['route_candidates'][0]['keys'] == [bridge_key, target_key]


async def test_v4_bridge_discovery_keeps_only_eight_highest_liquidity_keys():
    quote = '0x' + '3' * 40
    target_key = {'currency0': TOKEN, 'currency1': quote, 'fee': 3000,
                  'tick_spacing': 60, 'hooks': ZERO_ADDRESS}
    bridges = [
        {'currency0': ZERO_ADDRESS, 'currency1': quote, 'fee': 100 + index,
         'tick_spacing': 1, 'hooks': ZERO_ADDRESS}
        for index in range(10)
    ]
    rpc = FakeRPC(logs=[initialize_log(target_key), *(initialize_log(key) for key in bridges)])

    result = await PoolResolver(rpc, contracts(), INPUT, ZERO_ADDRESS).resolve_pool(
        signal(pool_id(target_key)))

    selected = [candidate['keys'][0] for candidate in result['route_candidates']]
    assert len(selected) == 8
    assert selected == list(reversed(bridges[-8:]))


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
                               deadline_seconds=60, max_fee_multiplier=Decimal('2'))
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


def v3_pool():
    return {'version': 'v3', 'address': POOL, 'router': contracts()['v3_router'],
            'quoter': contracts()['v3_quoter'], 'path_buy': [INPUT, TOKEN],
            'path_sell': [TOKEN, INPUT], 'fees_buy': [3000], 'fees_sell': [3000]}


async def test_v3_buy_quote_uses_quoter_v2_exact_input(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(return_value=encoded(
        ['uint256', 'uint160[]', 'uint32[]', 'uint256'], [123, [1], [2], 456]))

    assert await instance.quote_buy(signal(), v3_pool(), Decimal('0.002')) == Decimal(123)
    address, data = instance.rpc.eth_call.await_args.args
    assert address == contracts()['v3_quoter']
    assert data.startswith('0x' + selector('quoteExactInput(bytes,uint256)').hex())


async def test_v3_transient_quote_error_is_not_converted_to_zero_quote(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(side_effect=RPCError('eth_call_UNAVAILABLE'))

    with pytest.raises(RPCError, match='eth_call_UNAVAILABLE'):
        await instance.quote_buy(signal(), v3_pool(), Decimal('0.002'))


async def test_v3_retryable_json_rpc_error_is_not_converted_to_zero_quote(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(side_effect=RPCResponseError(
        'eth_call', -32000, 'upstream unavailable', retryable=True))

    with pytest.raises(RPCResponseError) as raised:
        await instance.quote_buy(signal(), v3_pool(), Decimal('0.002'))

    assert raised.value.retryable is True


async def test_v3_deterministic_revert_with_no_candidate_is_zero_quote(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(side_effect=RPCResponseError(
        'eth_call', 3, 'execution reverted'))

    with pytest.raises(ExecutionFailure, match='ZERO_QUOTE'):
        await instance.quote_buy(signal(), v3_pool(), Decimal('0.002'))


async def test_v3_native_buy_builds_deadlined_swaprouter02_multicall(db):
    instance = adapter(db)
    instance._base_transaction = AsyncMock(return_value={'nonce': 1})

    tx = await instance.build_buy_transaction(signal(), v3_pool(), Decimal('0.002'), Decimal(123))

    assert tx['_side'] == 'BUY'
    to, data, value = instance._base_transaction.await_args.args
    assert to == contracts()['v3_router'] and value == 2 * 10**15
    assert data.startswith('0x' + selector('multicall(uint256,bytes[])').hex())
    _, calls = decode(['uint256', 'bytes[]'], bytes.fromhex(data[10:]))
    assert len(calls) == 1
    assert calls[0].startswith(selector('exactInput((bytes,address,uint256,uint256))'))


async def test_nonce_is_not_reserved_when_gas_estimation_fails(db):
    instance = adapter(db)
    instance.rpc.call = AsyncMock(side_effect=['0x1', RPCError('eth_estimateGas_UNAVAILABLE')])
    instance.nonce.reserve = AsyncMock()

    with pytest.raises(RPCError, match='eth_estimateGas_UNAVAILABLE'):
        await instance._base_transaction(POOL, '0x1234')

    instance.nonce.reserve.assert_not_awaited()


async def test_gas_estimate_uses_type2_hex_quantities_with_fee_headroom(db):
    instance = adapter(db)
    instance.rpc.call = AsyncMock(side_effect=['0x64', '0x5208'])
    instance.nonce.reserve = AsyncMock(return_value=7)
    lowercase_router = '0xabcdefabcdefabcdefabcdefabcdefabcdefabcd'

    transaction = await instance._base_transaction(lowercase_router, '0x1234', value=42)

    estimate_call = instance.rpc.call.await_args_list[1]
    assert estimate_call.args[0] == 'eth_estimateGas'
    assert estimate_call.args[1][0] == {
        'to': to_checksum_address(lowercase_router),
        'data': '0x1234',
        'value': '0x2a',
        'chainId': '0x1237',
        'type': '0x2',
        'maxFeePerGas': '0xc8',
        'maxPriorityFeePerGas': '0x0',
        'from': WALLET,
    }
    assert transaction == {
        'to': to_checksum_address(lowercase_router),
        'data': '0x1234',
        'value': 42,
        'chainId': 4663,
        'type': 2,
        'maxFeePerGas': 200,
        'maxPriorityFeePerGas': 0,
        'gas': 25200,
        'nonce': 7,
    }


async def test_base_transaction_is_accepted_by_eth_account_signer(db):
    instance = adapter(db)
    instance.rpc.call = AsyncMock(side_effect=['0x64', '0x5208'])
    instance.nonce.reserve = AsyncMock(return_value=7)
    transaction = await instance._base_transaction(
        '0xabcdefabcdefabcdefabcdefabcdefabcdefabcd', '0x1234', value=42)

    signed = Account.sign_transaction(transaction, '0x' + '1' * 64)

    assert signed.hash


async def test_signing_failure_reconciles_reserved_nonce_before_retry(db):
    instance = adapter(db)
    instance.settings.private_key = lambda: 'not-a-private-key'
    instance.nonce.reconcile = AsyncMock(return_value=0)

    with pytest.raises(ValueError):
        await instance.submit_buy({'to': to_checksum_address(POOL), 'data': '0x1234',
                                   'value': 0, 'chainId': 4663, 'type': 2,
                                   'maxFeePerGas': 2, 'maxPriorityFeePerGas': 0,
                                   'gas': 21000, 'nonce': 3})

    instance.nonce.reconcile.assert_awaited_once_with()


async def test_submitted_transaction_hash_is_stored_with_json_rpc_prefix(db):
    instance = adapter(db)
    instance.settings.private_key = lambda: '0x' + '1' * 64

    async def accept_raw(raw):
        return '0x' + keccak(hexstr=raw).hex()

    instance.rpc.send_raw_transaction = AsyncMock(side_effect=accept_raw)
    result = await instance.submit_buy({
        'to': to_checksum_address(POOL), 'data': '0x1234', 'value': 0,
        'chainId': 4663, 'type': 2, 'maxFeePerGas': 2,
        'maxPriorityFeePerGas': 0, 'gas': 21000, 'nonce': 0,
    })

    assert result['tx_hash'].startswith('0x') and len(result['tx_hash']) == 66


async def test_submission_error_keeps_sanitized_rpc_reason_for_diagnosis(db):
    instance = adapter(db)
    instance.settings.private_key = lambda: '0x' + '1' * 64
    instance.rpc.send_raw_transaction = AsyncMock(side_effect=RPCResponseError(
        'eth_sendRawTransaction', -32000,
        'max fee per gas less than block base fee raw=0x' + 'a' * 200))
    instance.rpc.receipt = AsyncMock(return_value=None)

    with pytest.raises(ExecutionFailure, match='SUBMISSION_UNKNOWN'):
        await instance.submit_buy({
            'to': to_checksum_address(POOL), 'data': '0x1234', 'value': 0,
            'chainId': 4663, 'type': 2, 'maxFeePerGas': 2,
            'maxPriorityFeePerGas': 0, 'gas': 21000, 'nonce': 0,
            '_event_id': 'e', '_side': 'BUY',
        })

    unknown = db.conn.execute(
        "SELECT response_facts FROM execution_attempts WHERE status='UNKNOWN'").fetchone()
    facts = json.loads(unknown['response_facts'])
    assert facts == {'code': -32000, 'message':
                     'max fee per gas less than block base fee raw=[hex]',
                     'method': 'eth_sendRawTransaction', 'type': 'RPCResponseError'}


def test_rpc_error_facts_never_keeps_long_hex_payload():
    facts = json.loads(rpc_error_facts(RPCResponseError(
        'eth_sendRawTransaction', -32000, 'bad 0x' + 'b' * 128)))
    assert facts['message'] == 'bad [hex]'


async def test_v3_native_sell_approves_swaps_and_unwraps_weth(db):
    instance = adapter(db)
    pool = v3_pool()
    position = {'event_id': 'e', 'token_address': TOKEN, 'token_quantity': '42'}
    instance.pools['e'] = pool
    instance._allowance = AsyncMock(return_value=0)
    instance._approve_exact = AsyncMock()
    instance._base_transaction = AsyncMock(return_value={'nonce': 2})

    await instance.ensure_token_approval(position)
    tx = await instance.build_sell_transaction(position, Decimal('0.000001'))

    instance._approve_exact.assert_awaited_once_with('e', TOKEN, contracts()['v3_router'], 42)
    assert tx['_side'] == 'SELL'
    _, data, value = instance._base_transaction.await_args.args
    assert value == 0
    _, calls = decode(['uint256', 'bytes[]'], bytes.fromhex(data[10:]))
    assert len(calls) == 2
    assert calls[0].startswith(selector('exactInput((bytes,address,uint256,uint256))'))
    assert calls[1].startswith(selector('unwrapWETH9(uint256,address)'))


async def test_v3_native_sell_proceeds_use_router_weth_withdrawal(db):
    instance = adapter(db)
    pool = v3_pool()
    instance.pools['e'] = pool
    position = {'event_id': 'e', 'token_address': TOKEN, 'token_quantity': '42'}
    receipt = {'logs': [{
        'address': INPUT,
        'topics': [WETH_WITHDRAWAL_TOPIC, address_topic(contracts()['v3_router'])],
        'data': hex(10**15),
    }]}

    assert await instance.parse_actual_sell_proceeds(receipt, position) == Decimal('0.001')


async def test_token_balance_reads_raw_wallet_balance(db):
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(return_value=encoded(['uint256'], [123]))

    assert await instance.token_balance(TOKEN) == Decimal(123)
    address, data = instance.rpc.eth_call.await_args.args
    assert address == TOKEN
    assert data.startswith('0x' + selector('balanceOf(address)').hex())
    assert decode(['address'], bytes.fromhex(data[10:]))[0] == WALLET


async def test_v4_buy_quote_uses_exact_input_single(db):
    instance = adapter(db)
    instance.rpc.call = AsyncMock(return_value='0x')
    instance.rpc.eth_call = AsyncMock(return_value=encoded(['uint256', 'uint256'], [123, 456]))
    result = await instance.quote_buy(signal(), v4_pool(), Decimal('0.002'))
    assert result == Decimal(123)
    request = instance.rpc.call.await_args.args[1][0]
    assert instance.rpc.call.await_args.args[0] == 'eth_call'
    assert request['from'] == WALLET and request['to'] == TOKEN
    assert request['data'].startswith('0x' + selector('approve(address,uint256)').hex())
    address, data = instance.rpc.eth_call.await_args.args
    assert address == contracts()['v4_quoter']
    assert data.startswith('0x' + selector(
        'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))').hex())


async def test_v4_multihop_quote_and_calldata_use_exact_input_path(db):
    quote = '0x' + '3' * 40
    bridge_key = {'currency0': ZERO_ADDRESS, 'currency1': quote, 'fee': 500,
                  'tick_spacing': 10, 'hooks': ZERO_ADDRESS}
    target_key = {'currency0': TOKEN, 'currency1': quote, 'fee': 3000,
                  'tick_spacing': 60, 'hooks': ZERO_ADDRESS}
    pool = v4_pool()
    pool['pool_key'] = target_key
    pool['pool_id'] = pool_id(target_key)
    pool['route_candidates'] = [{'keys': [bridge_key, target_key]}]
    instance = adapter(db)
    instance.rpc.call = AsyncMock(return_value='0x')
    instance.rpc.eth_call = AsyncMock(return_value=encoded(['uint256', 'uint256'], [123, 456]))
    instance._base_transaction = AsyncMock(return_value={'nonce': 1})

    assert await instance.quote_buy(signal(), pool, Decimal('0.002')) == Decimal(123)
    _, quote_data = instance.rpc.eth_call.await_args.args
    assert quote_data.startswith('0x' + selector(
        'quoteExactInput((address,(address,uint24,int24,address,bytes)[],uint128))').hex())

    await instance.build_buy_transaction(signal(), pool, Decimal('0.002'), Decimal(120))
    _, data, _ = instance._base_transaction.await_args.args
    commands, inputs, _ = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(data[10:]))
    actions, params = decode(['bytes', 'bytes[]'], inputs[0])
    assert commands == b'\x10' and actions == b'\x07\x0c\x0f'
    route = decode(
        ['(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)'], params[0])[0]
    assert route[0] == ZERO_ADDRESS
    assert [hop[0] for hop in route[1]] == [quote, TOKEN]
    assert route[3:] == (2 * 10**15, 120)


async def test_v4_multihop_transient_quote_error_is_not_zero_quote(db):
    pool = v4_pool()
    pool['route_candidates'] = [{'keys': [pool['pool_key']]}]
    instance = adapter(db)
    instance.rpc.eth_call = AsyncMock(side_effect=RPCError('eth_call_UNAVAILABLE'))

    with pytest.raises(RPCError, match='eth_call_UNAVAILABLE'):
        await instance._quote_v4(42, pool, ZERO_ADDRESS)


async def test_v4_buy_is_rejected_before_order_when_token_blocks_permit2(db):
    instance = adapter(db)
    instance.rpc.call = AsyncMock(
        side_effect=RPCResponseError('eth_call', 3, 'execution reverted'))
    instance.rpc.eth_call = AsyncMock(return_value=encoded(['uint256', 'uint256'], [123, 456]))

    with pytest.raises(ExecutionFailure, match='V4_TOKEN_PERMIT2_UNSUPPORTED'):
        await instance.quote_buy(signal(), v4_pool(), Decimal('0.002'))


async def test_v4_permit2_check_propagates_retryable_rpc_error(db):
    instance = adapter(db)
    instance.rpc.call = AsyncMock(side_effect=RPCResponseError(
        'eth_call', -32000, 'upstream unavailable', retryable=True))
    instance.rpc.eth_call = AsyncMock(return_value=encoded(['uint256', 'uint256'], [123, 456]))

    with pytest.raises(RPCResponseError) as raised:
        await instance.quote_buy(signal(), v4_pool(), Decimal('0.002'))

    assert raised.value.retryable is True


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


async def test_approval_timeout_is_recovered_by_hash_before_any_resubmit(db):
    instance = adapter(db)
    tx_hash = '0x' + 'a' * 64
    instance._base_transaction = AsyncMock(return_value={'nonce': 3})
    instance._submit = AsyncMock(return_value={'tx_hash': tx_hash, 'nonce': 3})
    instance.wait_for_receipt = AsyncMock(side_effect=TimeoutError('RECEIPT_TIMEOUT'))

    with pytest.raises(ExecutionFailure, match='APPROVAL_PENDING'):
        await instance._approve_exact('e', TOKEN, POOL, 42)
    unknown = db.conn.execute(
        "SELECT * FROM execution_attempts WHERE side='APPROVAL' AND status='UNKNOWN'").fetchone()
    assert unknown['tx_hash'] == tx_hash and unknown['nonce'] == 3

    instance._submit.reset_mock()
    instance.receipt_by_hash = AsyncMock(return_value=None)
    with pytest.raises(ExecutionFailure, match='APPROVAL_PENDING'):
        await instance._approve_exact('e', TOKEN, POOL, 42)
    instance._submit.assert_not_awaited()

    instance.receipt_by_hash.return_value = {'status': '0x1', 'transactionHash': tx_hash}
    await instance._approve_exact('e', TOKEN, POOL, 42)
    instance._submit.assert_not_awaited()
    latest = db.conn.execute(
        "SELECT status FROM execution_attempts WHERE side='APPROVAL' "
        'ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
    assert latest['status'] == 'CONFIRMED'


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
