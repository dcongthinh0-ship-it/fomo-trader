import asyncio
import logging
import os
import signal
import time

import aiohttp
from aiohttp import web

from .api import create_app
from .db import DB
from .execution import FakeExecutionAdapter
from .nonce import NonceManager
from .rpc import RPC, RPCError
from .settings import Settings
from .uniswap import UniswapRobinhoodExecutionAdapter
from .worker import TradingWorker


async def run_worker_after_nonce_ready(worker, nonce, db, retry_delay=1, max_retry_delay=15):
    delay = retry_delay
    while True:
        try:
            await nonce.reconcile()
            await worker.reconcile_positions_once()
            with db.conn:
                db.set_state('worker_startup_pending', False)
                db.set_state('worker_last_error', None)
            return await worker.run()
        except asyncio.CancelledError:
            raise
        except RPCError as exc:
            with db.conn:
                db.set_state('worker_startup_pending', True)
                db.set_state('worker_last_error', {
                    'type': type(exc).__name__, 'phase': 'startup_reconcile',
                    'at': int(time.time()),
                })
            logging.getLogger(__name__).warning(
                'startup reconciliation retry type=%s delay=%s', type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_retry_delay)


async def serve():
    settings = Settings()
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings.shared_secret()  # Fail at startup, never accept unauthenticated signals.
    db = DB(settings.database)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8)) as session:
        rpc = RPC(session, settings.rpc_url, settings.rpc_rps,
                  max_in_flight=settings.rpc_max_in_flight,
                  send_url=settings.rpc_send_url) if settings.rpc_url else None
        if settings.adapter == 'fake':
            if settings.live:
                raise ValueError('fake adapter cannot run with live trading enabled')
            adapter = FakeExecutionAdapter()
            nonce = None
        elif settings.adapter == 'uniswap':
            if rpc is None:
                raise ValueError('uniswap adapter requires a configured trading RPC endpoint')
            nonce = NonceManager(db, rpc, settings.wallet_address)
            adapter = UniswapRobinhoodExecutionAdapter(
                db, rpc, nonce, settings)
        else:
            raise ValueError('unknown execution adapter')
        worker = TradingWorker(db, adapter, settings)
        with db.conn:
            db.set_state('worker_startup_pending', nonce is not None)
            db.set_state('worker_heartbeat_at', None)
            db.set_state('worker_last_error', None)
        app = create_app(db, settings, rpc)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', settings.health_port).start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        task = asyncio.create_task(
            run_worker_after_nonce_ready(worker, nonce, db) if nonce else worker.run())
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([task, stopper], return_when=asyncio.FIRST_COMPLETED)
        failure = next((item.exception() for item in done if item is task and item.exception()), None)
        task.cancel()
        stopper.cancel()
        await asyncio.gather(task, stopper, return_exceptions=True)
        await runner.cleanup()
        db.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.conn.close()
        if failure:
            raise failure


def main():
    try:
        asyncio.run(serve())
    except Exception as exc:
        logging.getLogger(__name__).error('fatal startup/runtime error type=%s', type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
