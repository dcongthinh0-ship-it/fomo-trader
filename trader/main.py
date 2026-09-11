import asyncio
import logging
import os
import signal

import aiohttp
from aiohttp import web

from .api import create_app
from .db import DB
from .execution import FakeExecutionAdapter
from .nonce import NonceManager
from .rpc import RPC
from .settings import Settings
from .uniswap import UniswapRobinhoodExecutionAdapter
from .worker import TradingWorker


async def serve():
    settings = Settings()
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings.shared_secret()  # Fail at startup, never accept unauthenticated signals.
    db = DB(settings.database)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8)) as session:
        rpc = RPC(session, settings.rpc_url, settings.rpc_rps) if settings.rpc_url else None
        if settings.adapter == 'fake':
            if settings.live:
                raise ValueError('fake adapter cannot run with live trading enabled')
            adapter = FakeExecutionAdapter()
        elif settings.adapter == 'uniswap':
            if rpc is None:
                raise ValueError('uniswap adapter requires a configured trading RPC endpoint')
            adapter = UniswapRobinhoodExecutionAdapter(
                db, rpc, NonceManager(db, rpc, settings.wallet_address), settings)
        else:
            raise ValueError('unknown execution adapter')
        worker = TradingWorker(db, adapter, settings)
        app = create_app(db, settings, rpc)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', settings.health_port).start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        task = asyncio.create_task(worker.run())
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
