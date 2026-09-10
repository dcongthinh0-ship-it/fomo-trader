import hashlib
import hmac
import time


class AuthenticationError(ValueError):
    pass


def signature(secret, timestamp, raw_body):
    return hmac.new(secret, str(timestamp).encode() + b'.' + raw_body, hashlib.sha256).hexdigest()


def verify_request(secret, timestamp, supplied, raw_body, max_skew=30, now=None):
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        raise AuthenticationError('INVALID_TIMESTAMP') from None
    if abs(int(now or time.time()) - stamp) > int(max_skew):
        raise AuthenticationError('STALE_TIMESTAMP')
    if not supplied or not hmac.compare_digest(signature(secret, stamp, raw_body), supplied):
        raise AuthenticationError('INVALID_SIGNATURE')
