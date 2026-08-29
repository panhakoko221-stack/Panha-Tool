"""Small, dependency-free RFC 6238 TOTP helpers.

The application only needs the common authenticator configuration: HMAC-SHA1,
a 30-second time step, and six output digits.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import struct
import time
from typing import Optional, Union


TOTP_DIGITS = 6
TOTP_PERIOD = 30

_EMPTY_SECRET_MESSAGE = "TOTP secret is empty."
_INVALID_SECRET_MESSAGE = "TOTP secret is invalid."


class InvalidTOTPSecret(ValueError):
    """Raised when a TOTP secret cannot be decoded safely."""


def normalize_totp_secret(secret: str) -> str:
    """Return an uppercase, correctly padded Base32 TOTP secret.

    Spaces and hyphens commonly inserted for readability are ignored.  Error
    messages deliberately never echo the supplied secret.
    """

    if not isinstance(secret, str):
        raise InvalidTOTPSecret(_INVALID_SECRET_MESSAGE)

    compact = "".join(secret.split()).replace("-", "").upper()
    if not compact:
        raise InvalidTOTPSecret(_EMPTY_SECRET_MESSAGE)

    # Existing trailing padding is accepted and recalculated.  Padding in the
    # middle is malformed and must not be silently discarded.
    unpadded = compact.rstrip("=")
    if not unpadded:
        raise InvalidTOTPSecret(_EMPTY_SECRET_MESSAGE)
    if "=" in unpadded or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for char in unpadded):
        raise InvalidTOTPSecret(_INVALID_SECRET_MESSAGE)

    padding = "=" * ((8 - len(unpadded) % 8) % 8)
    normalized = unpadded + padding
    try:
        decoded = base64.b32decode(normalized, casefold=False)
    except (binascii.Error, ValueError, TypeError):
        raise InvalidTOTPSecret(_INVALID_SECRET_MESSAGE) from None
    if not decoded:
        raise InvalidTOTPSecret(_INVALID_SECRET_MESSAGE)
    return normalized


def _decode_secret(secret: str) -> bytes:
    normalized = normalize_totp_secret(secret)
    try:
        return base64.b32decode(normalized, casefold=False)
    except (binascii.Error, ValueError, TypeError):
        # Keep this boundary defensive if normalization changes in the future.
        raise InvalidTOTPSecret(_INVALID_SECRET_MESSAGE) from None


def _timestamp_seconds(timestamp: Optional[Union[int, float]]) -> int:
    value = time.time() if timestamp is None else timestamp
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("TOTP timestamp must be a finite, non-negative number.") from None
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError("TOTP timestamp must be a finite, non-negative number.")
    return int(numeric)


def generate_totp(secret: str, timestamp: Optional[Union[int, float]] = None) -> str:
    """Generate the six-digit RFC 6238 code for ``secret`` at ``timestamp``."""

    key = _decode_secret(secret)
    counter = _timestamp_seconds(timestamp) // TOTP_PERIOD
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{truncated % (10 ** TOTP_DIGITS):0{TOTP_DIGITS}d}"


def totp_seconds_remaining(timestamp: Optional[Union[int, float]] = None) -> int:
    """Return whole seconds until the current 30-second TOTP window ends.

    At an exact window boundary this returns 30; one second before it returns 1.
    """

    seconds = _timestamp_seconds(timestamp)
    return TOTP_PERIOD - (seconds % TOTP_PERIOD)


# A concise alias is useful to callers that already operate in a TOTP context.
remaining_seconds = totp_seconds_remaining


__all__ = [
    "InvalidTOTPSecret",
    "TOTP_DIGITS",
    "TOTP_PERIOD",
    "generate_totp",
    "normalize_totp_secret",
    "remaining_seconds",
    "totp_seconds_remaining",
]
