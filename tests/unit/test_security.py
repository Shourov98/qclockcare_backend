"""Password hashing unit tests — argon2id round-trip + rehash detection."""

from __future__ import annotations

from src.core.security import hash_password, needs_rehash, verify_password


def test_hash_and_verify_round_trip() -> None:
    plain = "Sup3r$ecret!Pa55"
    hashed = hash_password(plain)

    assert hashed != plain
    assert hashed.startswith("$argon2id$")
    assert verify_password(plain, hashed) is True


def test_verify_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong", hashed) is False


def test_needs_rehash_false_on_fresh_hash() -> None:
    """A hash produced with current params should not need a rehash."""
    hashed = hash_password("Sup3r$ecret!Pa55")
    assert needs_rehash(hashed) is False
