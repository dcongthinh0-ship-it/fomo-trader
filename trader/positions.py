import hashlib
import time
from decimal import Decimal


def open_position(db, event_id, token, input_asset, actual_cost, quantity, buy_tx_hash, now=None):
    now = int(now or time.time())
    cost, qty = Decimal(str(actual_cost)), Decimal(str(quantity))
    pid = hashlib.sha256(f'{event_id}:position'.encode()).hexdigest()
    target = cost * Decimal('1.30')
    with db.conn:
        db.conn.execute('INSERT OR IGNORE INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (pid, event_id, token.lower(), input_asset, str(cost), str(qty),
                         str(cost / qty) if qty else None, str(target), 'OPEN', buy_tx_hash, None,
                         now, None, now))
    return dict(db.conn.execute('SELECT * FROM positions WHERE event_id=?', (event_id,)).fetchone())


def close_position(db, event_id, tx_hash, now=None):
    now = int(now or time.time())
    with db.conn:
        db.conn.execute("UPDATE positions SET status='CLOSED',sell_tx_hash=?,closed_at=?,updated_at=? WHERE event_id=?",
                        (tx_hash, now, now, event_id))


def mark_stuck(db, event_id, now=None):
    with db.conn:
        db.conn.execute("UPDATE positions SET status='POSITION_STUCK',updated_at=? WHERE event_id=?",
                        (int(now or time.time()), event_id))
