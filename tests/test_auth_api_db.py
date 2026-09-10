import json
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from trader.api import create_app
from trader.auth import AuthenticationError, signature, verify_request
from trader.models import SignalValidationError, TradeSignal


def test_hmac_accepts_exact_raw_body_and_rejects_tampering():
    raw = b'{"a":1}'
    sig = signature(b'secret', 100, raw)
    verify_request(b'secret', '100', sig, raw, now=100)
    with pytest.raises(AuthenticationError, match='INVALID_SIGNATURE'):
        verify_request(b'secret', '100', sig, b'{"a":2}', now=100)
    with pytest.raises(AuthenticationError, match='STALE_TIMESTAMP'):
        verify_request(b'secret', '100', sig, raw, now=131)


@pytest.mark.parametrize(('field', 'value', 'code'), [
    ('version', 'v0', 'UNSUPPORTED_VERSION'), ('chain_id', 1, 'INVALID_CHAIN_ID'),
    ('token_address', 'bad', 'INVALID_TOKEN_ADDRESS'), ('expires_at', 99, 'SIGNAL_EXPIRED'),
])
def test_signal_validation(valid_payload, field, value, code):
    body = {**valid_payload, field: value}
    with pytest.raises(SignalValidationError, match=code):
        TradeSignal.parse(body, now=100)


def test_signal_requires_eligible_true(valid_payload):
    valid_payload['eligibility']['eligible'] = False
    with pytest.raises(SignalValidationError, match='SIGNAL_NOT_ELIGIBLE'):
        TradeSignal.parse(valid_payload, now=100)


def test_database_event_id_is_idempotent(db, valid_payload):
    signal = TradeSignal.parse(valid_payload, now=100)
    assert db.accept_signal(signal, now=101)[0] == 'accepted'
    duplicate = {**valid_payload, 'signal_id': 'different'}
    assert db.accept_signal(TradeSignal.parse(duplicate, now=100), now=102) == ('duplicate', 'signal-1')
    assert db.conn.execute('SELECT count(*) FROM signals').fetchone()[0] == 1


async def test_api_accepts_then_deduplicates(db, settings, valid_payload):
    with patch('trader.auth.time.time', return_value=2_000_000_000), \
            patch('trader.models.time.time', return_value=2_000_000_000):
        client = TestClient(TestServer(create_app(db, settings)))
        await client.start_server()
        try:
            raw = json.dumps(valid_payload, separators=(',', ':')).encode()
            headers = {'X-Signal-Timestamp': '2000000000',
                       'X-Signal-Signature': signature(b'test-shared-secret', 2_000_000_000, raw),
                       'Content-Type': 'application/json'}
            first = await client.post('/v1/signals', data=raw, headers=headers)
            second = await client.post('/v1/signals', data=raw, headers=headers)
            assert first.status == 202 and (await first.json())['status'] == 'accepted'
            assert second.status == 200 and (await second.json())['status'] == 'duplicate'
        finally:
            await client.close()


async def test_api_rejects_bad_signature_without_database_write(db, settings, valid_payload):
    client = TestClient(TestServer(create_app(db, settings)))
    await client.start_server()
    try:
        response = await client.post('/v1/signals', json=valid_payload, headers={
            'X-Signal-Timestamp': '2000000000', 'X-Signal-Signature': 'bad'})
        assert response.status == 401
        assert db.conn.execute('SELECT count(*) FROM signals').fetchone()[0] == 0
    finally:
        await client.close()


async def test_health_contains_no_secrets_or_wallet(db, settings):
    client = TestClient(TestServer(create_app(db, settings)))
    await client.start_server()
    try:
        response = await client.get('/health')
        body = await response.json()
        assert response.status == 200
        assert body['live_trading_enabled'] is False
        assert not {'private_key', 'shared_secret', 'wallet_address', 'rpc_url'} & body.keys()
    finally:
        await client.close()
