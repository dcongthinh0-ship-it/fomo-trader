import json
import time
from decimal import Decimal

from eth_abi import decode
from eth_account import Account
from eth_utils import keccak

from .db import dumps
from .execution import ExecutionFailure, SubmissionUnknown
from .models import TradeSignal
from .orders import attempt
from .pools import PoolResolver, calldata
from .rpc import RPCError

TRANSFER_TOPIC = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
SWAP_V2_TOPIC = '0x' + keccak(text='Swap(address,uint256,uint256,uint256,uint256,address)').hex()


def raw_amount(amount, decimals):
    value = Decimal(str(amount)) * (Decimal(10) ** int(decimals))
    if value != value.to_integral_value() or value <= 0:
        raise ExecutionFailure('INVALID_ASSET_AMOUNT')
    return int(value)


def address_topic(address):
    return '0x' + address.lower().removeprefix('0x').rjust(64, '0')


class UniswapRobinhoodExecutionAdapter:
    """Direct execution. V2 is enabled in code; V3/V4 remain fail-closed pending fork validation."""
    def __init__(self, db, rpc, nonce, settings):
        self.db, self.rpc, self.nonce, self.settings = db, rpc, nonce, settings
        contracts = settings.config['contracts']
        input_asset = contracts['weth'] if settings.amount_mode == 'ETH' else settings.buy_asset_address
        self.input_asset = input_asset.lower()
        self.resolver = PoolResolver(rpc, contracts, self.input_asset)
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

    async def quote_buy(self, signal, pool, amount):
        if pool['version'] != 'v2':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        decimals = 18 if self.settings.amount_mode == 'ETH' else self.settings.buy_asset_decimals
        return Decimal(await self._quote_v2(raw_amount(amount, decimals), pool['path_buy']))

    async def _base_transaction(self, to, data, value=0):
        nonce = await self.nonce.reserve()
        tx = {'to': to, 'data': data, 'value': value, 'nonce': nonce,
              'chainId': 4663, 'gasPrice': int(await self.rpc.call('eth_gasPrice'), 16)}
        estimate = await self.rpc.call('eth_estimateGas', [{**tx, 'from': self.settings.wallet_address}])
        tx['gas'] = int(int(estimate, 16) * 1.2)
        return tx

    async def build_buy_transaction(self, signal, pool, amount, minimum):
        if pool['version'] != 'v2':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        deadline, router = int(time.time()) + self.settings.deadline_seconds, pool['router']
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
        if pool['version'] != 'v2':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        output = await self._quote_v2(int(Decimal(position['token_quantity'])), pool['path_sell'])
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
                tx_hash=result['tx_hash'], nonce=result['nonce'], response_facts=dumps({'status': status}))
        if status != 1:
            raise ExecutionFailure('APPROVAL_REVERTED')

    async def ensure_token_approval(self, position):
        pool = await self._pool_for_position(position)
        amount = int(Decimal(position['token_quantity']))
        if await self._allowance(position['token_address'], pool['router']) < amount:
            await self._approve_exact(position['event_id'], position['token_address'], pool['router'], amount)

    async def build_sell_transaction(self, position, minimum):
        pool = await self._pool_for_position(position)
        if pool['version'] != 'v2':
            raise ExecutionFailure(f'{pool["version"].upper()}_EXECUTION_NOT_FORK_VALIDATED')
        amount = int(Decimal(position['token_quantity']))
        min_raw = raw_amount(minimum, 18 if self.settings.amount_mode == 'ETH'
                             else self.settings.buy_asset_decimals)
        deadline = int(time.time()) + self.settings.deadline_seconds
        name = ('swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)'
                if self.settings.amount_mode == 'ETH' else
                'swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)')
        data = calldata(name, ['uint256', 'uint256', 'address[]', 'address', 'uint256'],
                        [amount, min_raw, pool['path_sell'],
                         self.settings.wallet_address, deadline])
        tx = await self._base_transaction(pool['router'], data)
        tx.update(_event_id=position['event_id'], _side='SELL')
        return tx

    def parse_actual_sell_proceeds(self, receipt, position):
        pool = self.pools.get(position['event_id'])
        if self.settings.amount_mode == 'USD':
            total = sum(int(item.get('data', '0x0'), 16) for item in receipt.get('logs') or []
                        if item.get('address', '').lower() == self.input_asset and len(item.get('topics') or []) >= 3
                        and item['topics'][0].lower() == TRANSFER_TOPIC
                        and item['topics'][2].lower() == address_topic(self.settings.wallet_address))
            return Decimal(total) / (Decimal(10) ** self.settings.buy_asset_decimals)
        if not pool:
            raise ExecutionFailure('POOL_CONTEXT_MISSING')
        total = 0
        for item in receipt.get('logs') or []:
            if item.get('address', '').lower() != pool['address'] or not item.get('topics'):
                continue
            if item['topics'][0].lower() == SWAP_V2_TOPIC:
                _, _, amount0_out, amount1_out = decode(['uint256'] * 4, bytes.fromhex(item['data'][2:]))
                total += amount0_out if pool['token0'] == self.input_asset else amount1_out
        return Decimal(total) / Decimal(10 ** 18)
