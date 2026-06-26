"""One-time password (OTP) generator — numeric code, configurable length.

Used by the OTP service for email-verification codes. The plaintext is
returned to the caller (which emails it) — we never persist plaintext, only
the argon2 hash (see `EmailVerificationOtp.otp_hash`).
"""

from __future__ import annotations

import secrets


def generate_otp(length: int = 4) -> str:
    """Return a numeric OTP of the given length.

    Uses `secrets.choice` over `0123456789`. Length is validated to be
    between 4 and 8 by the Settings model, so we don't repeat that here.
    """
    if not 4 <= length <= 8:
        raise ValueError("OTP length must be between 4 and 8")
    alphabet = "0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


__all__ = ["generate_otp"]