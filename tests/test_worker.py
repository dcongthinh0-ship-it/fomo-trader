import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trader.execution import ExecutionFailure, FakeExecutionAdapter, SubmissionUnknown
from trader.models import TradeSignal
from trader.nonce import NonceManager
from trader.positions import open_position
from trader.rpc import RPCError
from trader.worker import TradingWorker


def worker_settings(live=True, max_sell_attempts=3, amount='10', max_open_positions=3):
    return SimpleNamespace(live=live, amount=Decimal(amount), amount_mode='USD',
                           buy_slippage_bps=500, sell_slippage_bps=500,
                           crash_sell_drop_pct=Decimal('90'), crash_sell_slippage_bps=5000,
                           wallet_address='0x' + '1' * 40, max_sell_attempts=max_sell_attempts,
                           max_open_positions=max_open_positions, price_poll_seconds=1,
                           position_reconcile_seconds=30, single_buy_test_session='')


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


async def test_signal_skipped_at_three_positions_is_never_bought_later(db, valid_payload):
    payloads = []
    for index in range(4):
        payload = deepcopy(valid_payload)
        payload['signal_id'] = format(index + 1, '064x')
        payload['event_id'] = format(index + 11, '064x')
        payload['token_address'] = '0x' + format(index + 21, '040x')
        accept(db, payload)
        payloads.append(payload)
    for index, payload in enumerate(payloads[:3]):
        open_position(db, payload['event_id'], payload['token_address'], 'ETH', '0.0004', '100',
                      '0x' + format(index + 31, '064x'), now=101 + index)
        position_status = 'POSITION_STUCK' if index == 2 else 'OPEN'
        with db.conn:
            db.conn.execute('UPDATE positions SET status=? WHERE event_id=?',
                            (position_status, payload['event_id']))
            db.conn.execute('UPDATE signals SET status=? WHERE event_id=?',
                            (position_status, payload['event_id']))

    adapter = FakeExecutionAdapter()
    worker = TradingWorker(db, adapter, worker_settings(amount='0.0004'))
    assert not await worker.buy_once(now=200)
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT status FROM signals WHERE event_id=?", (payloads[3]['event_id'],)).fetchone()[0] == 'SKIPPED'

    with db.conn:
        db.conn.execute("UPDATE positions SET status='CLOSED' WHERE event_id=?",
                        (payloads[0]['event_id'],))
        db.conn.execute("UPDATE signals SET status='CLOSED' WHERE event_id=?",
                        (payloads[0]['event_id'],))
    assert not await worker.buy_once(now=201)
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 0

    fresh = deepcopy(valid_payload)
    fresh['signal_id'] = format(5, '064x')
    fresh['event_id'] = format(15, '064x')
    fresh['token_address'] = '0x' + format(25, '040x')
    accept(db, fresh)
    assert await worker.buy_once(now=202)
    active = db.conn.execute("SELECT count(*) FROM positions WHERE status!='CLOSED'").fetchone()[0]
    assert active == 3
    assert db.conn.execute(
        "SELECT status FROM signals WHERE event_id=?", (fresh['event_id'],)).fetchone()[0] == 'OPEN'


async def test_fake_adapter_full_buy_and_40_percent_sell(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(buy_quote=Decimal('100'), buy_received=Decimal('80'),
                                   sell_quote=Decimal('14'), sell_received=Decimal('13.8'))
    worker = TradingWorker(db, adapter, worker_settings())
    assert await worker.buy_once(now=101)
    position = db.conn.execute('SELECT * FROM positions').fetchone()
    assert position['actual_cost'] == '10'
    assert position['token_quantity'] == '80'
    assert position['target_proceeds'] == '14.00'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1
    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'CLOSED'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'CLOSED'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='SELL'").fetchone()[0] == 1


async def test_single_buy_test_session_stays_locked_after_position_closes(db, valid_payload):
    accept(db, valid_payload)
    settings = worker_settings()
    settings.single_buy_test_session = 'one-coin-tp-test'
    worker = TradingWorker(db, FakeExecutionAdapter(), settings)

    assert await worker.buy_once(now=101)
    latch = db.state('single_buy_test:one-coin-tp-test')
    assert latch['completed'] is True
    assert latch['event_id'] == valid_payload['event_id']

    with db.conn:
        db.conn.execute("UPDATE positions SET status='CLOSED' WHERE event_id=?",
                        (valid_payload['event_id'],))
        db.conn.execute("UPDATE signals SET status='CLOSED' WHERE event_id=?",
                        (valid_payload['event_id'],))

    second = deepcopy(valid_payload)
    second['signal_id'] = format(2, '064x')
    second['event_id'] = format(12, '064x')
    second['token_address'] = '0x' + format(22, '040x')
    accept(db, second)

    restarted_worker = TradingWorker(db, FakeExecutionAdapter(), settings)
    assert not await restarted_worker.buy_once(now=102)
    row = db.conn.execute(
        'SELECT status,last_error FROM signals WHERE event_id=?',
        (second['event_id'],)).fetchone()
    assert tuple(row) == ('SKIPPED', 'SINGLE_BUY_TEST_COMPLETE')
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1


async def test_sell_does_not_trigger_below_exact_target(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('13.999'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    assert not await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='SELL'").fetchone()[0] == 0


async def test_strategy_has_no_ordinary_stop_loss(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('2'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)

    assert not await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='SELL'").fetchone()[0] == 0


async def test_ninety_percent_loss_from_entry_triggers_immediate_full_sell(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('1'), sell_received=Decimal('0.9'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)

    assert await worker.sell_once(now=102)
    sell = db.conn.execute("SELECT * FROM orders WHERE side='SELL'").fetchone()
    trigger = db.conn.execute(
        "SELECT request_facts FROM execution_attempts WHERE side='SELL' AND status='TRIGGERED'"
    ).fetchone()
    facts = json.loads(trigger['request_facts'])
    assert sell['input_amount'] == '100'
    assert sell['expected_output'] == '1' and sell['minimum_output'] == '0.5'
    assert facts['reason'] == 'CRASH_FROM_ENTRY_90_PCT'
    assert facts['slippage_bps'] == 5000
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'CLOSED'


async def test_ninety_percent_interval_crash_triggers_even_above_entry_crash_floor(
        db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('1.3'), sell_received=Decimal('1.2'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    db.set_state(worker._quote_state_key(valid_payload['event_id']), {'quote': '13', 'at': 101})

    assert await worker.sell_once(now=102)
    trigger = db.conn.execute(
        "SELECT request_facts FROM execution_attempts WHERE side='SELL' AND status='TRIGGERED'"
    ).fetchone()
    assert json.loads(trigger['request_facts'])['reason'] == (
        'CRASH_FROM_PREVIOUS_QUOTE_90_PCT')


async def test_emergency_exit_remains_latched_until_sell_confirms(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('1'), sell_received=Decimal('1.8'))
    adapter.build_sell_transaction = AsyncMock(side_effect=[
        ExecutionFailure('SELL_BUILD_FAILED'),
        {'nonce': 2, 'minimum_output': '1'},
    ])
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)

    assert await worker.sell_once(now=102)
    adapter.sell_quote = Decimal('2')
    assert await worker.sell_once(now=103)
    assert adapter.build_sell_transaction.await_count == 2
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'CLOSED'
    assert db.state(worker._emergency_state_key(valid_payload['event_id'])) is None


async def test_buy_failure_is_terminal_and_not_retried(db, valid_payload):
    accept(db, valid_payload)
    worker = TradingWorker(db, FakeExecutionAdapter(fail_buy='build'), worker_settings())
    assert await worker.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'BUY_FAILED'
    assert not await worker.buy_once(now=102)
    assert db.conn.execute("SELECT count(*) FROM orders WHERE side='BUY'").fetchone()[0] == 1


async def test_rpc_failure_before_signing_retries_same_buy_order_safely(db, valid_payload):
    accept(db, valid_payload)
    transient = FakeExecutionAdapter()
    transient.build_buy_transaction = AsyncMock(side_effect=RPCError('eth_estimateGas_UNAVAILABLE'))
    worker = TradingWorker(db, transient, worker_settings())

    assert await worker.buy_once(now=101)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'RECEIVED'
    order = db.conn.execute("SELECT * FROM orders WHERE side='BUY'").fetchone()
    assert order['status'] == 'CREATED' and order['tx_hash'] is None

    restarted = TradingWorker(db, FakeExecutionAdapter(), worker_settings())
    assert await restarted.buy_once(now=102)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'OPEN'
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


async def test_expired_unknown_buy_not_found_on_chain_is_closed_without_resubmission(
        db, valid_payload):
    accept(db, valid_payload, expires=110)
    tx_hash = '0x' + 'c' * 64
    adapter = FakeExecutionAdapter()
    adapter.submit_buy = AsyncMock(side_effect=SubmissionUnknown(tx_hash, 0))
    worker = TradingWorker(db, adapter, worker_settings())

    assert await worker.buy_once(now=101)
    adapter.receipt_by_hash = AsyncMock(return_value=None)
    adapter.transaction_by_hash = AsyncMock(return_value=None)
    adapter.reconcile_nonce = AsyncMock()

    assert await worker.buy_once(now=111)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'EXPIRED'
    order = db.conn.execute("SELECT * FROM orders WHERE side='BUY'").fetchone()
    assert order['status'] == 'FAILED' and order['error_code'] == 'BROADCAST_NOT_FOUND'
    adapter.submit_buy.assert_awaited_once()
    adapter.reconcile_nonce.assert_awaited_once_with()


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


async def test_zero_quote_never_consumes_terminal_sell_failure_budget(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter()
    adapter.quote_full_sell = AsyncMock(side_effect=ExecutionFailure('ZERO_QUOTE'))
    worker = TradingWorker(db, adapter, worker_settings(max_sell_attempts=1))
    await worker.buy_once(now=101)

    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    signal = db.conn.execute('SELECT status,last_error FROM signals').fetchone()
    assert tuple(signal) == ('OPEN', 'ZERO_QUOTE')
    retry = db.conn.execute(
        "SELECT status,error_code FROM execution_attempts WHERE side='SELL'").fetchone()
    assert tuple(retry) == ('RETRYABLE', 'ZERO_QUOTE')


async def test_transient_sell_quote_rpc_error_keeps_position_open(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter()
    adapter.quote_full_sell = AsyncMock(side_effect=RPCError('eth_call_UNAVAILABLE'))
    worker = TradingWorker(db, adapter, worker_settings(max_sell_attempts=1))
    await worker.buy_once(now=101)

    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute('SELECT last_error FROM signals').fetchone()[0] == 'RPC_TRANSIENT'
    assert db.conn.execute(
        "SELECT count(*) FROM execution_attempts WHERE side='SELL' AND status='FAILED'"
    ).fetchone()[0] == 0


async def test_zero_balance_reconciliation_closes_manually_sold_position(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(wallet_token_balance=Decimal(0))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)

    assert await worker.reconcile_positions_once(now=102) == 1
    position = db.conn.execute('SELECT status,closed_at FROM positions').fetchone()
    signal = db.conn.execute('SELECT status,last_error FROM signals').fetchone()
    reconciled = db.conn.execute(
        "SELECT status,error_code FROM execution_attempts WHERE side='RECONCILE'").fetchone()
    assert tuple(position) == ('CLOSED', 102)
    assert tuple(signal) == ('CLOSED', None)
    assert tuple(reconciled) == ('CONFIRMED', 'ONCHAIN_BALANCE_ZERO')


async def test_stuck_zero_quote_position_reopens_when_balance_remains(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(wallet_token_balance=Decimal(100))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    with db.conn:
        db.conn.execute("UPDATE positions SET status='POSITION_STUCK'")
        db.conn.execute("UPDATE signals SET status='POSITION_STUCK',last_error='ZERO_QUOTE'")

    assert await worker.reconcile_positions_once(now=102) == 1
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    signal = db.conn.execute('SELECT status,last_error FROM signals').fetchone()
    assert tuple(signal) == ('OPEN', None)


async def test_partial_manual_sell_is_detected_without_automatic_resale(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(wallet_token_balance=Decimal(50))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)

    assert await worker.reconcile_positions_once(now=102) == 1
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'POSITION_STUCK'
    assert db.conn.execute('SELECT last_error FROM signals').fetchone()[0] == 'MANUAL_BALANCE_MISMATCH'


async def test_reverted_sell_receipt_does_not_close_position(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('14'), fail_sell='receipt')
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'OPEN'


async def test_successful_sell_with_unparsed_proceeds_is_never_resubmitted(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('14'), sell_received=Decimal('0'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)

    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'POSITION_STUCK'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'POSITION_STUCK'
    order = db.conn.execute("SELECT * FROM orders WHERE side='SELL'").fetchone()
    assert order['status'] == 'CONFIRMED'
    assert order['error_code'] == 'ZERO_SELL_PROCEEDS'
    assert not await worker.sell_once(now=103)


async def test_ambiguous_sell_submission_recovers_without_second_sell(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('14'))
    worker = TradingWorker(db, adapter, worker_settings())
    await worker.buy_once(now=101)
    sell_hash = '0x' + 'c' * 64
    adapter.submit_sell = AsyncMock(side_effect=SubmissionUnknown(sell_hash, 2))

    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'SELL_SUBMITTED'
    order = db.conn.execute("SELECT * FROM orders WHERE side='SELL'").fetchone()
    assert order['status'] == 'UNKNOWN' and order['tx_hash'] == sell_hash

    adapter.wallet_token_balance = Decimal(0)
    assert await worker.reconcile_positions_once(now=102) == 0
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'

    restarted_adapter = FakeExecutionAdapter(sell_received=Decimal('14'))
    restarted_adapter.submit_sell = AsyncMock()
    restarted = TradingWorker(db, restarted_adapter, worker_settings())
    assert await restarted.sell_once(now=103)
    restarted_adapter.submit_sell.assert_not_awaited()
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'CLOSED'


async def test_pending_approval_does_not_consume_sell_failure_budget(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(sell_quote=Decimal('14'))
    worker = TradingWorker(db, adapter, worker_settings(max_sell_attempts=1))
    await worker.buy_once(now=101)
    adapter.ensure_token_approval = AsyncMock(
        side_effect=ExecutionFailure('APPROVAL_PENDING'))

    assert await worker.sell_once(now=102)
    assert db.conn.execute('SELECT status FROM positions').fetchone()[0] == 'OPEN'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'OPEN'
    assert db.conn.execute(
        "SELECT count(*) FROM execution_attempts WHERE side='SELL' AND status='FAILED'"
    ).fetchone()[0] == 0


async def test_below_target_position_does_not_starve_later_positions(db, valid_payload):
    first = deepcopy(valid_payload)
    second = deepcopy(valid_payload)
    second['signal_id'] = 'f' * 64
    second['event_id'] = 'e' * 64
    second['token_address'] = '0x' + '9' * 40
    accept(db, first)
    accept(db, second)
    for index, payload in enumerate((first, second), 1):
        open_position(db, payload['event_id'], payload['token_address'], 'USD', '10', '100',
                      '0x' + str(index) * 64, now=100 + index)
        with db.conn:
            db.conn.execute("UPDATE signals SET status='OPEN' WHERE event_id=?", (payload['event_id'],))
    adapter = FakeExecutionAdapter(sell_quote=Decimal('14'))
    adapter.quote_full_sell = AsyncMock(
        side_effect=lambda position: Decimal('12') if position['event_id'] == first['event_id']
        else Decimal('14'))
    worker = TradingWorker(db, adapter, worker_settings())

    assert not await worker.sell_once(now=200)
    assert await worker.sell_once(now=201)
    statuses = {row['event_id']: row['status'] for row in db.conn.execute('SELECT * FROM positions')}
    assert statuses[first['event_id']] == 'OPEN'
    assert statuses[second['event_id']] == 'CLOSED'


async def test_buy_finalization_rolls_back_as_one_transaction(db, valid_payload, monkeypatch):
    accept(db, valid_payload)
    worker = TradingWorker(db, FakeExecutionAdapter(), worker_settings())

    def crash(*_, **__):
        raise RuntimeError('simulated crash during finalization')

    monkeypatch.setattr('trader.worker.open_position', crash)
    with pytest.raises(RuntimeError, match='simulated crash'):
        await worker.buy_once(now=101)

    assert db.conn.execute("SELECT status FROM orders WHERE side='BUY'").fetchone()[0] == 'SUBMITTED'
    assert db.conn.execute('SELECT status FROM signals').fetchone()[0] == 'BUY_SUBMITTED'
    assert db.conn.execute('SELECT count(*) FROM positions').fetchone()[0] == 0


async def test_six_usd_buys_then_exactly_8_4_sells_full_position(db, valid_payload):
    accept(db, valid_payload)
    adapter = FakeExecutionAdapter(buy_received=Decimal('600'), sell_quote=Decimal('8.4'))
    first = TradingWorker(db, adapter, worker_settings(amount='6'))
    await first.buy_once(now=101)
    position = db.conn.execute('SELECT * FROM positions').fetchone()
    assert position['target_proceeds'] == '8.40'
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
