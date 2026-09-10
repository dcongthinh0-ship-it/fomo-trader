import asyncio
import time


class NonceManager:
    def __init__(self, db, rpc, wallet_address):
        self.db, self.rpc, self.wallet = db, rpc, wallet_address.lower()
        self.lock = asyncio.Lock()

    async def reserve(self):
        async with self.lock:
            pending = await self.rpc.transaction_count(self.wallet, 'pending')
            row = self.db.conn.execute('SELECT next_nonce FROM nonce_state WHERE wallet_address=?',
                                       (self.wallet,)).fetchone()
            nonce = max(pending, row['next_nonce'] if row and row['next_nonce'] is not None else pending)
            with self.db.conn:
                self.db.conn.execute('INSERT OR REPLACE INTO nonce_state VALUES(?,?,?)',
                                     (self.wallet, nonce + 1, int(time.time())))
            return nonce

    async def reconcile(self):
        async with self.lock:
            pending = await self.rpc.transaction_count(self.wallet, 'pending')
            with self.db.conn:
                self.db.conn.execute('INSERT OR REPLACE INTO nonce_state VALUES(?,?,?)',
                                     (self.wallet, pending, int(time.time())))
            return pending
