from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from trader.execution import FakeExecutionAdapter
from trader.models import TradeSignal
from trader.nonce import NonceManager
from trader.worker import TradingWorker


def worker_settings(live=True, max_sell_attempts=3, amount='10'):
    return SimpleNamespace(live=live, amount=Decimal(amount), amount_mode='USD',
                           buy_slippage_bps=500, sell_slippage_bps=500,
                           wallet_address='0x' + '1' * 40, max_sell_attempts=max_sell_attempts,
                           price_poll_seconds=1)


def accept(db, payload, expires=1000):
    body = {**payload, 'expires_at': expires}
    signal = TradeSignal.parse(body, now=100)
    db.accept_signal(signal, now=100)


async def test_live_disabled_keeps_signal_received(db, valid_payload):
    accept(db, valid_payload)
    worker = TradingWorker(db, FakeExecutionAdapter(), worker_settings(live=False))
    assert not await worker.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'RECEIVED'


async def test_expired_signal_never_buys(db, valid_payload):
    accept(db, valid_payload, expires=101)
    worker = TradingWorker(db, FakeExecutionAdapter(), worker_settings())
    assert await worker.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'EXPIRED'
    assert db.conn.execute('SELECT count(*) FROM orders').fetchone()[0] == 0


async def test_fake_adapter_full_buy_and_30_percent_sell(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(buy_quote=Decimal('100'), buy_received=Decimal('80'),
                                   sell_quote=Decimal('13'), sell_received=Decimal('12.8'))
    worker = TradingWorker(db, adapter, worker_settings())
    assert await worker.buy_once(now=101)
    position = db.conn.execute('SELECT * FROM positions').fetchone()
    assert position['actual_cost'] == '10'
    assert position['token_quantity'] == '80'
    assert position['target_proceeds'] == '13.00'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1
    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'CLOSED'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'CLOSED'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='SELL'").fetchone()[0] == 1


async def test_sell_does_not_trigger_below_exact_target(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('12.999'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    assert not await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='SELL'").fetchone()[0] == 0


async def test_buy_failure_is_terminal_and_not_retried(db, valid_payload):
    accept(db, valid_payload)
    worker = TradingWorker(db, FakeExecutionAdapter(fail_buy='build'), worker_settings())
    assert await worker.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'BUY_FAILED'
    assert not await worker.buy_once(now=102)
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1


async def test_reverted_buy_receipt_never_creates_open_position(db, valid_payload):
    accept(db, valid_payload)
    worker = TradingWorker(db, FakeExecutionAdapter(fail_buy='receipt'), worker_settings())
    assert await worker.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'BUY_FAILED'
    assert db.conn.execute('SELECT count(*) FROM positions').fetchone()[0] == 0


async def test_receipt_timeout_recovers_without_second_buy(db, valid_payload):
    accept(db, valid_payload)
    first = TradingWorker(db, FakeExecutionAdapter(fail_buy='timeout'), worker_settings())
    assert await first.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'BUY_SUBMITTED'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1
    restarted = TradingWorker(db, FakeExecutionAdapter(buy_received=Decimal('77')), worker_settings())
    assert await restarted.buy_once(now=102)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'OPEN'
    assert db.conn.execute('SELECT token_quantity FROM positions').fetchone()[0] == '77'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1


async def test_sell_failure_keeps_open_then_marks_stuck(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(fail_sell='quote')
    worker = TradingWorker(db, adapter, worker_settings(max_sell_attempts=2))
    await worker.buy_once(now=101)
    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert await worker.sell_once(now=103)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'POSITION_STUCK'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'POSITION_STUCK'


async def test_reverted_sell_receipt_does_not_close_position(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('13'), fail_sell='receipt')
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'OPEN'


async def test_six_usd_buys_then_exactly_7_8_sells_full_position(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(buy_received=Decimal('600'), sell_quote=Decimal('7.8'))
    first = TradingWorker(db, adapter, worker_settings(amount='6'))
    await first.buy_once(now=101)
    position = db.conn.execute('SELECT * FROM positions').fetchone()
    assert position['target_proceeds'] == '7.80'
    restarted = TradingWorker(db, adapter, worker_settings(amount='6'))
    assert await restarted.sell_once(now=102)
    sell = db.conn.execute("SELECT * FROM orders WHERE side='SELL'").fetchone()
    assert sell['input_amount'] == position['token_quantity'] == '600'
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'CLOSED'


async def test_nonce_uses_max_of_chain_and_persisted_value(db):
    rpc = SimpleNamespace(transaction_count=AsyncMock(side_effect=[7, 7]))
    manager = NonceManager(db, rpc, '0x' + '1' * 40)
    assert await manager.reserve() == 7
    assert await manager.reserve() == 8


async def test_nonce_reconcile_prefers_chain_truth(db):
    rpc = SimpleNamespace(transaction_count=AsyncMock(return_value=12))
    manager = NonceManager(db, rpc, '0x' + '1' * 40)
    assert await manager.reconcile() == 12
    assert db.conn.execute('SELECT next_nonce FROM nonce_state').fetchone()[0] == 12
