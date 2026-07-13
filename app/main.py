from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.impersonation import ImpersonationWriteGuardMiddleware
from app.core.rate_limit import limiter
from app.core.security_headers import SecurityHeadersMiddleware, build_csp

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="E-commerce API built with FastAPI",
)

# Impersonation write-guard. Registered first so it ends up the INNERMOST
# middleware — its 403 still flows back out through CORS, so the browser can
# read the body (a guard outside CORS would surface as an opaque CORS error).
app.add_middleware(ImpersonationWriteGuardMiddleware)

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
    account,
    addresses,
    admin_audit,
    admin_banners,
    admin_campaigns,
    admin_collections,
    admin_companies,
    admin_coupons,
    admin_customers,
    admin_dashboard,
    admin_draft_orders,
    admin_inventory,
    admin_media,
    admin_pages,
    admin_product_tags,
    admin_products,
    admin_quotes,
    admin_reports,
    admin_returns,
    admin_reviews,
    admin_segments,
    admin_settings,
    admin_shipping,
    admin_staff,
    admin_tags,
    admin_translations,
    admin_warehouses,
    auth,
    banners,
    blocks,
    carts,
    categories,
    collections,
    companies,
    company_team,
    contact,
    cookie_consent,
    coupons,
    feeds,
    newsletter,
    orders,
    pages,
    payments,
    product_variants,
    products,
    quotes,
    reviews,
    search,
    shipping,
    sitemap,
    store_config,
    uploads,
    wishlist,
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(products.router, prefix="/api/v1/products", tags=["Products"])
app.include_router(categories.router, prefix="/api/v1/categories", tags=["Categories"])
app.include_router(collections.router, prefix="/api/v1/collections", tags=["Collections"])
app.include_router(orders.router, prefix="/api/v1/orders", tags=["Orders"])
app.include_router(carts.router, prefix="/api/v1/carts", tags=["Carts"])
app.include_router(coupons.router, prefix="/api/v1/coupons", tags=["Coupons"])
app.include_router(contact.router, prefix="/api/v1", tags=["Contact"])
app.include_router(banners.router, prefix="/api/v1/banners", tags=["Banners"])
app.include_router(pages.router, prefix="/api/v1/pages", tags=["Pages"])
app.include_router(blocks.router, prefix="/api/v1/blocks", tags=["Blocks"])
app.include_router(newsletter.router, prefix="/api/v1/newsletter", tags=["Newsletter"])
app.include_router(payments.router, prefix="/api/v1/payments", tags=["Payments"])
app.include_router(search.router, prefix="/api/v1/search", tags=["Search"])
app.include_router(sitemap.router, prefix="/api/v1/sitemap", tags=["Sitemap"])
app.include_router(feeds.router, prefix="/api/v1/feeds", tags=["Feeds"])
app.include_router(shipping.router, prefix="/api/v1/shipping", tags=["Shipping"])
app.include_router(
    admin_customers.router, prefix="/api/v1/admin/customers", tags=["Admin Customers"]
)
app.include_router(admin_shipping.router, prefix="/api/v1/admin/shipping", tags=["Admin Shipping"])
app.include_router(
    admin_dashboard.router, prefix="/api/v1/admin/dashboard", tags=["Admin Dashboard"]
)
app.include_router(
    admin_inventory.router, prefix="/api/v1/admin/inventory", tags=["Admin Inventory"]
)
app.include_router(
    admin_audit.router, prefix="/api/v1/admin/audit-events", tags=["Admin Audit Log"]
)
app.include_router(admin_reports.router, prefix="/api/v1/admin/reports", tags=["Admin Reports"])
app.include_router(admin_returns.router, prefix="/api/v1/admin/returns", tags=["Admin Returns"])
app.include_router(admin_reviews.router, prefix="/api/v1/admin/reviews", tags=["Admin Reviews"])
app.include_router(admin_segments.router, prefix="/api/v1/admin/segments", tags=["Admin Segments"])
app.include_router(admin_tags.router, prefix="/api/v1/admin/tags", tags=["Admin Tags"])
app.include_router(
    admin_product_tags.router, prefix="/api/v1/admin/product-tags", tags=["Admin Product Tags"]
)
app.include_router(admin_products.router, prefix="/api/v1/admin/products", tags=["Admin Products"])
app.include_router(admin_banners.router, prefix="/api/v1/admin/banners", tags=["Admin Banners"])
app.include_router(admin_pages.router, prefix="/api/v1/admin/pages", tags=["Admin Pages"])
app.include_router(admin_media.router, prefix="/api/v1/admin/media", tags=["Admin Media"])
app.include_router(
    admin_campaigns.router, prefix="/api/v1/admin/campaigns", tags=["Admin Campaigns"]
)
app.include_router(
    admin_companies.router, prefix="/api/v1/admin/companies", tags=["Admin Companies"]
)
app.include_router(admin_coupons.router, prefix="/api/v1/admin/coupons", tags=["Admin Coupons"])
app.include_router(
    admin_collections.router, prefix="/api/v1/admin/collections", tags=["Admin Collections"]
)
app.include_router(admin_quotes.router, prefix="/api/v1/admin/quotes", tags=["Admin Quotes"])
app.include_router(
    admin_draft_orders.router,
    prefix="/api/v1/admin/draft-orders",
    tags=["Admin Draft Orders"],
)
app.include_router(admin_staff.router, prefix="/api/v1/admin/staff", tags=["Admin Staff"])
app.include_router(admin_settings.router, prefix="/api/v1/admin/settings", tags=["Admin Settings"])
app.include_router(
    admin_translations.router, prefix="/api/v1/admin/translations", tags=["Admin Translations"]
)
app.include_router(
    admin_warehouses.router, prefix="/api/v1/admin/warehouses", tags=["Admin Warehouses"]
)
# Product image upload routes are nested under products since an image has no
# meaning without a parent. Prefix matches so paths surface as
# /api/v1/products/{id}/images/...
app.include_router(uploads.router, prefix="/api/v1/products", tags=["Product Images"])
# Customer + public reviews are nested under products (a review has no meaning
# without its product). Paths surface as /api/v1/products/{id}/reviews...
app.include_router(reviews.router, prefix="/api/v1/products", tags=["Reviews"])
# Admin option/variant matrix routes are nested under products (a variant has
# no meaning without its product). Paths surface as
# /api/v1/products/{id}/options and /api/v1/products/{id}/variants...
app.include_router(product_variants.router, prefix="/api/v1/products", tags=["Product Variants"])
app.include_router(account.router, prefix="/api/v1/me", tags=["Account"])
app.include_router(addresses.router, prefix="/api/v1/addresses", tags=["Addresses"])
app.include_router(wishlist.router, prefix="/api/v1/wishlist", tags=["Wishlist"])
app.include_router(quotes.router, prefix="/api/v1/quotes", tags=["Quotes"])
app.include_router(companies.router, prefix="/api/v1/companies", tags=["Companies"])
# TM-be (Wave D slice 5) — company team management. Distinct path from
# /companies/me (this is /companies/me/users/...); kept in its own router file.
app.include_router(company_team.router, prefix="/api/v1/companies/me/users", tags=["Company Team"])
app.include_router(store_config.router, prefix="/api/v1/store-config", tags=["Store Config"])
app.include_router(cookie_consent.router, prefix="/api/v1", tags=["Cookie Consent"])
