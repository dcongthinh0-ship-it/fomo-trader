import json

from .models import SignalValidationError, TradeSignal


def parse_signal(raw_body, now=None):
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SignalValidationError('INVALID_JSON') from None
    return TradeSignal.parse(body, now=now)
