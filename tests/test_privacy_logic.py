"""Privacy: IP hashing uses a dedicated salt only (no payment-secret reuse)."""
import hashlib
from app.routers.privacy import _hash_ip
from app.config import settings


def test_empty_ip_returns_empty():
    assert _hash_ip("") == ""


def test_deterministic():
    assert _hash_ip("203.0.113.7") == _hash_ip("203.0.113.7")


def test_different_ips_differ():
    assert _hash_ip("1.1.1.1") != _hash_ip("2.2.2.2")


def test_uses_dedicated_ip_salt():
    expected = hashlib.sha256(settings.IP_HASH_SALT.encode() + b"203.0.113.7").hexdigest()[:32]
    assert _hash_ip("203.0.113.7") == expected


def test_does_not_reuse_razorpay_secret():
    # The fix: the Razorpay key must NEVER be used as the hashing salt.
    razorpay_based = hashlib.sha256(
        settings.RAZORPAY_KEY_SECRET.encode() + b"203.0.113.7"
    ).hexdigest()[:32]
    assert _hash_ip("203.0.113.7") != razorpay_based
