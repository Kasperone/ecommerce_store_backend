from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def build_csp(extra_connect_origins: Iterable[str] = ()) -> str:
    """Compose the Content-Security-Policy used by the API.

    The backend serves JSON only — the policy is intentionally permissive
    around CSS (`'unsafe-inline'` for Nuxt UI / Tailwind during this
    migration) and locks down everything else. ``connect-src`` always
    allows ``'self'`` and is extended with the configured CORS origins so
    the frontend can hit ``/api/v1/*`` without breaking the policy when
    the API HTML pages (``/docs``) embed swagger UI.
    """
    connect_src = ["'self'", *extra_connect_origins]
    directives = [
        "default-src 'self'",
        "img-src 'self' data: https://*.r2.cloudflarestorage.com",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self'",
        f"connect-src {' '.join(connect_src)}",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]
    return "; ".join(directives)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds defense-in-depth response headers.

    `setdefault` is used so an upstream reverse proxy can override any header
    by setting it before the response leaves the edge.

    CSP is shipped as ``Content-Security-Policy-Report-Only`` first
    (``csp_enforce=False``) so any breakage shows up in browser consoles
    without breaking the live UI. Flip to enforce after a watch period.
    """

    def __init__(
        self,
        app,
        *,
        hsts: bool,
        csp: str | None = None,
        csp_enforce: bool = False,
    ) -> None:
        super().__init__(app)
        self._hsts = hsts
        self._csp = csp
        self._csp_header = (
            "Content-Security-Policy" if csp_enforce else "Content-Security-Policy-Report-Only"
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if self._csp:
            headers.setdefault(self._csp_header, self._csp)
        if self._hsts:
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response
