import re
import time
from dataclasses import dataclass

ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')


class SignalValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TradeSignal:
    signal_id: str
    event_id: str
    chain_id: int
    token_address: str
    expires_at: int
    payload: dict

    @classmethod
    def parse(cls, body, now=None):
        now = int(now or time.time())
        if not isinstance(body, dict):
            raise SignalValidationError('INVALID_JSON_OBJECT')
        if body.get('version') != 'trade_signal_v1':
            raise SignalValidationError('UNSUPPORTED_VERSION')
        if body.get('chain_id') != 4663:
            raise SignalValidationError('INVALID_CHAIN_ID')
        if not ADDRESS.fullmatch(str(body.get('token_address', ''))):
            raise SignalValidationError('INVALID_TOKEN_ADDRESS')
        try:
            expires_at = int(body['expires_at'])
        except (KeyError, TypeError, ValueError):
            raise SignalValidationError('INVALID_EXPIRY') from None
        if expires_at <= now:
            raise SignalValidationError('SIGNAL_EXPIRED')
        if (body.get('eligibility') or {}).get('eligible') is not True:
            raise SignalValidationError('SIGNAL_NOT_ELIGIBLE')
        signal_id, event_id = str(body.get('signal_id', '')), str(body.get('event_id', ''))
        if not signal_id or not event_id:
            raise SignalValidationError('MISSING_ID')
        return cls(signal_id, event_id, 4663, body['token_address'].lower(), expires_at, body)
