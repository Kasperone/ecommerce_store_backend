"""Unit tests for rate-limit key functions in app.core.rate_limit.

The slowapi key functions are sync and read directly from
``request.state.rate_limit_email`` (populated by the stash_* dependencies).
We exercise them with lightweight stand-ins instead of a real Request to
keep these tests pure.
"""

from types import SimpleNamespace

from app.core.rate_limit import get_email_or_ip_key, get_ip_key


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


def _make_request(*, email: str | None = None, ip: str = "203.0.113.5"):
    """Minimal stand-in for starlette.Request that ``get_remote_address``
    accepts. ``get_remote_address`` reads ``request.client.host`` (and
    falls back to headers, but those default to "" here).
    """
    state = SimpleNamespace()
    if email is not None:
        state.rate_limit_email = email
    return SimpleNamespace(
        client=_FakeClient(ip),
        headers={},
        state=state,
        scope={"client": (ip, 0)},
    )


def test_ip_key_returns_remote_address():
    req = _make_request(ip="198.51.100.7")
    assert get_ip_key(req) == "198.51.100.7"


def test_email_key_uses_email_when_stashed():
    req = _make_request(email="alice@example.com", ip="198.51.100.7")
    assert get_email_or_ip_key(req) == "email:alice@example.com"


def test_email_key_falls_back_to_ip_when_unstashed():
    req = _make_request(ip="198.51.100.7")  # no email on state
    assert get_email_or_ip_key(req) == "ip:198.51.100.7"


def test_email_key_falls_back_when_email_is_empty_string():
    """``""`` is falsy; the function must still fall back to IP rather than
    return ``email:``."""
    req = _make_request(email="", ip="198.51.100.7")
    assert get_email_or_ip_key(req) == "ip:198.51.100.7"


def test_email_key_lowercases_address():
    req = _make_request(email="Alice@Example.COM")
    assert get_email_or_ip_key(req) == "email:alice@example.com"


def test_email_key_trims_whitespace():
    req = _make_request(email="  alice@example.com  ")
    assert get_email_or_ip_key(req) == "email:alice@example.com"


def test_email_key_normalises_case_and_whitespace_together():
    req = _make_request(email="  ALICE@example.com\t")
    assert get_email_or_ip_key(req) == "email:alice@example.com"
