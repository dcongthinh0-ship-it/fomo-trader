from unittest.mock import AsyncMock, patch

from trader.main import run_worker_after_nonce_ready
from trader.rpc import RPCError


async def test_worker_waits_for_nonce_reconciliation_without_exiting(db):
    worker = type('Worker', (), {'run': AsyncMock(return_value=None)})()
    nonce = type('Nonce', (), {
        'reconcile': AsyncMock(side_effect=[RPCError('temporary'), 0]),
    })()

    with patch('trader.main.asyncio.sleep', new=AsyncMock()) as sleep:
        await run_worker_after_nonce_ready(worker, nonce, db, retry_delay=1)

    assert nonce.reconcile.await_count == 2
    sleep.assert_awaited_once_with(1)
    worker.run.assert_awaited_once_with()
    assert db.state('worker_startup_pending') is False
    assert db.state('worker_last_error') is None
