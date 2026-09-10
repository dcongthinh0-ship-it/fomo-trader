from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class ExecutionFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SubmissionUnknown(ExecutionFailure):
    def __init__(self, tx_hash, nonce):
        super().__init__('SUBMISSION_UNKNOWN')
        self.tx_hash, self.nonce = tx_hash, nonce


class ExecutionAdapter(Protocol):
    async def resolve_pool(self, signal): ...
    async def quote_buy(self, signal, pool, amount): ...
    async def build_buy_transaction(self, signal, pool, amount, minimum): ...
    async def submit_buy(self, transaction): ...
    async def wait_for_receipt(self, tx_hash): ...
    def parse_actual_token_received(self, receipt, token, wallet): ...
    async def quote_full_sell(self, position): ...
    async def ensure_token_approval(self, position): ...
    async def build_sell_transaction(self, position, minimum): ...
    async def submit_sell(self, transaction): ...
    async def parse_actual_sell_proceeds(self, receipt, position): ...


@dataclass
class FakeExecutionAdapter:
    buy_quote: Decimal = Decimal('100')
    sell_quote: Decimal = Decimal('13')
    buy_received: Decimal = Decimal('100')
    sell_received: Decimal = Decimal('13')
    fail_buy: str | None = None
    fail_sell: str | None = None

    async def resolve_pool(self, signal):
        return {'version': 'v2', 'address': signal.payload['market_snapshot']['pool_address']}

    async def quote_buy(self, signal, pool, amount):
        if self.fail_buy == 'quote':
            raise ExecutionFailure('BUY_QUOTE_FAILED')
        return self.buy_quote

    async def build_buy_transaction(self, signal, pool, amount, minimum):
        if self.fail_buy == 'build':
            raise ExecutionFailure('BUY_BUILD_FAILED')
        return {'nonce': 1, 'minimum_output': str(minimum)}

    async def submit_buy(self, transaction):
        if self.fail_buy == 'submit':
            raise ExecutionFailure('BUY_SUBMIT_FAILED')
        return {'tx_hash': '0x' + 'a' * 64, 'nonce': transaction['nonce']}

    async def wait_for_receipt(self, tx_hash):
        if self.fail_buy == 'timeout' and tx_hash.endswith('a' * 64):
            raise TimeoutError('RECEIPT_TIMEOUT')
        if self.fail_sell == 'timeout' and tx_hash.endswith('b' * 64):
            raise TimeoutError('RECEIPT_TIMEOUT')
        if self.fail_buy == 'receipt' and tx_hash.endswith('a' * 64):
            return {'status': '0x0', 'transactionHash': tx_hash}
        if self.fail_sell == 'receipt' and tx_hash.endswith('b' * 64):
            return {'status': '0x0', 'transactionHash': tx_hash}
        return {'status': '0x1', 'transactionHash': tx_hash}

    def parse_actual_token_received(self, receipt, token, wallet):
        return self.buy_received

    async def quote_full_sell(self, position):
        if self.fail_sell == 'quote':
            raise ExecutionFailure('SELL_QUOTE_FAILED')
        return self.sell_quote

    async def ensure_token_approval(self, position):
        if self.fail_sell == 'approval':
            raise ExecutionFailure('SELL_APPROVAL_FAILED')
        return None

    async def build_sell_transaction(self, position, minimum):
        if self.fail_sell == 'build':
            raise ExecutionFailure('SELL_BUILD_FAILED')
        return {'nonce': 2, 'minimum_output': str(minimum)}

    async def submit_sell(self, transaction):
        if self.fail_sell == 'submit':
            raise ExecutionFailure('SELL_SUBMIT_FAILED')
        return {'tx_hash': '0x' + 'b' * 64, 'nonce': transaction['nonce']}

    async def parse_actual_sell_proceeds(self, receipt, position):
        return self.sell_received

    async def receipt_by_hash(self, tx_hash):
        return {'status': '0x1', 'transactionHash': tx_hash}
