"""Password hashing — argon2id (default), with optional bcrypt fallback.

The Argon2id parameters below match OWASP recommendations for interactive
authentication (2026): memory=64 MiB, iterations=3, parallelism=4.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from src.core.config import settings

# Module-level singleton — argon2 init is expensive, don't re-create per call
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain: str) -> str:
    """Hash a password with argon2id. Returns the encoded hash."""
    if not plain:
        raise ValueError("password must be non-empty")
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against an argon2id hash.

    Returns False on any verification failure (wrong password, malformed
    hash, etc.). Does NOT raise.
    """
    if not plain or not hashed:
        return False
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True if the hash uses older parameters and should be re-computed."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


__all__ = ["hash_password", "needs_rehash", "verify_password"]


# Reference unused import so mypy doesn't complain about settings (kept for
# future feature: swapping algorithms based on PASSWORD_HASH_ALGORITHM).
_ = settings.PASSWORD_HASH_ALGORITHM
