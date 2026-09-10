from aiohttp import web

from .auth import AuthenticationError, verify_request
from .models import SignalValidationError
from .signals import parse_signal


def create_app(db, settings, rpc=None):
    app = web.Application(client_max_size=64 * 1024)

    async def receive_signal(request):
        raw = await request.read()
        try:
            verify_request(settings.shared_secret(), request.headers.get('X-Signal-Timestamp'),
                           request.headers.get('X-Signal-Signature'), raw, settings.max_clock_skew)
            signal = parse_signal(raw)
        except (AuthenticationError, SignalValidationError, ValueError) as exc:
            return web.json_response({'status': 'rejected', 'error': str(exc)}, status=401 if isinstance(
                exc, AuthenticationError) else 400)
        status, sid = db.accept_signal(signal)
        return web.json_response({'status': status, 'signal_id': sid}, status=202 if status == 'accepted' else 200)

    async def health(_request):
        pending = db.conn.execute("SELECT count(*) FROM signals WHERE status IN ('RECEIVED','BUY_PENDING',"
                                  "'BUY_SUBMITTED','SELL_PENDING','SELL_SUBMITTED')").fetchone()[0]
        opened = db.conn.execute("SELECT count(*) FROM positions WHERE status='OPEN'").fetchone()[0]
        return web.json_response({
            'service': 'ok', 'database': 'WAL', 'live_trading_enabled': settings.live,
            'execution_adapter': settings.adapter, 'pending_signals': pending, 'open_positions': opened,
            'last_processed_signal_at': db.state('last_processed_signal_at'),
            'rpc_status': getattr(rpc, 'status', 'not_configured'),
        })

    app.router.add_post('/v1/signals', receive_signal)
    app.router.add_get('/health', health)
    return app
