from eth_abi import decode, encode
from eth_utils import keccak

from .execution import ExecutionFailure
from .models import ADDRESS

ZERO_ADDRESS = '0x' + '0' * 40
V4_INITIALIZE_TOPIC = '0x' + keccak(
    text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)').hex()


def selector(signature):
    return keccak(text=signature)[:4]


def calldata(signature, types=(), values=()):
    return '0x' + (selector(signature) + encode(list(types), list(values))).hex()


def word_address(raw):
    return '0x' + bytes.fromhex(raw.removeprefix('0x'))[-20:].hex()


class PoolResolver:
    def __init__(self, rpc, contracts, input_asset, v4_input_asset=None):
        self.rpc = rpc
        self.contracts = {key: value.lower() for key, value in contracts.items()}
        self.input_asset = input_asset.lower()
        self.v4_input_asset = (v4_input_asset or input_asset).lower()

    async def _call(self, address, signature, types=(), values=()):
        return await self.rpc.eth_call(address, calldata(signature, types, values))

    async def identify_pool_version(self, hint):
        if isinstance(hint, str) and hint.startswith('0x') and len(hint) == 66:
            return 'v4'
        if not ADDRESS.fullmatch(str(hint)):
            raise ExecutionFailure('INVALID_POOL_HINT')
        if await self.rpc.get_code(hint) in ('0x', '0x0', None):
            raise ExecutionFailure('POOL_CODE_MISSING')
        try:
            factory = word_address(await self._call(hint, 'factory()'))
        except Exception:
            raise ExecutionFailure('POOL_FACTORY_UNREADABLE') from None
        if factory == self.contracts['v2_factory']:
            return 'v2'
        if factory == self.contracts['v3_factory']:
            return 'v3'
        raise ExecutionFailure('UNVERIFIED_POOL_FACTORY')

    async def resolve_pool(self, signal):
        snapshot = signal.payload.get('market_snapshot') or {}
        hint = snapshot.get('pool_address')
        version = await self.identify_pool_version(hint)
        if version == 'v4':
            return await self._resolve_v4(signal, hint, snapshot.get('pool_key'))
        token0 = word_address(await self._call(hint, 'token0()'))
        token1 = word_address(await self._call(hint, 'token1()'))
        if signal.token_address.lower() not in {token0, token1}:
            raise ExecutionFailure('POOL_ASSET_MISMATCH')
        other = token1 if token0 == signal.token_address.lower() else token0
        result = {'version': version, 'address': hint.lower(), 'token0': token0, 'token1': token1,
                  'route_asset': other}
        if version == 'v2':
            reserves = await self._call(hint, 'getReserves()')
            reserve0, reserve1, _ = decode(['uint112', 'uint112', 'uint32'], bytes.fromhex(reserves[2:]))
            if reserve0 == 0 or reserve1 == 0:
                raise ExecutionFailure('POOL_HAS_NO_RESERVES')
            result.update(factory=self.contracts['v2_factory'], router=self.contracts['v2_router'],
                          reserves=(reserve0, reserve1))
            if other == self.input_asset:
                path = [self.input_asset, signal.token_address.lower()]
            else:
                bridge_raw = await self._call(self.contracts['v2_factory'], 'getPair(address,address)',
                                              ['address', 'address'], [self.input_asset, other])
                bridge = word_address(bridge_raw)
                if bridge == '0x' + '0' * 40 or await self.rpc.get_code(bridge) in ('0x', '0x0', None):
                    raise ExecutionFailure('ROUTE_NOT_FOUND')
                if word_address(await self._call(bridge, 'factory()')) != self.contracts['v2_factory']:
                    raise ExecutionFailure('UNVERIFIED_ROUTE_FACTORY')
                bridge_reserves = await self._call(bridge, 'getReserves()')
                left, right, _ = decode(['uint112', 'uint112', 'uint32'], bytes.fromhex(bridge_reserves[2:]))
                if left == 0 or right == 0:
                    raise ExecutionFailure('ROUTE_HAS_NO_RESERVES')
                result['bridge_pair'] = bridge
                path = [self.input_asset, other, signal.token_address.lower()]
            result.update(path_buy=path, path_sell=list(reversed(path)))
        else:
            fee = decode(['uint24'], bytes.fromhex((await self._call(hint, 'fee()'))[2:]))[0]
            spacing = decode(['int24'], bytes.fromhex((await self._call(hint, 'tickSpacing()'))[2:]))[0]
            result.update(factory=self.contracts['v3_factory'], router=self.contracts['v3_router'],
                          quoter=self.contracts['v3_quoter'], fee=fee, tick_spacing=spacing)
            if other != self.input_asset:
                raise ExecutionFailure('V3_MULTIHOP_NOT_FORK_VALIDATED')
            result.update(path_buy=[self.input_asset, signal.token_address.lower()],
                          path_sell=[signal.token_address.lower(), self.input_asset])
        return result

    async def _resolve_v4(self, signal, pool_id, key):
        if not isinstance(key, dict):
            key = await self._discover_v4_key(pool_id)
        required = ('currency0', 'currency1', 'fee', 'tick_spacing', 'hooks')
        if any(name not in key for name in required):
            raise ExecutionFailure('V4_POOL_KEY_INCOMPLETE')
        currency0, currency1 = str(key['currency0']).lower(), str(key['currency1']).lower()
        hooks = str(key['hooks']).lower()
        if not all(ADDRESS.fullmatch(item) for item in (currency0, currency1, hooks)):
            raise ExecutionFailure('V4_POOL_KEY_INVALID')
        normalized = {'currency0': currency0, 'currency1': currency1,
                      'fee': int(key['fee']), 'tick_spacing': int(key['tick_spacing']),
                      'hooks': hooks}
        if {currency0, currency1} != {signal.token_address.lower(), self.v4_input_asset}:
            raise ExecutionFailure('POOL_ASSET_MISMATCH')
        encoded = encode(['address', 'address', 'uint24', 'int24', 'address'],
                         [currency0, currency1, normalized['fee'], normalized['tick_spacing'], hooks])
        if '0x' + keccak(encoded).hex() != pool_id.lower():
            raise ExecutionFailure('V4_POOL_ID_MISMATCH')
        for contract in ('v4_pool_manager', 'v4_state_view', 'v4_quoter', 'universal_router'):
            if await self.rpc.get_code(self.contracts[contract]) in ('0x', '0x0', None):
                raise ExecutionFailure('VERIFIED_ROUTER_CODE_MISSING')
        return {'version': 'v4', 'pool_id': pool_id.lower(), 'pool_key': normalized,
                'pool_manager': self.contracts['v4_pool_manager'],
                'quoter': self.contracts['v4_quoter'],
                'router': self.contracts['universal_router']}

    async def _discover_v4_key(self, pool_id):
        logs = await self.rpc.call('eth_getLogs', [{
            'address': self.contracts['v4_pool_manager'],
            'fromBlock': '0x0',
            'toBlock': 'latest',
            'topics': [V4_INITIALIZE_TOPIC, pool_id.lower()],
        }])
        if not logs:
            raise ExecutionFailure('V4_POOL_KEY_NOT_FOUND')
        if len(logs) != 1:
            raise ExecutionFailure('V4_POOL_KEY_AMBIGUOUS')
        topics = logs[0].get('topics') or []
        data = logs[0].get('data', '0x')
        if len(topics) != 4 or topics[0].lower() != V4_INITIALIZE_TOPIC:
            raise ExecutionFailure('V4_INITIALIZE_LOG_INVALID')
        try:
            fee, spacing, hooks, _, _ = decode(
                ['uint24', 'int24', 'address', 'uint160', 'int24'],
                bytes.fromhex(data.removeprefix('0x')),
            )
            return {'currency0': word_address(topics[2]), 'currency1': word_address(topics[3]),
                    'fee': int(fee), 'tick_spacing': int(spacing), 'hooks': hooks.lower()}
        except (TypeError, ValueError, OverflowError):
            raise ExecutionFailure('V4_INITIALIZE_LOG_INVALID') from None
