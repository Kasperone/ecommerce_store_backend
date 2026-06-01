"""View-only enforcement for admin customer impersonation.

An impersonation access token carries an ``impersonator_id`` claim and resolves
(via ``get_current_user``) to the impersonated *customer*. This middleware is the
single choke point that keeps impersonation read-only: any non-safe HTTP method
presented with such a token is rejected with 403, so every current **and future**
mutating customer route is covered without per-route opt-in (a missed route would
be a security hole — method-based blocking can't be forgotten).

Tokens that are absent, malformed, expired, or non-impersonation pass through
untouched — the normal auth dependencies still apply (a missing/invalid token
401s at the route; a normal customer/admin token is unaffected). Admin endpoints
are already unreachable under impersonation: the token's ``sub`` is the customer,
so ``get_current_admin`` 403s on the role check regardless of method.
"""

from jwt import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.security import decode_token

# Methods that only read state — always allowed under impersonation.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

IMPERSONATION_WRITE_BLOCKED = (
    "This action is not allowed while impersonating a customer (view-only)."
)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


class ImpersonationWriteGuardMiddleware(BaseHTTPMiddleware):
    """Reject non-safe HTTP methods when the bearer token is an impersonation token."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in SAFE_METHODS:
            token = _bearer_token(request)
            if token is not None:
                try:
                    decoded = decode_token(token, expected_type="access")
                except PyJWTError:
                    decoded = None
                if decoded is not None and decoded.impersonator_id is not None:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": IMPERSONATION_WRITE_BLOCKED},
                    )
        return await call_next(request)
