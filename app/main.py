from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security_headers import SecurityHeadersMiddleware, build_csp

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="E-commerce API built with FastAPI",
)

# Rate limiting (slowapi)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # pyright: ignore[reportArgumentType] -- slowapi handler signature is narrower than Starlette's ExceptionHandler protocol
app.add_middleware(SlowAPIMiddleware)

# CORS Configuration. Explicit method/header allow-lists instead of "*" so
# the preflight surface is exactly what the frontend actually uses — keeps
# unusual verbs (TRACE, CONNECT) and arbitrary client headers off the
# allow list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Accept-Language",
        "X-Requested-With",
    ],
)

# Security headers (HSTS only outside dev so localhost http isn't pinned).
# CSP ships as Report-Only first; flip to enforce after a watch period in
# production.
app.add_middleware(
    SecurityHeadersMiddleware,
    hsts=settings.ENVIRONMENT == "production",
    csp=build_csp(extra_connect_origins=settings.CORS_ORIGINS),
    csp_enforce=False,
)


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "message": "E-commerce API is running",
        "version": settings.APP_VERSION,
        "status": "healthy",
    }


@app.get("/health")
async def health():
    """Health check for monitoring"""
    return {"status": "ok"}


# Import routers after middleware/limiter/CORS setup so they pick up the configured state.
from app.api.v1 import (  # noqa: E402
    admin_audit,
    admin_customers,
    admin_inventory,
    admin_reviews,
    auth,
    categories,
    orders,
    payments,
    products,
    reviews,
    search,
    uploads,
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(products.router, prefix="/api/v1/products", tags=["Products"])
app.include_router(categories.router, prefix="/api/v1/categories", tags=["Categories"])
app.include_router(orders.router, prefix="/api/v1/orders", tags=["Orders"])
app.include_router(payments.router, prefix="/api/v1/payments", tags=["Payments"])
app.include_router(search.router, prefix="/api/v1/search", tags=["Search"])
app.include_router(
    admin_customers.router, prefix="/api/v1/admin/customers", tags=["Admin Customers"]
)
app.include_router(
    admin_inventory.router, prefix="/api/v1/admin/inventory", tags=["Admin Inventory"]
)
app.include_router(
    admin_audit.router, prefix="/api/v1/admin/audit-events", tags=["Admin Audit Log"]
)
app.include_router(admin_reviews.router, prefix="/api/v1/admin/reviews", tags=["Admin Reviews"])
# Product image upload routes are nested under products since an image has no
# meaning without a parent. Prefix matches so paths surface as
# /api/v1/products/{id}/images/...
app.include_router(uploads.router, prefix="/api/v1/products", tags=["Product Images"])
# Customer + public reviews are nested under products (a review has no meaning
# without its product). Paths surface as /api/v1/products/{id}/reviews...
app.include_router(reviews.router, prefix="/api/v1/products", tags=["Reviews"])
