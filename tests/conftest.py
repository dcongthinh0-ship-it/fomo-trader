from types import SimpleNamespace

import pytest

from trader.db import DB


@pytest.fixture
def db(tmp_path):
    result = DB(str(tmp_path / 'trader.db'))
    yield result
    result.conn.close()


@pytest.fixture
def settings(tmp_path):
    secret = tmp_path / 'shared-secret'
    secret.write_bytes(b'test-shared-secret')
    return SimpleNamespace(shared_secret=lambda: secret.read_bytes(), max_clock_skew=30,
                           live=False, adapter='fake')


@pytest.fixture
def valid_payload():
    return {'version': 'trade_signal_v1', 'signal_id': 'signal-1', 'event_id': 'event-1',
            'chain_id': 4663, 'token_address': '0x' + '2' * 40, 'expires_at': 2_000_000_030,
            'eligibility': {'eligible': True}, 'market_snapshot': {'market_cap_usd': '50000',
            'liquidity_usd': '9000', 'price_usd': '0.01', 'pool_address': '0x' + '3' * 40}}
