import asyncio
import json
import logging
import time
from decimal import Decimal

from .db import dumps
from .execution import ExecutionFailure, SubmissionUnknown
from .models import TradeSignal
from .orders import attempt, create_order, update_order
from .positions import close_position, mark_stuck, open_position
from .rpc import RPCError

log = logging.getLogger(__name__)


class TradingWorker:
    def __init__(self, db, adapter, settings):
        self.db, self.adapter, self.settings = db, adapter, settings
        self._last_heartbeat = 0
        self._last_position_reconcile = 0

    def _signal(self, row):
        payload = json.loads(row['payload'])
        return TradeSignal(row['signal_id'], row['event_id'], 4663, payload['token_address'],
                           row['expires_at'], payload)

    @staticmethod
    def _quote_state_key(event_id):
        return f'position_quote:{event_id}'

    @staticmethod
    def _emergency_state_key(event_id):
        return f'emergency_exit:{event_id}'

    def _clear_exit_tracking(self, event_id):
        self.db.set_state(self._quote_state_key(event_id), None)
        self.db.set_state(self._emergency_state_key(event_id), None)

    def _finalize_buy(self, signal, quantity, tx_hash, nonce, receipt, now):
        with self.db.conn:
            update_order(self.db, signal.event_id, 'BUY', 'CONFIRMED', now, commit=False,
                         actual_output=str(quantity))
            attempt(self.db, signal.event_id, 'BUY', 'CONFIRMED', now, commit=False,
                    tx_hash=tx_hash, nonce=nonce, response_facts=dumps(receipt))
            open_position(self.db, signal.event_id, signal.token_address, self.settings.amount_mode,
                          self.settings.amount, quantity, tx_hash, now, commit=False)
            self.db.conn.execute(
                "UPDATE signals SET status='OPEN',last_error=NULL WHERE event_id=?",
                (signal.event_id,))
            self.db.set_state('last_processed_signal_at', now)

    def _finalize_sell(self, event_id, proceeds, tx_hash, nonce, receipt, now):
        with self.db.conn:
            update_order(self.db, event_id, 'SELL', 'CONFIRMED', now, commit=False,
                         actual_output=str(proceeds), error_code=None)
            attempt(self.db, event_id, 'SELL', 'CONFIRMED', now, commit=False,
                    tx_hash=tx_hash, nonce=nonce, response_facts=dumps(receipt))
            close_position(self.db, event_id, tx_hash, now, commit=False)
            self.db.conn.execute(
                "UPDATE signals SET status='CLOSED',last_error=NULL WHERE event_id=?", (event_id,))
            self._clear_exit_tracking(event_id)

    def _mark_sell_output_unknown(self, event_id, tx_hash, now):
        with self.db.conn:
            update_order(self.db, event_id, 'SELL', 'CONFIRMED', now, commit=False,
                         error_code='ZERO_SELL_PROCEEDS')
            self.db.conn.execute(
                "UPDATE positions SET status='POSITION_STUCK',sell_tx_hash=?,updated_at=? "
                'WHERE event_id=?', (tx_hash, now, event_id))
            self.db.conn.execute(
                "UPDATE signals SET status='POSITION_STUCK',last_error='ZERO_SELL_PROCEEDS' "
                'WHERE event_id=?', (event_id,))

    async def reconcile_positions_once(self, now=None):
        now = int(now or time.time())
        rows = self.db.conn.execute(
            "SELECT p.*,s.status AS signal_status,s.last_error FROM positions p "
            "JOIN signals s ON s.event_id=p.event_id "
            "WHERE p.status IN ('OPEN','POSITION_STUCK') "
            "AND s.status NOT IN ('SELL_PENDING','SELL_SUBMITTED') "
            "AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.event_id=p.event_id "
            "AND o.side='SELL' AND o.status IN ('SIGNED','SUBMITTED','UNKNOWN')) "
            'ORDER BY p.updated_at,p.opened_at'
        ).fetchall()
        changed = 0
        for row in rows:
            position = dict(row)
            balance = Decimal(str(await self.adapter.token_balance(position['token_address'])))
            tracked = Decimal(position['token_quantity'])
            request = dumps({'tracked_quantity': tracked, 'position_status': position['status']})
            response = dumps({'onchain_balance': balance})
            if balance == 0:
                with self.db.conn:
                    self.db.conn.execute(
                        "UPDATE positions SET status='CLOSED',closed_at=?,updated_at=? WHERE event_id=?",
                        (now, now, position['event_id']))
                    self.db.conn.execute(
                        "UPDATE signals SET status='CLOSED',last_error=NULL WHERE event_id=?",
                        (position['event_id'],))
                    attempt(self.db, position['event_id'], 'RECONCILE', 'CONFIRMED', now,
                            commit=False, request_facts=request, response_facts=response,
                            error_code='ONCHAIN_BALANCE_ZERO')
                    self._clear_exit_tracking(position['event_id'])
                changed += 1
            elif position['status'] == 'POSITION_STUCK' and position['last_error'] == 'ZERO_QUOTE':
                with self.db.conn:
                    self.db.conn.execute(
                        "UPDATE positions SET status='OPEN',updated_at=? WHERE event_id=?",
                        (now, position['event_id']))
                    self.db.conn.execute(
                        "UPDATE signals SET status='OPEN',last_error=NULL WHERE event_id=?",
                        (position['event_id'],))
                    attempt(self.db, position['event_id'], 'RECONCILE', 'REOPENED', now,
                            commit=False, request_facts=request, response_facts=response)
                changed += 1
            elif 0 < balance < tracked and position['last_error'] != 'MANUAL_BALANCE_MISMATCH':
                with self.db.conn:
                    self.db.conn.execute(
                        "UPDATE positions SET status='POSITION_STUCK',updated_at=? WHERE event_id=?",
                        (now, position['event_id']))
                    self.db.conn.execute(
                        "UPDATE signals SET status='POSITION_STUCK',last_error=? WHERE event_id=?",
                        ('MANUAL_BALANCE_MISMATCH', position['event_id']))
                    attempt(self.db, position['event_id'], 'RECONCILE', 'STUCK', now,
                            commit=False, request_facts=request, response_facts=response,
                            error_code='MANUAL_BALANCE_MISMATCH')
                changed += 1
        return changed

    async def buy_once(self, now=None):
        now = int(now or time.time())
        recovering = self.db.conn.execute(
            "SELECT event_id FROM signals WHERE status IN ('BUY_PENDING','BUY_SUBMITTED') "
            'ORDER BY received_at LIMIT 1').fetchone()
        if recovering:
            return await self.recover_submitted(recovering['event_id'], 'BUY')
        row = self.db.conn.execute("SELECT * FROM signals WHERE status='RECEIVED' ORDER BY received_at LIMIT 1").fetchone()
        if not row or not self.settings.live:
            return False
        if row['expires_at'] <= now:
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='EXPIRED',last_error='SIGNAL_EXPIRED' WHERE event_id=?",
                                     (row['event_id'],))
            return True
        active_positions = self.db.conn.execute(
            "SELECT count(*) FROM positions WHERE status!='CLOSED'").fetchone()[0]
        if active_positions >= self.settings.max_open_positions:
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE signals SET status='SKIPPED',last_error='MAX_OPEN_POSITIONS' "
                    'WHERE event_id=?', (row['event_id'],))
            return False
        existing = self.db.conn.execute(
            "SELECT * FROM orders WHERE event_id=? AND side='BUY'", (row['event_id'],)).fetchone()
        if existing and existing['status'] != 'CREATED':
            return await self.recover_submitted(row['event_id'], 'BUY')
        signal = self._signal(row)
        try:
            pool = await self.adapter.resolve_pool(signal)
            expected = Decimal(str(await self.adapter.quote_buy(signal, pool, self.settings.amount)))
            minimum = expected * (Decimal(10000 - self.settings.buy_slippage_bps) / Decimal(10000))
            if minimum <= 0:
                raise ExecutionFailure('INVALID_MINIMUM_OUTPUT')
            create_order(self.db, signal.event_id, 'BUY', self.settings.amount_mode,
                         self.settings.amount, expected, minimum, now)
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='BUY_PENDING' WHERE event_id=?", (signal.event_id,))
            transaction = await self.adapter.build_buy_transaction(signal, pool, self.settings.amount, minimum)
            result = await self.adapter.submit_buy(transaction)
            update_order(self.db, signal.event_id, 'BUY', 'SUBMITTED', now,
                         tx_hash=result['tx_hash'], nonce=result['nonce'])
            attempt(self.db, signal.event_id, 'BUY', 'SUBMITTED', now,
                    tx_hash=result['tx_hash'], nonce=result['nonce'])
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='BUY_SUBMITTED' WHERE event_id=?", (signal.event_id,))
            receipt = await self.adapter.wait_for_receipt(result['tx_hash'])
            if int(receipt.get('status', '0x0'), 16) != 1:
                attempt(self.db, signal.event_id, 'BUY', 'REVERTED', now,
                        tx_hash=result['tx_hash'], nonce=result['nonce'], response_facts=dumps(receipt),
                        error_code='BUY_REVERTED')
                raise ExecutionFailure('BUY_REVERTED')
            quantity = Decimal(str(self.adapter.parse_actual_token_received(
                receipt, signal.token_address, getattr(self.settings, 'wallet_address', ''))))
            if quantity <= 0:
                raise ExecutionFailure('ZERO_TOKEN_RECEIVED')
            self._finalize_buy(signal, quantity, result['tx_hash'], result['nonce'], receipt, now)
        except SubmissionUnknown as exc:
            update_order(self.db, signal.event_id, 'BUY', 'UNKNOWN', now,
                         tx_hash=exc.tx_hash, nonce=exc.nonce, error_code=exc.code)
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='BUY_SUBMITTED',last_error=? WHERE event_id=?",
                                     (exc.code, signal.event_id))
        except TimeoutError:
            update_order(self.db, signal.event_id, 'BUY', 'UNKNOWN', now, error_code='RECEIPT_PENDING')
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='BUY_SUBMITTED',last_error='RECEIPT_PENDING' "
                                     'WHERE event_id=?', (signal.event_id,))
        except RPCError:
            order = self.db.conn.execute(
                "SELECT * FROM orders WHERE event_id=? AND side='BUY'", (signal.event_id,)).fetchone()
            if order and order['tx_hash']:
                update_order(self.db, signal.event_id, 'BUY', 'UNKNOWN', now,
                             error_code='RPC_TRANSIENT')
                status = 'BUY_SUBMITTED'
            else:
                status = 'RECEIVED'
            with self.db.conn:
                self.db.conn.execute(
                    'UPDATE signals SET status=?,last_error=? WHERE event_id=?',
                    (status, 'RPC_TRANSIENT', signal.event_id))
        except ExecutionFailure as exc:
            if exc.code == 'APPROVAL_PENDING':
                with self.db.conn:
                    self.db.conn.execute(
                        "UPDATE signals SET status='RECEIVED',last_error=? WHERE event_id=?",
                        (exc.code, signal.event_id))
                return True
            status = 'REVERTED' if exc.code == 'BUY_REVERTED' else 'FAILED'
            if self.db.conn.execute("SELECT 1 FROM orders WHERE event_id=? AND side='BUY'",
                                    (signal.event_id,)).fetchone():
                update_order(self.db, signal.event_id, 'BUY', status, now, error_code=exc.code)
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='BUY_FAILED',last_error=? WHERE event_id=?",
                                     (exc.code, signal.event_id))
            attempt(self.db, signal.event_id, 'BUY', status, now, error_code=exc.code)
        return True

    async def recover_submitted(self, event_id, side, now=None):
        now = int(now or time.time())
        order = self.db.conn.execute('SELECT * FROM orders WHERE event_id=? AND side=?', (event_id, side)).fetchone()
        if order and order['status'] == 'CREATED' and not order['tx_hash']:
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE signals SET status=?,last_error='PRE_SUBMISSION_RETRY' WHERE event_id=?",
                    ('RECEIVED' if side == 'BUY' else 'OPEN', event_id))
                if side == 'SELL':
                    self.db.conn.execute(
                        "UPDATE positions SET updated_at=? WHERE event_id=?", (int(time.time()), event_id))
            return True
        if not order or not order['tx_hash'] or order['status'] not in ('SIGNED', 'SUBMITTED', 'UNKNOWN'):
            return False
        receipt = await self.adapter.receipt_by_hash(order['tx_hash'])
        if not receipt:
            signal = self.db.conn.execute(
                'SELECT expires_at FROM signals WHERE event_id=?', (event_id,)).fetchone()
            if side == 'BUY' and signal and signal['expires_at'] <= now:
                transaction = await self.adapter.transaction_by_hash(order['tx_hash'])
                if transaction:
                    return False
                with self.db.conn:
                    update_order(self.db, event_id, side, 'FAILED', now, commit=False,
                                 error_code='BROADCAST_NOT_FOUND')
                    self.db.conn.execute(
                        "UPDATE signals SET status='EXPIRED',last_error='BROADCAST_NOT_FOUND' "
                        'WHERE event_id=?', (event_id,))
                await self.adapter.reconcile_nonce()
                return True
            return False
        if int(receipt.get('status', '0x0'), 16) != 1:
            update_order(self.db, event_id, side, 'REVERTED', error_code=f'{side}_REVERTED')
            with self.db.conn:
                self.db.conn.execute('UPDATE signals SET status=?,last_error=? WHERE event_id=?',
                                     ('BUY_FAILED' if side == 'BUY' else 'OPEN', f'{side}_REVERTED', event_id))
            return True
        if side == 'BUY':
            signal_row = self.db.conn.execute('SELECT * FROM signals WHERE event_id=?', (event_id,)).fetchone()
            signal = self._signal(signal_row)
            quantity = Decimal(str(self.adapter.parse_actual_token_received(
                receipt, signal.token_address, getattr(self.settings, 'wallet_address', ''))))
            if quantity <= 0:
                with self.db.conn:
                    update_order(self.db, event_id, 'BUY', 'CONFIRMED', commit=False,
                                 error_code='ZERO_TOKEN_RECEIVED')
                    self.db.conn.execute(
                        "UPDATE signals SET status='BUY_FAILED',last_error='ZERO_TOKEN_RECEIVED' "
                        'WHERE event_id=?', (event_id,))
                return True
            self._finalize_buy(signal, quantity, order['tx_hash'], order['nonce'], receipt, int(time.time()))
        else:
            position = self.db.conn.execute('SELECT * FROM positions WHERE event_id=?', (event_id,)).fetchone()
            if not position:
                with self.db.conn:
                    update_order(self.db, event_id, 'SELL', 'CONFIRMED', commit=False,
                                 error_code='POSITION_MISSING')
                    self.db.conn.execute(
                        "UPDATE signals SET status='POSITION_STUCK',last_error='POSITION_MISSING' "
                        'WHERE event_id=?', (event_id,))
                return True
            proceeds = await self.adapter.parse_actual_sell_proceeds(receipt, dict(position))
            now = int(time.time())
            if proceeds <= 0:
                self._mark_sell_output_unknown(event_id, order['tx_hash'], now)
                return True
            self._finalize_sell(event_id, proceeds, order['tx_hash'], order['nonce'], receipt, now)
        return True

    async def sell_once(self, now=None):
        now = int(now or time.time())
        if not self.settings.live:
            return False
        recovering = self.db.conn.execute(
            "SELECT event_id FROM signals WHERE status IN ('SELL_PENDING','SELL_SUBMITTED') "
            'ORDER BY received_at LIMIT 1').fetchone()
        if recovering:
            return await self.recover_submitted(recovering['event_id'], 'SELL')
        position = self.db.conn.execute(
            "SELECT * FROM positions WHERE status='OPEN' ORDER BY updated_at,opened_at LIMIT 1").fetchone()
        if not position:
            return False
        position = dict(position)
        try:
            quote = Decimal(str(await self.adapter.quote_full_sell(position)))
            cost = Decimal(position['actual_cost'])
            target = Decimal(position['target_proceeds'])
            previous_state = self.db.state(self._quote_state_key(position['event_id'])) or {}
            previous_quote = Decimal(previous_state['quote']) if previous_state.get('quote') else None
            emergency = self.db.state(self._emergency_state_key(position['event_id'])) or {}
            remaining = (
                Decimal(100) - self.settings.crash_sell_drop_pct) / Decimal(100)
            crash_from_entry = quote <= cost * remaining
            crash_from_previous = previous_quote is not None and quote <= previous_quote * remaining
            crash_reason = emergency.get('reason')
            if not crash_reason and crash_from_entry:
                crash_reason = 'CRASH_FROM_ENTRY_90_PCT'
            if not crash_reason and crash_from_previous:
                crash_reason = 'CRASH_FROM_PREVIOUS_QUOTE_90_PCT'
            with self.db.conn:
                self.db.set_state(self._quote_state_key(position['event_id']), {
                    'quote': str(quote), 'at': now,
                })
                if crash_reason and not emergency:
                    self.db.set_state(self._emergency_state_key(position['event_id']), {
                        'reason': crash_reason, 'trigger_quote': str(quote), 'at': now,
                    })
                if not crash_reason and quote < target:
                    self.db.conn.execute(
                        "UPDATE signals SET last_error=NULL WHERE event_id=?",
                        (position['event_id'],))
            if not crash_reason and quote < target:
                with self.db.conn:
                    self.db.conn.execute(
                        'UPDATE positions SET updated_at=? WHERE event_id=?',
                        (now, position['event_id']))
                return False
            slippage_bps = (self.settings.crash_sell_slippage_bps
                            if crash_reason else self.settings.sell_slippage_bps)
            trigger_reason = crash_reason or 'TAKE_PROFIT_40_PCT'
            minimum = quote * (Decimal(10000 - slippage_bps) / Decimal(10000))
            order = create_order(self.db, position['event_id'], 'SELL', position['token_address'],
                                 position['token_quantity'], quote, minimum, now)
            if order['status'] in ('SIGNED', 'SUBMITTED', 'UNKNOWN'):
                return await self.recover_submitted(position['event_id'], 'SELL')
            update_order(self.db, position['event_id'], 'SELL', 'CREATED', now,
                         expected_output=str(quote), minimum_output=str(minimum), error_code=None)
            attempt(self.db, position['event_id'], 'SELL', 'TRIGGERED', now,
                    request_facts=dumps({
                        'reason': trigger_reason, 'quote': quote, 'previous_quote': previous_quote,
                        'actual_cost': cost, 'target_proceeds': target,
                        'slippage_bps': slippage_bps,
                    }))
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE signals SET status='SELL_PENDING',last_error=NULL WHERE event_id=?",
                    (position['event_id'],))
            await self.adapter.ensure_token_approval(position)
            transaction = await self.adapter.build_sell_transaction(position, minimum)
            result = await self.adapter.submit_sell(transaction)
            update_order(self.db, position['event_id'], 'SELL', 'SUBMITTED', now,
                         tx_hash=result['tx_hash'], nonce=result['nonce'])
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='SELL_SUBMITTED' WHERE event_id=?",
                                     (position['event_id'],))
            receipt = await self.adapter.wait_for_receipt(result['tx_hash'])
            if int(receipt.get('status', '0x0'), 16) != 1:
                attempt(self.db, position['event_id'], 'SELL', 'REVERTED', now,
                        tx_hash=result['tx_hash'], nonce=result['nonce'], response_facts=dumps(receipt),
                        error_code='SELL_REVERTED')
                raise ExecutionFailure('SELL_REVERTED')
            proceeds = await self.adapter.parse_actual_sell_proceeds(receipt, position)
            if proceeds <= 0:
                self._mark_sell_output_unknown(position['event_id'], result['tx_hash'], now)
                return True
            self._finalize_sell(position['event_id'], proceeds, result['tx_hash'], result['nonce'],
                                receipt, now)
        except SubmissionUnknown as exc:
            update_order(self.db, position['event_id'], 'SELL', 'UNKNOWN', now,
                         tx_hash=exc.tx_hash, nonce=exc.nonce, error_code=exc.code)
            with self.db.conn:
                self.db.conn.execute(
                    "UPDATE signals SET status='SELL_SUBMITTED',last_error=? WHERE event_id=?",
                    (exc.code, position['event_id']))
        except ExecutionFailure as exc:
            if exc.code in ('APPROVAL_PENDING', 'ZERO_QUOTE'):
                with self.db.conn:
                    self.db.conn.execute(
                        "UPDATE signals SET status='OPEN',last_error=? WHERE event_id=?",
                        (exc.code, position['event_id']))
                    self.db.conn.execute(
                        'UPDATE positions SET updated_at=? WHERE event_id=?',
                        (now, position['event_id']))
                    if exc.code == 'ZERO_QUOTE':
                        attempt(self.db, position['event_id'], 'SELL', 'RETRYABLE', now,
                                commit=False, error_code=exc.code)
                return True
            attempt(self.db, position['event_id'], 'SELL', 'FAILED', now, error_code=exc.code)
            failures = self.db.conn.execute(
                "SELECT count(*) FROM execution_attempts WHERE event_id=? AND side='SELL' AND status='FAILED'",
                (position['event_id'],)).fetchone()[0]
            if failures >= self.settings.max_sell_attempts:
                mark_stuck(self.db, position['event_id'], now)
                signal_status = 'POSITION_STUCK'
            else:
                signal_status = 'OPEN'
                with self.db.conn:
                    self.db.conn.execute(
                        'UPDATE positions SET updated_at=? WHERE event_id=?',
                        (now, position['event_id']))
            if self.db.conn.execute("SELECT 1 FROM orders WHERE event_id=? AND side='SELL'",
                                    (position['event_id'],)).fetchone():
                update_order(self.db, position['event_id'], 'SELL', 'FAILED', now, error_code=exc.code)
            with self.db.conn:
                self.db.conn.execute('UPDATE signals SET status=?,last_error=? WHERE event_id=?',
                                     (signal_status, exc.code, position['event_id']))
        except TimeoutError:
            update_order(self.db, position['event_id'], 'SELL', 'UNKNOWN', now, error_code='RECEIPT_PENDING')
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='SELL_SUBMITTED',last_error='RECEIPT_PENDING' "
                                     'WHERE event_id=?', (position['event_id'],))
        except RPCError:
            order = self.db.conn.execute(
                "SELECT * FROM orders WHERE event_id=? AND side='SELL'",
                (position['event_id'],)).fetchone()
            if order and order['tx_hash']:
                update_order(self.db, position['event_id'], 'SELL', 'UNKNOWN', now,
                             error_code='RPC_TRANSIENT')
                status = 'SELL_SUBMITTED'
            else:
                status = 'OPEN'
                with self.db.conn:
                    self.db.conn.execute(
                        'UPDATE positions SET updated_at=? WHERE event_id=?',
                        (now, position['event_id']))
            with self.db.conn:
                self.db.conn.execute(
                    'UPDATE signals SET status=?,last_error=? WHERE event_id=?',
                    (status, 'RPC_TRANSIENT', position['event_id']))
        return True

    async def run(self):
        while True:
            now = int(time.time())
            if now - self._last_heartbeat >= 5:
                with self.db.conn:
                    self.db.set_state('worker_heartbeat_at', now)
                self._last_heartbeat = now
            try:
                if now - self._last_position_reconcile >= self.settings.position_reconcile_seconds:
                    self._last_position_reconcile = now
                    await self.reconcile_positions_once(now)
                found = await self.buy_once() or await self.sell_once()
            except Exception as exc:
                found = False
                with self.db.conn:
                    self.db.set_state('worker_last_error', {
                        'type': type(exc).__name__, 'at': int(time.time())})
                log.warning('worker retry type=%s', type(exc).__name__)
            await asyncio.sleep(0.2 if found else self.settings.price_poll_seconds)
