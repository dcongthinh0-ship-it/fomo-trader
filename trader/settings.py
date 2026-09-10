import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from eth_account import Account


def load_env(path='.env'):
    if Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())


def decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{name} must be numeric') from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f'{name} must be positive and finite')
    return result


class Settings:
    def __init__(self):
        load_env()
        self.database = os.getenv('DATABASE_PATH', 'data/trader.db')
        self.config_path = Path(os.getenv('CONFIG_PATH', 'config/trading.yaml'))
        self.config = yaml.safe_load(self.config_path.read_text())
        self.chain_id = int(self.config.get('chain_id', 4663))
        if self.chain_id != 4663:
            raise ValueError('chain_id must be 4663')
        self.secret_file = os.getenv('SIGNAL_SHARED_SECRET_FILE', '')
        self.max_clock_skew = int(os.getenv('SIGNAL_MAX_CLOCK_SKEW_SECONDS', '30'))
        self.live = os.getenv('LIVE_TRADING_ENABLED', 'false').lower() == 'true'
        self.adapter = os.getenv('EXECUTION_ADAPTER', 'uniswap')
        self.rpc_url = os.getenv('ROBINHOOD_TRADING_RPC_URL', '')
        self.rpc_rps = float(os.getenv('ROBINHOOD_TRADING_RPC_REQUESTS_PER_SECOND', '5'))
        self.health_port = int(os.getenv('HEALTH_PORT', '8090'))
        order = self.config['order']
        self.amount_mode = os.getenv('BUY_AMOUNT_MODE', order['amount_mode']).upper()
        self.amount = decimal(os.getenv('BUY_AMOUNT', order['amount']), 'BUY_AMOUNT')
        self.take_profit_pct = decimal(order.get('take_profit_pct', '30'), 'take_profit_pct')
        self.sell_percentage = decimal(order.get('sell_percentage', '100'), 'sell_percentage')
        if self.take_profit_pct != Decimal('30') or self.sell_percentage != Decimal('100'):
            raise ValueError('strategy is fixed at 30% take profit and 100% sell')
        self.buy_slippage_bps = int(os.getenv('BUY_MAX_SLIPPAGE_BPS', order['buy_max_slippage_bps']))
        self.sell_slippage_bps = int(os.getenv('SELL_MAX_SLIPPAGE_BPS', order['sell_max_slippage_bps']))
        self.deadline_seconds = int(os.getenv('TX_DEADLINE_SECONDS', order['tx_deadline_seconds']))
        self.price_poll_seconds = float(order.get('price_poll_seconds', 1))
        self.max_sell_attempts = int(order.get('max_sell_attempts', 3))
        for value in (self.buy_slippage_bps, self.sell_slippage_bps):
            if not 0 <= value < 10000:
                raise ValueError('slippage bps must be in [0,10000)')
        default_asset = self.config.get('contracts', {}).get('usdg', '') if self.amount_mode == 'USD' else ''
        self.buy_asset_address = os.getenv('BUY_ASSET_ADDRESS', default_asset).lower()
        self.buy_asset_decimals = int(os.getenv('BUY_ASSET_DECIMALS', '6' if self.amount_mode == 'USD' else '18'))
        self.buy_asset_symbol = os.getenv('BUY_ASSET_SYMBOL', 'USDG' if self.amount_mode == 'USD' else 'ETH')
        if self.amount_mode not in ('ETH', 'USD'):
            raise ValueError('BUY_AMOUNT_MODE must be ETH or USD')
        if self.amount_mode == 'USD' and not re.fullmatch(r'0x[0-9a-f]{40}', self.buy_asset_address):
            raise ValueError('USD mode requires BUY_ASSET_ADDRESS')
        self.wallet_address = os.getenv('TRADER_WALLET_ADDRESS', '')
        self.private_key_file = os.getenv('TRADER_PRIVATE_KEY_FILE', '')
        if self.live:
            self._validate_live_credentials()

    def shared_secret(self):
        value = Path(self.secret_file).read_bytes().strip()
        if not value:
            raise ValueError('empty signal shared secret')
        return value

    def _validate_live_credentials(self):
        if not self.rpc_url or not self.private_key_file or not Path(self.private_key_file).is_file():
            raise ValueError('live trading requires RPC and private-key file')
        key = Path(self.private_key_file).read_text().strip()
        derived = Account.from_key(key).address
        if derived.lower() != self.wallet_address.lower():
            raise ValueError('configured wallet does not match private key')

    def private_key(self):
        if not self.live:
            raise RuntimeError('LIVE_TRADING_DISABLED')
        return Path(self.private_key_file).read_text().strip()
