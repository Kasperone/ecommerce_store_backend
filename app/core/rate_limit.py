"""Rate limiting helpers built on top of slowapi.

The ``limiter`` instance is wired into the FastAPI app in ``app.main`` and
exposed for per-route decorators (``@limiter.limit("...")``).

Two key functions are provided:
- ``get_ip_key`` — default, throttles per remote IP
- ``get_email_or_ip_key`` — throttles per email when one of the
  ``stash_*_email_for_rate_limit`` dependencies has populated
  ``request.state.rate_limit_email``; otherwise falls back to IP

The ``stash_*_email_for_rate_limit`` dependencies pre-read the request body
(JSON or form) and cache the email on ``request.state`` so the (sync)
slowapi key function can read it without re-parsing. Starlette caches the
parsed body on the request, so the route's own body/form parsing still
works.
"""

from typing import Optional

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings


def get_ip_key(request: Request) -> str:
    return get_remote_address(request)


def get_email_or_ip_key(request: Request) -> str:
    email: Optional[str] = getattr(request.state, "rate_limit_email", None)
    if email:
        return f"email:{email.strip().lower()}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=get_ip_key,
    storage_uri=settings.RATE_LIMIT_STORAGE_URL,
)


async def stash_login_email_for_rate_limit(request: Request) -> None:
    """Pre-read the OAuth2 form and stash username (email) for the limiter."""
    try:
        form = await request.form()
        username = form.get("username")
        if username:
            request.state.rate_limit_email = str(username)
    except Exception:
        pass


async def stash_json_email_for_rate_limit(request: Request) -> None:
    """Pre-read JSON body and stash ``email`` for the limiter."""
    try:
        body = await request.json()
        if isinstance(body, dict):
            email = body.get("email")
            if email:
                request.state.rate_limit_email = str(email)
    except Exception:
        pass
