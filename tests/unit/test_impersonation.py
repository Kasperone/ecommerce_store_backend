"""Unit tests for impersonation token plumbing + the middleware's bearer parsing
(pure functions — no DB, no HTTP)."""

from __future__ import annotations

from starlette.requests import Request

from app.core.impersonation import _bearer_token
from app.core.security import create_access_token, decode_token


def _request(headers: dict[str, str]) -> Request:
    """A real Starlette Request carrying just the given headers (for ``_bearer_token``)."""
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_access_token_carries_impersonator_claim():
    token, _, _ = create_access_token(subject="42", impersonator_id=7)
    decoded = decode_token(token, expected_type="access")
    assert decoded.subject == "42"
    assert decoded.impersonator_id == 7


def test_normal_access_token_has_no_impersonator_claim():
    token, _, _ = create_access_token(subject="42")
    assert decode_token(token, expected_type="access").impersonator_id is None


def test_bearer_token_extracts_from_authorization_header():
    assert _bearer_token(_request({"authorization": "Bearer abc.def.ghi"})) == "abc.def.ghi"


def test_bearer_token_none_when_missing_or_wrong_scheme():
    assert _bearer_token(_request({})) is None
    assert _bearer_token(_request({"authorization": "Basic abc"})) is None
    assert _bearer_token(_request({"authorization": "Bearer "})) is None
