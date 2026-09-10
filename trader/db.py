import json
import sqlite3
import time
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS signals(signal_id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,
 payload TEXT NOT NULL,received_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,status TEXT NOT NULL,last_error TEXT);
CREATE INDEX IF NOT EXISTS signals_due ON signals(status,received_at,expires_at);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,side TEXT NOT NULL,
 input_asset TEXT,input_amount TEXT,expected_output TEXT,minimum_output TEXT,actual_output TEXT,
 tx_hash TEXT UNIQUE,nonce INTEGER,status TEXT NOT NULL,error_code TEXT,created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL,UNIQUE(event_id,side));
CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,
 token_address TEXT NOT NULL,input_asset TEXT NOT NULL,actual_cost TEXT,token_quantity TEXT,
 average_entry TEXT,target_proceeds TEXT,status TEXT NOT NULL,buy_tx_hash TEXT,sell_tx_hash TEXT,
 opened_at INTEGER,closed_at INTEGER,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS execution_attempts(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,side TEXT NOT NULL,
 tx_hash TEXT,nonce INTEGER,request_facts TEXT,response_facts TEXT,status TEXT NOT NULL,error_code TEXT,
 created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS nonce_state(wallet_address TEXT PRIMARY KEY,next_nonce INTEGER,updated_at INTEGER);
CREATE TABLE IF NOT EXISTS system_state(key TEXT PRIMARY KEY,value TEXT);
PRAGMA user_version=1;
'''


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


class DB:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        if self.conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise sqlite3.DatabaseError('SQLite integrity check failed')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA busy_timeout=15000')
        self.conn.executescript(SCHEMA)

    def accept_signal(self, signal, now=None):
        now = int(now or time.time())
        payload = dumps(signal.payload)
        with self.conn:
            existing = self.conn.execute('SELECT signal_id,status FROM signals WHERE event_id=?',
                                         (signal.event_id,)).fetchone()
            if existing:
                return 'duplicate', existing['signal_id']
            self.conn.execute('INSERT INTO signals VALUES(?,?,?,?,?,?,NULL)',
                              (signal.signal_id, signal.event_id, payload, now, signal.expires_at, 'RECEIVED'))
        return 'accepted', signal.signal_id

    def state(self, key, default=None):
        row = self.conn.execute('SELECT value FROM system_state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        self.conn.execute('INSERT OR REPLACE INTO system_state VALUES(?,?)', (key, dumps(value)))
