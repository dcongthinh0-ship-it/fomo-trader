import json
import time
from decimal import Decimal

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak

from .db import dumps
from .execution import ExecutionFailure, SubmissionUnknown
from .models import TradeSignal
from .orders import attempt
from .pools import ZERO_ADDRESS, PoolResolver, calldata, selector
from .rpc import RPCError

TRANSFER_TOPIC = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
SWAP_V2_TOPIC = '0x' + keccak(text='Swap(address,uint256,uint256,uint256,uint256,address)').hex()
SWAP_V4_TOPIC = '0x' + keccak(
    text='Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)').hex()
MAX_UINT128 = 2 ** 128 - 1
MAX_UINT160 = 2 ** 160 - 1
MAX_UINT256 = 2 ** 256 - 1
V4_SWAP_COMMAND = b'\x10'
V4_EXACT_IN_SINGLE = b'\x06'
V4_SETTLE_ALL = b'\x0c'
V4_TAKE_ALL = b'\x0f'


def raw_amount(amount, decimals):
    value = Decimal(str(amount)) * (Decimal(10) ** int(decimals))
    if value != value.to_integral_value() or value <= 0:
        raise ExecutionFailure('INVALID_ASSET_AMOUNT')
    return int(value)


def address_topic(address):
    return '0x' + address.lower().removeprefix('0x').rjust(64, '0')


class UniswapRobinhoodExecutionAdapter:
    """Direct V2 and single-pool V4 execution; V3 remains fail-closed."""
    def __init__(self, db, rpc, nonce, settings):
        self.db, self.rpc, self.nonce, self.settings = db, rpc, nonce, settings
        contracts = settings.config['contracts']
        input_asset = contracts['weth'] if settings.amount_mode == 'ETH' else settings.buy_asset_address
        self.input_asset = input_asset.lower()
        self.v4_input_asset = ZERO_ADDRESS if settings.amount_mode == 'ETH' else self.input_asset
        self.resolver = PoolResolver(rpc, contracts, self.input_asset, self.v4_input_asset)
        self.pools = {}

    async def identify_pool_version(self, hint):
        return await self.resolver.identify_pool_version(hint)

    async def resolve_pool(self, signal):
        pool = await self.resolver.resolve_pool(signal)
        self.pools[signal.event_id] = pool
        return pool

    async def _quote_v2(self, amount_in, path):
        data = await self.rpc.eth_call(self.resolver.contracts['v2_router'],
                                       calldata('getAmountsOut(uint256,address[])',
                                                ['uint256', 'address[]'], [amount_in, path]))
        amounts = decode(['uint256[]'], bytes.fromhex(data[2:]))[0]
        if not amounts or amounts[-1] <= 0:
            raise ExecutionFailure('ZERO_QUOTE')
        return int(amounts[-1])

    @staticmethod
    def _v4_key(pool):
        key = pool['pool_key']
        return (key['currency0'], key['currency1'], int(key['fee']),
                int(key['tick_spacing']), key['hooks'])

    @staticmethod
    def _v4_zero_for_one(pool, currency_in):
        currency_in = currency_in.lower()
        if currency_in == pool['pool_key']['currency0']:
            return True
        if currency_in == pool['pool_key']['currency1']:
            return False
        raise ExecutionFailure('V4_INPUT_ASSET_MISMATCH')

    async def _quote_v4(self, amount_in, pool, currency_in):
        if not 0 < amount_in <= MAX_UINT128:
            raise ExecutionFailure('V4_AMOUNT_OUT_OF_RANGE')
        params = (self._v4_key(pool), self._v4_zero_for_one(pool, currency_in), amount_in, b'')
        data = await self.rpc.eth_call(
            pool['quoter'],
            calldata(
                'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))',
                ['((address,address,uint24,int24,address),bool,uint128,bytes)'],
                [params],
            ),
        )
        amount_out, _ = decode(['uint256', 'uint256'], bytes.fromhex(data[2:]))
        if amount_out <= 0:
            raise ExecutionFailure('ZERO_QUOTE')
        return int(amount_out)

    async def quote_buy(self, signal, pool, amount):
        if pool['version'] == 'v3':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        decimals = 18 if self.settings.amount_mode == 'ETH' else self.settings.buy_asset_decimals
        amount_in = raw_amount(amount, decimals)
        if pool['version'] == 'v4':
            return Decimal(await self._quote_v4(amount_in, pool, self.v4_input_asset))
        return Decimal(await self._quote_v2(amount_in, pool['path_buy']))

    async def _base_transaction(self, to, data, value=0):
        nonce = await self.nonce.reserve()
        tx = {'to': to, 'data': data, 'value': value, 'nonce': nonce,
              'chainId': 4663, 'gasPrice': int(await self.rpc.call('eth_gasPrice'), 16)}
        estimate = await self.rpc.call('eth_estimateGas', [{**tx, 'from': self.settings.wallet_address}])
        tx['gas'] = int(int(estimate, 16) * 1.2)
        return tx

    async def build_buy_transaction(self, signal, pool, amount, minimum):
        if pool['version'] == 'v3':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        deadline, router = int(time.time()) + self.settings.deadline_seconds, pool['router']
        if pool['version'] == 'v4':
            decimals = 18 if self.settings.amount_mode == 'ETH' else self.settings.buy_asset_decimals
            amount_in = raw_amount(amount, decimals)
            if self.settings.amount_mode == 'USD':
                await self._ensure_v4_approval(signal.event_id, self.input_asset, amount_in)
            output_currency = (pool['pool_key']['currency1']
                               if self._v4_zero_for_one(pool, self.v4_input_asset)
                               else pool['pool_key']['currency0'])
            data = self._v4_swap_calldata(pool, self.v4_input_asset, output_currency,
                                          amount_in, int(minimum), deadline)
            tx = await self._base_transaction(
                router, data, amount_in if self.v4_input_asset == ZERO_ADDRESS else 0)
            tx.update(_event_id=signal.event_id, _side='BUY')
            return tx
        if self.settings.amount_mode == 'ETH':
            value = raw_amount(amount, 18)
            data = calldata('swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)',
                            ['uint256', 'address[]', 'address', 'uint256'],
                            [int(minimum), pool['path_buy'],
                             self.settings.wallet_address, deadline])
            tx = await self._base_transaction(router, data, value)
            tx.update(_event_id=signal.event_id, _side='BUY')
            return tx
        value = raw_amount(amount, self.settings.buy_asset_decimals)
        if await self._allowance(self.input_asset, router) < value:
            await self._approve_exact(signal.event_id, self.input_asset, router, value)
        data = calldata(
            'swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)',
            ['uint256', 'uint256', 'address[]', 'address', 'uint256'],
            [value, int(minimum), pool['path_buy'], self.settings.wallet_address, deadline])
        tx = await self._base_transaction(router, data)
        tx.update(_event_id=signal.event_id, _side='BUY')
        return tx

    def _v4_swap_calldata(self, pool, currency_in, currency_out, amount_in, minimum, deadline):
        if not 0 < amount_in <= MAX_UINT128 or not 0 <= minimum <= MAX_UINT128:
            raise ExecutionFailure('V4_AMOUNT_OUT_OF_RANGE')
        swap = (self._v4_key(pool), self._v4_zero_for_one(pool, currency_in),
                amount_in, minimum, 0, b'')
        actions = V4_EXACT_IN_SINGLE + V4_SETTLE_ALL + V4_TAKE_ALL
        params = [
            encode(['((address,address,uint24,int24,address),bool,uint128,uint128,uint256,bytes)'],
                   [swap]),
            encode(['address', 'uint256'], [currency_in, MAX_UINT256]),
            encode(['address', 'uint256'], [currency_out, minimum]),
        ]
        plan = encode(['bytes', 'bytes[]'], [actions, params])
        return '0x' + (
            selector('execute(bytes,bytes[],uint256)')
            + encode(['bytes', 'bytes[]', 'uint256'], [V4_SWAP_COMMAND, [plan], deadline])
        ).hex()

    async def _submit(self, transaction):
        transaction = dict(transaction)
        event_id, side = transaction.pop('_event_id', None), transaction.pop('_side', None)
        signed = Account.sign_transaction(transaction, self.settings.private_key())
        tx_hash, nonce = signed.hash.hex(), transaction['nonce']
        if event_id and side in ('BUY', 'SELL'):
            from .orders import update_order
            update_order(self.db, event_id, side, 'SIGNED', tx_hash=tx_hash, nonce=nonce)
            attempt(self.db, event_id, side, 'SIGNED', tx_hash=tx_hash, nonce=nonce)
        try:
            returned = await self.rpc.send_raw_transaction('0x' + signed.raw_transaction.hex())
            if returned.lower().removeprefix('0x') != tx_hash.lower().removeprefix('0x'):
                raise ExecutionFailure('RPC_TX_HASH_MISMATCH')
        except RPCError:
            try:
                if await self.rpc.receipt(tx_hash):
                    return {'tx_hash': tx_hash, 'nonce': nonce}
            except RPCError:
                pass
            raise SubmissionUnknown(tx_hash, nonce) from None
        return {'tx_hash': tx_hash, 'nonce': nonce}

    async def submit_buy(self, transaction):
        return await self._submit(transaction)

    async def submit_sell(self, transaction):
        return await self._submit(transaction)

    async def wait_for_receipt(self, tx_hash):
        return await self.rpc.wait_receipt(tx_hash)

    async def receipt_by_hash(self, tx_hash):
        return await self.rpc.receipt(tx_hash)

    def parse_actual_token_received(self, receipt, token, wallet):
        total = 0
        for item in receipt.get('logs') or []:
            topics = item.get('topics') or []
            if (item.get('address', '').lower() == token.lower() and len(topics) >= 3
                    and topics[0].lower() == TRANSFER_TOPIC and topics[2].lower() == address_topic(wallet)):
                total += int(item.get('data', '0x0'), 16)
        return Decimal(total)

    async def _pool_for_position(self, position):
        if position['event_id'] in self.pools:
            return self.pools[position['event_id']]
        row = self.db.conn.execute('SELECT * FROM signals WHERE event_id=?', (position['event_id'],)).fetchone()
        payload = json.loads(row['payload'])
        signal = TradeSignal(row['signal_id'], row['event_id'], 4663, payload['token_address'],
                             row['expires_at'], payload)
        return await self.resolve_pool(signal)

    async def quote_full_sell(self, position):
        pool = await self._pool_for_position(position)
        if pool['version'] == 'v3':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        amount = int(Decimal(position['token_quantity']))
        output = (await self._quote_v4(amount, pool, position['token_address'])
                  if pool['version'] == 'v4'
                  else await self._quote_v2(amount, pool['path_sell']))
        decimals = 18 if self.settings.amount_mode == 'ETH' else self.settings.buy_asset_decimals
        return Decimal(output) / (Decimal(10) ** decimals)

    async def _allowance(self, token, spender):
        result = await self.rpc.eth_call(token, calldata('allowance(address,address)', ['address', 'address'],
                                                        [self.settings.wallet_address, spender]))
        return decode(['uint256'], bytes.fromhex(result[2:]))[0]

    async def _approve_exact(self, event_id, token, spender, amount):
        tx = await self._base_transaction(token, calldata('approve(address,uint256)',
                                                          ['address', 'uint256'], [spender, amount]))
        result = await self._submit(tx)
        receipt = await self.wait_for_receipt(result['tx_hash'])
        status = int(receipt.get('status', '0x0'), 16)
        attempt(self.db, event_id, 'APPROVAL', 'CONFIRMED' if status == 1 else 'REVERTED',
                tx_hash=result['tx_hash'], nonce=result['nonce'], response_facts=dumps(receipt))
        if status != 1:
            raise ExecutionFailure('APPROVAL_REVERTED')

    async def _permit2_allowance(self, token):
        result = await self.rpc.eth_call(
            self.resolver.contracts['permit2'],
            calldata('allowance(address,address,address)', ['address', 'address', 'address'],
                     [self.settings.wallet_address, token, self.resolver.contracts['universal_router']]),
        )
        return decode(['uint160', 'uint48', 'uint48'], bytes.fromhex(result[2:]))

    async def _approve_permit2(self, event_id, token, amount):
        expiration = min(int(time.time()) + 3600, 2 ** 48 - 1)
        data = calldata('approve(address,address,uint160,uint48)',
                        ['address', 'address', 'uint160', 'uint48'],
                        [token, self.resolver.contracts['universal_router'], amount, expiration])
        tx = await self._base_transaction(self.resolver.contracts['permit2'], data)
        result = await self._submit(tx)
        receipt = await self.wait_for_receipt(result['tx_hash'])
        status = int(receipt.get('status', '0x0'), 16)
        attempt(self.db, event_id, 'APPROVAL', 'CONFIRMED' if status == 1 else 'REVERTED',
                tx_hash=result['tx_hash'], nonce=result['nonce'], response_facts=dumps(receipt))
        if status != 1:
            raise ExecutionFailure('APPROVAL_REVERTED')

    async def _ensure_v4_approval(self, event_id, token, amount):
        if not 0 < amount <= MAX_UINT160:
            raise ExecutionFailure('V4_AMOUNT_OUT_OF_RANGE')
        permit2 = self.resolver.contracts['permit2']
        if await self._allowance(token, permit2) < amount:
            await self._approve_exact(event_id, token, permit2, amount)
        allowance, expiration, _ = await self._permit2_allowance(token)
        if allowance < amount or expiration < int(time.time()) + self.settings.deadline_seconds:
            await self._approve_permit2(event_id, token, amount)

    async def ensure_token_approval(self, position):
        pool = await self._pool_for_position(position)
        amount = int(Decimal(position['token_quantity']))
        if pool['version'] == 'v4':
            await self._ensure_v4_approval(position['event_id'], position['token_address'], amount)
            return
        if await self._allowance(position['token_address'], pool['router']) < amount:
            await self._approve_exact(position['event_id'], position['token_address'], pool['router'], amount)

    async def build_sell_transaction(self, position, minimum):
        pool = await self._pool_for_position(position)
        if pool['version'] == 'v3':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        amount = int(Decimal(position['token_quantity']))
        min_raw = raw_amount(minimum, 18 if self.settings.amount_mode == 'ETH'
                             else self.settings.buy_asset_decimals)
        deadline = int(time.time()) + self.settings.deadline_seconds
        if pool['version'] == 'v4':
            data = self._v4_swap_calldata(pool, position['token_address'], self.v4_input_asset,
                                          amount, min_raw, deadline)
            tx = await self._base_transaction(pool['router'], data, 0)
            tx.update(_event_id=position['event_id'], _side='SELL')
            return tx
        name = ('swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)'
                if self.settings.amount_mode == 'ETH' else
                'swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)')
        data = calldata(name, ['uint256', 'uint256', 'address[]', 'address', 'uint256'],
                        [amount, min_raw, pool['path_sell'],
                         self.settings.wallet_address, deadline])
        tx = await self._base_transaction(pool['router'], data)
        tx.update(_event_id=position['event_id'], _side='SELL')
        return tx

    async def parse_actual_sell_proceeds(self, receipt, position):
        pool = self.pools.get(position['event_id'])
        if self.settings.amount_mode == 'USD':
            total = sum(int(item.get('data', '0x0'), 16) for item in receipt.get('logs') or []
                        if item.get('address', '').lower() == self.input_asset and len(item.get('topics') or []) >= 3
                        and item['topics'][0].lower() == TRANSFER_TOPIC
                        and item['topics'][2].lower() == address_topic(self.settings.wallet_address))
            return Decimal(total) / (Decimal(10) ** self.settings.buy_asset_decimals)
        if not pool:
            pool = await self._pool_for_position(position)
        if pool['version'] == 'v4':
            zero_for_one = self._v4_zero_for_one(pool, position['token_address'])
            output_index = 1 if zero_for_one else 0
            total = 0
            for item in receipt.get('logs') or []:
                topics = item.get('topics') or []
                if (item.get('address', '').lower() != pool['pool_manager'] or len(topics) < 2
                        or topics[0].lower() != SWAP_V4_TOPIC
                        or topics[1].lower() != pool['pool_id']):
                    continue
                amounts = decode(['int128', 'int128'], bytes.fromhex(item['data'][2:])[:64])
                total += max(0, -int(amounts[output_index]))
            return Decimal(total) / Decimal(10 ** 18)
        total = 0
        for item in receipt.get('logs') or []:
            if item.get('address', '').lower() != pool['address'] or not item.get('topics'):
                continue
            if item['topics'][0].lower() == SWAP_V2_TOPIC:
                _, _, amount0_out, amount1_out = decode(['uint256'] * 4, bytes.fromhex(item['data'][2:]))
                total += amount0_out if pool['token0'] == self.input_asset else amount1_out
        return Decimal(total) / Decimal(10 ** 18)
