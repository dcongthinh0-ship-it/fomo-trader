import asyncio
import json
import logging
import time
from decimal import Decimal

from .execution import ExecutionFailure, SubmissionUnknown
from .models import TradeSignal
from .orders import attempt, create_order, update_order
from .positions import close_position, mark_stuck, open_position

log = logging.getLogger(__name__)


class TradingWorker:
    def __init__(self, db, adapter, settings):
        self.db, self.adapter, self.settings = db, adapter, settings

    def _signal(self, row):
        payload = json.loads(row['payload'])
        return TradeSignal(row['signal_id'], row['event_id'], 4663, payload['token_address'],
                           row['expires_at'], payload)

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
        if self.db.conn.execute("SELECT 1 FROM orders WHERE event_id=? AND side='BUY'", (row['event_id'],)).fetchone():
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
                raise ExecutionFailure('BUY_REVERTED')
            quantity = Decimal(str(self.adapter.parse_actual_token_received(
                receipt, signal.token_address, getattr(self.settings, 'wallet_address', ''))))
            if quantity <= 0:
                raise ExecutionFailure('ZERO_TOKEN_RECEIVED')
            update_order(self.db, signal.event_id, 'BUY', 'CONFIRMED', now, actual_output=str(quantity))
            open_position(self.db, signal.event_id, signal.token_address, self.settings.amount_mode,
                          self.settings.amount, quantity, result['tx_hash'], now)
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='OPEN',last_error=NULL WHERE event_id=?",
                                     (signal.event_id,))
                self.db.set_state('last_processed_signal_at', now)
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
        except ExecutionFailure as exc:
            status = 'REVERTED' if exc.code == 'BUY_REVERTED' else 'FAILED'
            if self.db.conn.execute("SELECT 1 FROM orders WHERE event_id=? AND side='BUY'",
                                    (signal.event_id,)).fetchone():
                update_order(self.db, signal.event_id, 'BUY', status, now, error_code=exc.code)
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='BUY_FAILED',last_error=? WHERE event_id=?",
                                     (exc.code, signal.event_id))
            attempt(self.db, signal.event_id, 'BUY', status, now, error_code=exc.code)
        return True

    async def recover_submitted(self, event_id, side):
        order = self.db.conn.execute('SELECT * FROM orders WHERE event_id=? AND side=?', (event_id, side)).fetchone()
        if not order or not order['tx_hash'] or order['status'] not in ('SIGNED', 'SUBMITTED', 'UNKNOWN'):
            return False
        receipt = await self.adapter.receipt_by_hash(order['tx_hash'])
        if not receipt:
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
                return False
            update_order(self.db, event_id, 'BUY', 'CONFIRMED', actual_output=str(quantity))
            open_position(self.db, event_id, signal.token_address, self.settings.amount_mode,
                          self.settings.amount, quantity, order['tx_hash'])
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='OPEN',last_error=NULL WHERE event_id=?", (event_id,))
        else:
            position = self.db.conn.execute('SELECT * FROM positions WHERE event_id=?', (event_id,)).fetchone()
            if not position:
                return False
            proceeds = self.adapter.parse_actual_sell_proceeds(receipt, dict(position))
            update_order(self.db, event_id, 'SELL', 'CONFIRMED', actual_output=str(proceeds))
            close_position(self.db, event_id, order['tx_hash'])
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='CLOSED',last_error=NULL WHERE event_id=?", (event_id,))
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
        position = self.db.conn.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at LIMIT 1").fetchone()
        if not position:
            return False
        position = dict(position)
        try:
            quote = Decimal(str(await self.adapter.quote_full_sell(position)))
            if quote < Decimal(position['target_proceeds']):
                return False
            minimum = quote * (Decimal(10000 - self.settings.sell_slippage_bps) / Decimal(10000))
            order = create_order(self.db, position['event_id'], 'SELL', position['token_address'],
                                 position['token_quantity'], quote, minimum, now)
            if order['status'] in ('SUBMITTED', 'UNKNOWN'):
                return await self.recover_submitted(position['event_id'], 'SELL')
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='SELL_PENDING' WHERE event_id=?",
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
                raise ExecutionFailure('SELL_REVERTED')
            proceeds = self.adapter.parse_actual_sell_proceeds(receipt, position)
            update_order(self.db, position['event_id'], 'SELL', 'CONFIRMED', now,
                         actual_output=str(proceeds))
            close_position(self.db, position['event_id'], result['tx_hash'], now)
            with self.db.conn:
                self.db.conn.execute("UPDATE signals SET status='CLOSED',last_error=NULL WHERE event_id=?",
                                     (position['event_id'],))
        except ExecutionFailure as exc:
            attempt(self.db, position['event_id'], 'SELL', 'FAILED', now, error_code=exc.code)
            failures = self.db.conn.execute(
                "SELECT count(*) FROM execution_attempts WHERE event_id=? AND side='SELL' AND status='FAILED'",
                (position['event_id'],)).fetchone()[0]
            if failures >= self.settings.max_sell_attempts:
                mark_stuck(self.db, position['event_id'], now)
                signal_status = 'POSITION_STUCK'
            else:
                signal_status = 'OPEN'
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
        return True

    async def run(self):
        while True:
            try:
                found = await self.buy_once() or await self.sell_once()
            except Exception as exc:
                found = False
                log.warning('worker retry type=%s', type(exc).__name__)
            await asyncio.sleep(0.2 if found else self.settings.price_poll_seconds)
