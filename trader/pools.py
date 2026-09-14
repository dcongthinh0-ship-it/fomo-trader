from eth_abi import decode, encode
from eth_utils import keccak

from .execution import ExecutionFailure
from .models import ADDRESS

ZERO_ADDRESS = '0x' + '0' * 40
V4_INITIALIZE_TOPIC = '0x' + keccak(
    text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)').hex()
MAX_V4_BRIDGE_CANDIDATES = 8


def selector(signature):
    return keccak(text=signature)[:4]


def calldata(signature, types=(), values=()):
    return '0x' + (selector(signature) + encode(list(types), list(values))).hex()


def word_address(raw):
    return '0x' + bytes.fromhex(raw.removeprefix('0x'))[-20:].hex()


def address_topic(address):
    return '0x' + address.lower().removeprefix('0x').rjust(64, '0')


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
            if other == self.input_asset:
                result.update(path_buy=[self.input_asset, signal.token_address.lower()],
                              path_sell=[signal.token_address.lower(), self.input_asset],
                              fees_buy=[fee], fees_sell=[fee])
            else:
                routes = await self._discover_v3_bridge_routes(other, signal.token_address.lower(), fee)
                if not routes:
                    raise ExecutionFailure('ROUTE_NOT_FOUND')
                result['route_candidates'] = routes
        return result

    async def _discover_v3_bridge_routes(self, route_asset, token, target_fee):
        routes = []
        seen = set()
        for fee in (100, 500, 2500, 3000, 10000):
            raw = await self._call(
                self.contracts['v3_factory'], 'getPool(address,address,uint24)',
                ['address', 'address', 'uint24'], [self.input_asset, route_asset, fee])
            bridge = word_address(raw)
            if bridge == ZERO_ADDRESS or (bridge, fee) in seen:
                continue
            if await self.rpc.get_code(bridge) in ('0x', '0x0', None):
                continue
            if word_address(await self._call(bridge, 'factory()')) != self.contracts['v3_factory']:
                continue
            seen.add((bridge, fee))
            routes.append({
                'bridge_pool': bridge,
                'path_buy': [self.input_asset, route_asset, token],
                'path_sell': [token, route_asset, self.input_asset],
                'fees_buy': [fee, target_fee],
                'fees_sell': [target_fee, fee],
            })
        return routes

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
        token = signal.token_address.lower()
        if token not in {currency0, currency1}:
            raise ExecutionFailure('POOL_ASSET_MISMATCH')
        encoded = encode(['address', 'address', 'uint24', 'int24', 'address'],
                         [currency0, currency1, normalized['fee'], normalized['tick_spacing'], hooks])
        if '0x' + keccak(encoded).hex() != pool_id.lower():
            raise ExecutionFailure('V4_POOL_ID_MISMATCH')
        for contract in ('v4_pool_manager', 'v4_state_view', 'v4_quoter',
                         'universal_router', 'multicall3'):
            if await self.rpc.get_code(self.contracts[contract]) in ('0x', '0x0', None):
                raise ExecutionFailure('VERIFIED_ROUTER_CODE_MISSING')
        result = {'version': 'v4', 'pool_id': pool_id.lower(), 'pool_key': normalized,
                  'pool_manager': self.contracts['v4_pool_manager'],
                  'quoter': self.contracts['v4_quoter'],
                  'router': self.contracts['universal_router']}
        route_asset = currency1 if currency0 == token else currency0
        result['route_asset'] = route_asset
        if route_asset != self.v4_input_asset:
            bridges = await self._discover_v4_keys_between(self.v4_input_asset, route_asset)
            if not bridges:
                raise ExecutionFailure('ROUTE_NOT_FOUND')
            result['route_candidates'] = [
                {'keys': [bridge, normalized], 'keys_buy': [bridge, normalized],
                 'keys_sell': [normalized, bridge]}
                for bridge in bridges
            ]
        return result

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
        return self._decode_v4_initialize(logs[0])

    async def _discover_v4_keys_between(self, left, right):
        currency0, currency1 = sorted((left.lower(), right.lower()))
        logs = await self.rpc.call('eth_getLogs', [{
            'address': self.contracts['v4_pool_manager'],
            'fromBlock': '0x0',
            'toBlock': 'latest',
            'topics': [V4_INITIALIZE_TOPIC, None, address_topic(currency0), address_topic(currency1)],
        }])
        result = []
        for item in logs or []:
            try:
                key = self._decode_v4_initialize(item)
            except ExecutionFailure:
                continue
            if {key['currency0'], key['currency1']} == {left.lower(), right.lower()}:
                result.append((item['topics'][1].lower(), key))
        if not result:
            return []
        ranked = await self._rank_v4_keys_by_liquidity(result)
        return [key for liquidity, key in ranked[:MAX_V4_BRIDGE_CANDIDATES] if liquidity > 0]

    async def _rank_v4_keys_by_liquidity(self, identified_keys):
        calls = [(
            self.contracts['v4_state_view'],
            bytes.fromhex(calldata('getLiquidity(bytes32)', ['bytes32'],
                                   [bytes.fromhex(pool_id[2:])])[2:]),
        ) for pool_id, _ in identified_keys]
        response = await self.rpc.eth_call(
            self.contracts['multicall3'],
            calldata('tryAggregate(bool,(address,bytes)[])',
                     ['bool', '(address,bytes)[]'], [False, calls]),
        )
        rows = decode(['(bool,bytes)[]'], bytes.fromhex(response[2:]))[0]
        if len(rows) != len(identified_keys):
            raise ExecutionFailure('V4_LIQUIDITY_BATCH_INVALID')
        ranked = []
        for (_, key), (success, raw) in zip(identified_keys, rows, strict=True):
            liquidity = int.from_bytes(raw, 'big') if success and len(raw) == 32 else 0
            ranked.append((liquidity, key))
        return sorted(ranked, key=lambda item: item[0], reverse=True)

    @staticmethod
    def _decode_v4_initialize(item):
        topics = item.get('topics') or []
        data = item.get('data', '0x')
        if len(topics) != 4 or topics[0].lower() != V4_INITIALIZE_TOPIC:
            raise ExecutionFailure('V4_INITIALIZE_LOG_INVALID')
        try:
            fee, spacing, hooks, _, _ = decode(
                ['uint24', 'int24', 'address', 'uint160', 'int24'],
                bytes.fromhex(data.removeprefix('0x')),
            )
            key = {'currency0': word_address(topics[2]), 'currency1': word_address(topics[3]),
                   'fee': int(fee), 'tick_spacing': int(spacing), 'hooks': hooks.lower()}
            encoded = encode(['address', 'address', 'uint24', 'int24', 'address'],
                             [key['currency0'], key['currency1'], key['fee'],
                              key['tick_spacing'], key['hooks']])
            if '0x' + keccak(encoded).hex() != topics[1].lower():
                raise ExecutionFailure('V4_POOL_ID_MISMATCH')
            return key
        except (TypeError, ValueError, OverflowError):
            raise ExecutionFailure('V4_INITIALIZE_LOG_INVALID') from None
