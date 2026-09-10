import hashlib
import time


def order_id(event_id, side):
    return hashlib.sha256(f'{event_id}:{side}'.encode()).hexdigest()


def create_order(db, event_id, side, input_asset, input_amount, expected, minimum, now=None):
    now = int(now or time.time())
    oid = order_id(event_id, side)
    with db.conn:
        db.conn.execute('INSERT OR IGNORE INTO orders('
                        'id,event_id,side,input_asset,input_amount,expected_output,minimum_output,status,created_at,updated_at'
                        ") VALUES(?,?,?,?,?,?,?,'CREATED',?,?)",
                        (oid, event_id, side, input_asset, str(input_amount), str(expected), str(minimum), now, now))
    return dict(db.conn.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())


def update_order(db, event_id, side, status, now=None, **fields):
    values = {'status': status, 'updated_at': int(now or time.time()), **fields}
    with db.conn:
        db.conn.execute('UPDATE orders SET ' + ','.join(f'{key}=?' for key in values) +
                        ' WHERE event_id=? AND side=?', (*values.values(), event_id, side))


def attempt(db, event_id, side, status, now=None, **facts):
    now = int(now or time.time())
    sequence = db.conn.execute('SELECT count(*) FROM execution_attempts WHERE event_id=? AND side=?',
                               (event_id, side)).fetchone()[0]
    aid = hashlib.sha256(f'{event_id}:{side}:{now}:{status}:{sequence}'.encode()).hexdigest()
    with db.conn:
        db.conn.execute('INSERT OR REPLACE INTO execution_attempts VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (aid, event_id, side, facts.get('tx_hash'), facts.get('nonce'),
                         facts.get('request_facts'), facts.get('response_facts'), status,
                         facts.get('error_code'), now))
    return aid
