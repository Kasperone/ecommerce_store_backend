"""Pydantic schemas for the CMS page-builder.

The crux is a **discriminated (tagged) union** on ``type`` (research §3.4): each of
the 14 block types is one ``*Settings`` model + one ``*Section`` union member
(``type: Literal[...]``, ``settings: *Settings``, optional ``id`` + optional
``blocks`` for sub-block taxonomies). ``PageLayout`` wraps ``sections: list[Section]``.

Two parallel surfaces are intentional (research §3.5):

- The **Pydantic union** validates layouts (route calls ``PageLayout.model_validate``).
- The **``BLOCK_SCHEMAS`` registry** is a Shopify-style declarative schema served as
  data to the admin auto-form (``GET /blocks/schema``) — it carries ``label`` /
  ``default`` / control hints the raw JSON-Schema lacks.

Locale/type/status ``*Literal`` aliases are hardcoded for pyright but cross-checked
against the model's ``*_VALUES`` tuples at import (mirrors ``BannerPlacementLiteral``).

Image fields are a plain ``image_url: str | None`` for V1 (parity with
``Banner.image_url``). Every raw-HTML block field (``custom-html`` html,
``rich-text``/``image-with-text`` body, ``faq`` answers) is server-sanitized via
``nh3`` in the route before it is ever stored, and every ``*_url`` field is
scheme-validated here (``_is_safe_url``) — never trust these fields at render.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.page import (
    PAGE_LOCALE_VALUES,
    PAGE_STATUS_VALUES,
    PAGE_TYPE_VALUES,
)
from app.schemas.base import BaseSchema

# --------------------------------------------------------------------------- #
# Enum literals (hardcoded for pyright; import-time drift catch vs the model)  #
# --------------------------------------------------------------------------- #

PageTypeLiteral = Literal["home", "standard", "landing"]
PageLocaleLiteral = Literal["en", "pl"]
PageStatusLiteral = Literal["draft", "published", "scheduled"]

assert set(PageTypeLiteral.__args__) == set(PAGE_TYPE_VALUES), (  # noqa: S101
    "PageTypeLiteral drifted from app.models.page.PAGE_TYPE_VALUES: "
    f"schema={set(PageTypeLiteral.__args__)} vs model={set(PAGE_TYPE_VALUES)}"
)
assert set(PageLocaleLiteral.__args__) == set(PAGE_LOCALE_VALUES), (  # noqa: S101
    "PageLocaleLiteral drifted from app.models.page.PAGE_LOCALE_VALUES: "
    f"schema={set(PageLocaleLiteral.__args__)} vs model={set(PAGE_LOCALE_VALUES)}"
)
assert set(PageStatusLiteral.__args__) == set(PAGE_STATUS_VALUES), (  # noqa: S101
    "PageStatusLiteral drifted from app.models.page.PAGE_STATUS_VALUES: "
    f"schema={set(PageStatusLiteral.__args__)} vs model={set(PAGE_STATUS_VALUES)}"
)


# --------------------------------------------------------------------------- #
# Block settings models (one per block type) + shared sub-block models         #
# --------------------------------------------------------------------------- #
# All block settings forbid unknown keys so a typo in admin payload 422s loudly
# rather than silently storing dead config.


# A CMS link/image URL (``*_url`` field) is safe if it is empty, a site-relative
# path, a fragment, or an explicit http(s)/mailto/tel URL. Everything else —
# ``javascript:``, ``data:``, ``vbscript:``, protocol-relative ``//host`` — is
# rejected: these fields flow into ``NuxtLink :to`` / ``<img src>`` on public
# pages, so a ``javascript:`` scheme there is a stored XSS (SEC-012). The scheme
# check is the real backstop; the frontend link guard is defense-in-depth.
_URL_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel"})


def _is_safe_url(value: str) -> bool:
    v = value.strip()
    if v == "":
        return True
    # Browsers strip control chars before parsing (``java\tscript:`` → ``javascript:``),
    # so reject any embedded control char outright.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        return False
    # Protocol-relative ``//host`` is treated as absolute by browsers.
    if v.startswith("//"):
        return False
    match = _URL_SCHEME_RE.match(v)
    if match is None:
        # No scheme → relative path / fragment; cannot open a new context.
        return True
    return match.group(1).lower() in _SAFE_URL_SCHEMES


class _BlockBase(BaseModel):
    """Base for every block settings + sub-block model: strict (extra keys 422)."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _reject_unsafe_urls(self) -> _BlockBase:
        """Scheme-validate every ``*_url`` field (cta_url/link_url/image_url/…)
        so an unsafe scheme can never be stored — see ``_is_safe_url`` / SEC-012."""
        for name, value in self.__dict__.items():
            if name.endswith("_url") and isinstance(value, str) and not _is_safe_url(value):
                raise ValueError(
                    f"{name!r} must be empty, a relative path, or an http(s)/mailto/tel URL"
                )
        return self


# ---- Hero ----------------------------------------------------------------- #
class HeroSettings(_BlockBase):
    heading: str = ""
    subheading: str | None = None
    image_url: str | None = None
    overlay_opacity: int = Field(default=40, ge=0, le=100)
    alignment: Literal["left", "center", "right"] = "left"
    cta_label: str | None = None
    cta_url: str | None = None
    theme: Literal["light", "dark"] = "dark"


# ---- Slideshow ------------------------------------------------------------ #
class SlideBlockSettings(_BlockBase):
    image_url: str | None = None
    heading: str | None = None
    subheading: str | None = None
    cta_label: str | None = None
    cta_url: str | None = None


class SlideBlock(_BlockBase):
    type: Literal["slide"] = "slide"
    id: str | None = None
    settings: SlideBlockSettings = Field(default_factory=SlideBlockSettings)


class SlideshowSettings(_BlockBase):
    autoplay: bool = True
    interval_ms: int = Field(default=6000, ge=1000, le=30000)
    show_dots: bool = True
    show_arrows: bool = True


# ---- Promo strip ---------------------------------------------------------- #
class PromoStripSettings(_BlockBase):
    text: str = ""
    bg_color: str | None = None
    link_url: str | None = None
    dismissible: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None


# ---- Product grid --------------------------------------------------------- #
class ProductGridSettings(_BlockBase):
    heading: str | None = None
    source: Literal["featured", "bestsellers", "new", "collection", "manual"] = "featured"
    collection_slug: str | None = None
    product_ids: list[int] = Field(default_factory=list)
    columns: int = Field(default=4, ge=1, le=6)
    limit: int = Field(default=8, ge=1, le=48)
    show_price: bool = True
    show_rating: bool = True
    show_badge: bool = True


# ---- Product carousel ----------------------------------------------------- #
class ProductCarouselSettings(_BlockBase):
    heading: str | None = None
    source: Literal["featured", "bestsellers", "new", "collection", "manual"] = "featured"
    collection_slug: str | None = None
    product_ids: list[int] = Field(default_factory=list)
    slides_per_view: int = Field(default=4, ge=1, le=8)
    limit: int = Field(default=12, ge=1, le=48)
    autoplay: bool = False


# ---- Featured single product ---------------------------------------------- #
class FeaturedProductSettings(_BlockBase):
    product_id: int | None = None
    layout: Literal["image-left", "image-right"] = "image-left"
    show_description: bool = True
    cta_label: str | None = None


# ---- Collection list / category tiles ------------------------------------- #
class CollectionTileSettings(_BlockBase):
    image_url: str | None = None
    label: str | None = None
    link_url: str | None = None


class CollectionTileBlock(_BlockBase):
    type: Literal["tile"] = "tile"
    id: str | None = None
    settings: CollectionTileSettings = Field(default_factory=CollectionTileSettings)


class CollectionListSettings(_BlockBase):
    heading: str | None = None
    columns: int = Field(default=3, ge=1, le=6)


# ---- Rich text ------------------------------------------------------------ #
class RichTextSettings(_BlockBase):
    heading: str | None = None
    body: str = ""
    width: Literal["narrow", "normal", "wide"] = "normal"
    alignment: Literal["left", "center", "right"] = "left"


# ---- Image with text ------------------------------------------------------ #
class ImageWithTextSettings(_BlockBase):
    image_url: str | None = None
    heading: str | None = None
    body: str = ""
    image_position: Literal["left", "right"] = "left"
    cta_label: str | None = None
    cta_url: str | None = None


# ---- Image gallery -------------------------------------------------------- #
class GalleryImageSettings(_BlockBase):
    image_url: str | None = None
    alt: str | None = None
    link_url: str | None = None


class GalleryImageBlock(_BlockBase):
    type: Literal["image"] = "image"
    id: str | None = None
    settings: GalleryImageSettings = Field(default_factory=GalleryImageSettings)


class ImageGallerySettings(_BlockBase):
    heading: str | None = None
    layout: Literal["grid", "masonry", "carousel"] = "grid"
    columns: int = Field(default=3, ge=1, le=6)


# ---- FAQ / accordion ------------------------------------------------------ #
class FaqItemSettings(_BlockBase):
    question: str = ""
    answer: str = ""


class FaqItemBlock(_BlockBase):
    type: Literal["faq-item"] = "faq-item"
    id: str | None = None
    settings: FaqItemSettings = Field(default_factory=FaqItemSettings)


class FaqSettings(_BlockBase):
    heading: str | None = None


# ---- Coupon / promo callout ----------------------------------------------- #
class CouponCalloutSettings(_BlockBase):
    coupon_code: str = ""
    heading: str | None = None
    body: str | None = None
    cta_label: str | None = None
    cta_url: str | None = None


# ---- Spacer / divider ----------------------------------------------------- #
class SpacerSettings(_BlockBase):
    height: int = Field(default=48, ge=0, le=480)
    show_divider: bool = False
    color: str | None = None


# ---- Custom HTML / embed -------------------------------------------------- #
class CustomHtmlSettings(_BlockBase):
    # Raw HTML, server-sanitized (nh3 allowlist) in the route before storage.
    html: str = ""


# --------------------------------------------------------------------------- #
# Section union members (the discriminated union on ``type``)                  #
# --------------------------------------------------------------------------- #


class _SectionBase(BaseModel):
    """Shared shape: an optional stable ``id`` for keyed reorders (Shopify-style)."""

    model_config = ConfigDict(extra="forbid")
    id: str | None = None


class HeroSection(_SectionBase):
    type: Literal["hero"]
    settings: HeroSettings = Field(default_factory=HeroSettings)


class SlideshowSection(_SectionBase):
    type: Literal["slideshow"]
    settings: SlideshowSettings = Field(default_factory=SlideshowSettings)
    blocks: list[SlideBlock] = Field(default_factory=list)


class PromoStripSection(_SectionBase):
    type: Literal["promo-strip"]
    settings: PromoStripSettings = Field(default_factory=PromoStripSettings)


class ProductGridSection(_SectionBase):
    type: Literal["product-grid"]
    settings: ProductGridSettings = Field(default_factory=ProductGridSettings)


class ProductCarouselSection(_SectionBase):
    type: Literal["product-carousel"]
    settings: ProductCarouselSettings = Field(default_factory=ProductCarouselSettings)


class FeaturedProductSection(_SectionBase):
    type: Literal["featured-product"]
    settings: FeaturedProductSettings = Field(default_factory=FeaturedProductSettings)


class CollectionListSection(_SectionBase):
    type: Literal["collection-list"]
    settings: CollectionListSettings = Field(default_factory=CollectionListSettings)
    blocks: list[CollectionTileBlock] = Field(default_factory=list)


class RichTextSection(_SectionBase):
    type: Literal["rich-text"]
    settings: RichTextSettings = Field(default_factory=RichTextSettings)


class ImageWithTextSection(_SectionBase):
    type: Literal["image-with-text"]
    settings: ImageWithTextSettings = Field(default_factory=ImageWithTextSettings)


class ImageGallerySection(_SectionBase):
    type: Literal["image-gallery"]
    settings: ImageGallerySettings = Field(default_factory=ImageGallerySettings)
    blocks: list[GalleryImageBlock] = Field(default_factory=list)


class FaqSection(_SectionBase):
    type: Literal["faq"]
    settings: FaqSettings = Field(default_factory=FaqSettings)
    blocks: list[FaqItemBlock] = Field(default_factory=list)


class CouponCalloutSection(_SectionBase):
    type: Literal["coupon-callout"]
    settings: CouponCalloutSettings = Field(default_factory=CouponCalloutSettings)


class SpacerSection(_SectionBase):
    type: Literal["spacer"]
    settings: SpacerSettings = Field(default_factory=SpacerSettings)


class CustomHtmlSection(_SectionBase):
    type: Literal["custom-html"]
    settings: CustomHtmlSettings = Field(default_factory=CustomHtmlSettings)


Section = Annotated[
    HeroSection
    | SlideshowSection
    | PromoStripSection
    | ProductGridSection
    | ProductCarouselSection
    | FeaturedProductSection
    | CollectionListSection
    | RichTextSection
    | ImageWithTextSection
    | ImageGallerySection
    | FaqSection
    | CouponCalloutSection
    | SpacerSection
    | CustomHtmlSection,
    Field(discriminator="type"),
]


# The 14 block ``type`` strings, in canonical order (drives schema-list order).
BLOCK_TYPES: tuple[str, ...] = (
    "hero",
    "slideshow",
    "promo-strip",
    "product-grid",
    "product-carousel",
    "featured-product",
    "collection-list",
    "rich-text",
    "image-with-text",
    "image-gallery",
    "faq",
    "coupon-callout",
    "spacer",
    "custom-html",
)


class PageLayout(BaseModel):
    """A page's full layout: an ordered list of typed sections.

    Validated explicitly in the endpoint via ``PageLayout.model_validate(...)``
    (the FastAPI nested-union caveat, research §3.4) so an unknown ``type`` yields
    one clean error rather than N per-branch ones.
    """

    model_config = ConfigDict(extra="forbid")
    sections: list[Section] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Page request / response schemas                                             #
# --------------------------------------------------------------------------- #


class PageCreate(BaseSchema):
    """Request body for ``POST /api/v1/admin/pages``.

    A new page starts as a ``draft`` with an empty (or supplied) ``draft_layout``.
    """

    slug: Annotated[str, Field(min_length=1, max_length=255)]
    type: PageTypeLiteral = "standard"
    locale: PageLocaleLiteral = "en"
    draft_layout: PageLayout = Field(default_factory=PageLayout)
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=500)
    seo_image_url: str | None = Field(default=None, max_length=2048)


class PageDraftUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/pages/{id}/draft``.

    All fields optional — a partial edit may touch only the layout or only SEO.
    """

    draft_layout: PageLayout | None = None
    seo_title: str | None = Field(default=None, max_length=255)
    seo_description: str | None = Field(default=None, max_length=500)
    seo_image_url: str | None = Field(default=None, max_length=2048)


class PagePublishRequest(BaseSchema):
    """Request body for ``POST /api/v1/admin/pages/{id}/publish`` (optional label)."""

    label: str | None = Field(default=None, max_length=255)


class PageResponse(BaseSchema):
    """Full admin page shape (draft + published + metadata)."""

    id: int
    slug: str
    type: PageTypeLiteral
    locale: PageLocaleLiteral
    status: PageStatusLiteral
    draft_layout: PageLayout
    published_layout: PageLayout | None = None
    published_version_id: int | None = None
    scheduled_at: datetime | None = None
    unpublish_at: datetime | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    seo_image_url: str | None = None
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime


class PageListItem(BaseSchema):
    """Slim row for the admin pages list (no layout blobs)."""

    id: int
    slug: str
    type: PageTypeLiteral
    locale: PageLocaleLiteral
    status: PageStatusLiteral
    published_version_id: int | None = None
    seo_title: str | None = None
    created_at: datetime
    updated_at: datetime


class PageListResponse(BaseSchema):
    """Paginated admin pages list."""

    items: list[PageListItem]
    total: int
    page: int
    page_size: int
    pages: int


class PagePublic(BaseSchema):
    """Public storefront page shape — serves the published layout + page SEO.

    No draft, no audit internals. ``GET /pages/{slug}`` returns this from
    ``published_layout``; the preview route returns it from ``draft_layout``.
    """

    slug: str
    type: PageTypeLiteral
    locale: PageLocaleLiteral
    layout: PageLayout
    seo_title: str | None = None
    seo_description: str | None = None
    seo_image_url: str | None = None


class PageVersionResponse(BaseSchema):
    """A page-version row (immutable snapshot)."""

    id: int
    page_id: int
    version_no: int
    label: str | None = None
    created_by: int | None = None
    created_at: datetime


class PageVersionListResponse(BaseSchema):
    """Versions of a page, newest first."""

    items: list[PageVersionResponse]
    total: int


class PreviewTokenResponse(BaseSchema):
    """A signed, short-TTL preview token + its preview URL fragment."""

    token: str
    expires_at: datetime


# --------------------------------------------------------------------------- #
# Settings-schema registry (research §3.5) — feeds the admin auto-form         #
# --------------------------------------------------------------------------- #
# Declarative Shopify-style schema, served as data by ``GET /blocks/schema``.
# Each entry: {name, settings:[{type,id,label,default,...}], presets:[...]}.
# The control ``type`` strings map to admin form controls (text/textarea/richtext/
# image_picker/url/range/select/checkbox/number/product/collection/color/blocks).


def _opt(values: list[str]) -> list[dict[str, str]]:
    return [{"value": v, "label": v} for v in values]


BLOCK_SCHEMAS: dict[str, dict[str, Any]] = {
    "hero": {
        "name": "Hero",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading", "default": ""},
            {"type": "text", "id": "subheading", "label": "Subheading"},
            {"type": "image_picker", "id": "image_url", "label": "Background image"},
            {
                "type": "range",
                "id": "overlay_opacity",
                "label": "Overlay %",
                "min": 0,
                "max": 100,
                "step": 5,
                "default": 40,
            },
            {
                "type": "select",
                "id": "alignment",
                "label": "Text alignment",
                "options": _opt(["left", "center", "right"]),
                "default": "left",
            },
            {"type": "text", "id": "cta_label", "label": "Button label"},
            {"type": "url", "id": "cta_url", "label": "Button link"},
            {
                "type": "select",
                "id": "theme",
                "label": "Theme",
                "options": _opt(["light", "dark"]),
                "default": "dark",
            },
        ],
        "presets": [{"name": "Hero", "settings": {"heading": "Welcome"}}],
    },
    "slideshow": {
        "name": "Slideshow",
        "settings": [
            {"type": "checkbox", "id": "autoplay", "label": "Autoplay", "default": True},
            {
                "type": "range",
                "id": "interval_ms",
                "label": "Interval (ms)",
                "min": 1000,
                "max": 30000,
                "step": 500,
                "default": 6000,
            },
            {"type": "checkbox", "id": "show_dots", "label": "Show dots", "default": True},
            {"type": "checkbox", "id": "show_arrows", "label": "Show arrows", "default": True},
        ],
        "blocks": {
            "slide": {
                "name": "Slide",
                "settings": [
                    {"type": "image_picker", "id": "image_url", "label": "Image"},
                    {"type": "text", "id": "heading", "label": "Heading"},
                    {"type": "text", "id": "subheading", "label": "Subheading"},
                    {"type": "text", "id": "cta_label", "label": "Button label"},
                    {"type": "url", "id": "cta_url", "label": "Button link"},
                ],
            }
        },
        "presets": [
            {
                "name": "Slideshow",
                "settings": {},
                "blocks": [{"type": "slide", "id": "slide-0", "settings": {}}],
            }
        ],
    },
    "promo-strip": {
        "name": "Promo strip",
        "settings": [
            {"type": "text", "id": "text", "label": "Text", "default": ""},
            {"type": "color", "id": "bg_color", "label": "Background color"},
            {"type": "url", "id": "link_url", "label": "Link"},
            {"type": "checkbox", "id": "dismissible", "label": "Dismissible", "default": False},
            {"type": "datetime", "id": "starts_at", "label": "Starts at"},
            {"type": "datetime", "id": "ends_at", "label": "Ends at"},
        ],
        "presets": [{"name": "Promo strip", "settings": {"text": "Free shipping over $50"}}],
    },
    "product-grid": {
        "name": "Product grid",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading"},
            {
                "type": "select",
                "id": "source",
                "label": "Source",
                "options": _opt(["featured", "bestsellers", "new", "collection", "manual"]),
                "default": "featured",
            },
            {"type": "collection", "id": "collection_slug", "label": "Collection"},
            {"type": "product", "id": "product_ids", "label": "Products", "multiple": True},
            {
                "type": "range",
                "id": "columns",
                "label": "Columns",
                "min": 1,
                "max": 6,
                "step": 1,
                "default": 4,
            },
            {
                "type": "range",
                "id": "limit",
                "label": "Max products",
                "min": 1,
                "max": 48,
                "step": 1,
                "default": 8,
            },
            {"type": "checkbox", "id": "show_price", "label": "Show price", "default": True},
            {"type": "checkbox", "id": "show_rating", "label": "Show rating", "default": True},
            {"type": "checkbox", "id": "show_badge", "label": "Show badge", "default": True},
        ],
        "presets": [{"name": "Featured products", "settings": {"source": "featured"}}],
    },
    "product-carousel": {
        "name": "Product carousel",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading"},
            {
                "type": "select",
                "id": "source",
                "label": "Source",
                "options": _opt(["featured", "bestsellers", "new", "collection", "manual"]),
                "default": "featured",
            },
            {"type": "collection", "id": "collection_slug", "label": "Collection"},
            {"type": "product", "id": "product_ids", "label": "Products", "multiple": True},
            {
                "type": "range",
                "id": "slides_per_view",
                "label": "Slides per view",
                "min": 1,
                "max": 8,
                "step": 1,
                "default": 4,
            },
            {
                "type": "range",
                "id": "limit",
                "label": "Max products",
                "min": 1,
                "max": 48,
                "step": 1,
                "default": 12,
            },
            {"type": "checkbox", "id": "autoplay", "label": "Autoplay", "default": False},
        ],
        "presets": [{"name": "Product carousel", "settings": {"source": "featured"}}],
    },
    "featured-product": {
        "name": "Featured product",
        "settings": [
            {"type": "product", "id": "product_id", "label": "Product"},
            {
                "type": "select",
                "id": "layout",
                "label": "Layout",
                "options": _opt(["image-left", "image-right"]),
                "default": "image-left",
            },
            {
                "type": "checkbox",
                "id": "show_description",
                "label": "Show description",
                "default": True,
            },
            {"type": "text", "id": "cta_label", "label": "Button label"},
        ],
        "presets": [{"name": "Featured product", "settings": {}}],
    },
    "collection-list": {
        "name": "Collection list",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading"},
            {
                "type": "range",
                "id": "columns",
                "label": "Columns",
                "min": 1,
                "max": 6,
                "step": 1,
                "default": 3,
            },
        ],
        "blocks": {
            "tile": {
                "name": "Tile",
                "settings": [
                    {"type": "image_picker", "id": "image_url", "label": "Image"},
                    {"type": "text", "id": "label", "label": "Label"},
                    {"type": "url", "id": "link_url", "label": "Link"},
                ],
            }
        },
        "presets": [
            {
                "name": "Collection list",
                "settings": {},
                "blocks": [{"type": "tile", "id": "tile-0", "settings": {}}],
            }
        ],
    },
    "rich-text": {
        "name": "Rich text",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading"},
            {"type": "richtext", "id": "body", "label": "Body", "default": ""},
            {
                "type": "select",
                "id": "width",
                "label": "Width",
                "options": _opt(["narrow", "normal", "wide"]),
                "default": "normal",
            },
            {
                "type": "select",
                "id": "alignment",
                "label": "Alignment",
                "options": _opt(["left", "center", "right"]),
                "default": "left",
            },
        ],
        "presets": [{"name": "Rich text", "settings": {"body": "<p>Tell your story…</p>"}}],
    },
    "image-with-text": {
        "name": "Image with text",
        "settings": [
            {"type": "image_picker", "id": "image_url", "label": "Image"},
            {"type": "text", "id": "heading", "label": "Heading"},
            {"type": "richtext", "id": "body", "label": "Body", "default": ""},
            {
                "type": "select",
                "id": "image_position",
                "label": "Image position",
                "options": _opt(["left", "right"]),
                "default": "left",
            },
            {"type": "text", "id": "cta_label", "label": "Button label"},
            {"type": "url", "id": "cta_url", "label": "Button link"},
        ],
        "presets": [{"name": "Image with text", "settings": {}}],
    },
    "image-gallery": {
        "name": "Image gallery",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading"},
            {
                "type": "select",
                "id": "layout",
                "label": "Layout",
                "options": _opt(["grid", "masonry", "carousel"]),
                "default": "grid",
            },
            {
                "type": "range",
                "id": "columns",
                "label": "Columns",
                "min": 1,
                "max": 6,
                "step": 1,
                "default": 3,
            },
        ],
        "blocks": {
            "image": {
                "name": "Image",
                "settings": [
                    {"type": "image_picker", "id": "image_url", "label": "Image"},
                    {"type": "text", "id": "alt", "label": "Alt text"},
                    {"type": "url", "id": "link_url", "label": "Link"},
                ],
            }
        },
        "presets": [
            {
                "name": "Image gallery",
                "settings": {},
                "blocks": [{"type": "image", "id": "image-0", "settings": {}}],
            }
        ],
    },
    "faq": {
        "name": "FAQ",
        "settings": [
            {"type": "text", "id": "heading", "label": "Heading"},
        ],
        "blocks": {
            "faq-item": {
                "name": "Question",
                "settings": [
                    {"type": "text", "id": "question", "label": "Question", "default": ""},
                    {"type": "richtext", "id": "answer", "label": "Answer", "default": ""},
                ],
            }
        },
        "presets": [
            {
                "name": "FAQ",
                "settings": {},
                "blocks": [{"type": "faq-item", "id": "faq-item-0", "settings": {}}],
            }
        ],
    },
    "coupon-callout": {
        "name": "Coupon callout",
        "settings": [
            {"type": "text", "id": "coupon_code", "label": "Coupon code", "default": ""},
            {"type": "text", "id": "heading", "label": "Heading"},
            {"type": "richtext", "id": "body", "label": "Body"},
            {"type": "text", "id": "cta_label", "label": "Button label"},
            {"type": "url", "id": "cta_url", "label": "Button link"},
        ],
        "presets": [{"name": "Coupon callout", "settings": {"coupon_code": "SAVE10"}}],
    },
    "spacer": {
        "name": "Spacer",
        "settings": [
            {
                "type": "range",
                "id": "height",
                "label": "Height (px)",
                "min": 0,
                "max": 480,
                "step": 8,
                "default": 48,
            },
            {"type": "checkbox", "id": "show_divider", "label": "Show divider", "default": False},
            {"type": "color", "id": "color", "label": "Divider color"},
        ],
        "presets": [{"name": "Spacer", "settings": {"height": 48}}],
    },
    "custom-html": {
        "name": "Custom HTML",
        "settings": [
            {
                "type": "textarea",
                "id": "html",
                "label": "HTML (sanitized on save)",
                "default": "",
            },
        ],
        "presets": [{"name": "Custom HTML", "settings": {}}],
    },
}

# Import-time guard: the registry and the discriminated union must list exactly
# the same 14 block types (any addition/rename to one without the other fails the
# import — and so every dependent route/test — with a clear message).
assert set(BLOCK_SCHEMAS) == set(BLOCK_TYPES), (  # noqa: S101
    "BLOCK_SCHEMAS drifted from BLOCK_TYPES: "
    f"registry={set(BLOCK_SCHEMAS)} vs union={set(BLOCK_TYPES)}"
)
