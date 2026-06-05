"""Unit tests for the CMS page-layout discriminated union + block schemas.

Pure Pydantic-level tests (no DB, no HTTP): the discriminated union on ``type``,
defaults, strict extra-key rejection, sub-block taxonomies, and the
``BLOCK_SCHEMAS`` registry / ``BLOCK_TYPES`` parity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.page import (
    BLOCK_SCHEMAS,
    BLOCK_TYPES,
    HeroSection,
    PageLayout,
    ProductGridSection,
)

EXPECTED_BLOCK_TYPES = {
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
}


def test_block_types_are_the_expected_fourteen():
    assert set(BLOCK_TYPES) == EXPECTED_BLOCK_TYPES
    assert len(BLOCK_TYPES) == 14


def test_registry_matches_union():
    assert set(BLOCK_SCHEMAS) == set(BLOCK_TYPES)
    # Every registry entry has a name + settings list.
    for t, schema in BLOCK_SCHEMAS.items():
        assert schema["name"], t
        assert isinstance(schema["settings"], list), t


def test_empty_layout_is_valid():
    layout = PageLayout.model_validate({"sections": []})
    assert layout.sections == []


def test_default_layout_is_empty():
    assert PageLayout().sections == []


def test_all_fourteen_blocks_round_trip():
    sections = [{"type": t} for t in BLOCK_TYPES]
    layout = PageLayout.model_validate({"sections": sections})
    assert len(layout.sections) == 14
    assert [s.type for s in layout.sections] == list(BLOCK_TYPES)


def test_unknown_type_one_clean_error():
    with pytest.raises(ValidationError) as exc:
        PageLayout.model_validate({"sections": [{"type": "totally-bogus"}]})
    # Pydantic's discriminated-union error mentions the discriminator/tag.
    msg = str(exc.value).lower()
    assert "totally-bogus" in msg or "tag" in msg or "discrimin" in msg


def test_hero_settings_defaults_and_validation():
    s = HeroSection.model_validate({"type": "hero", "settings": {"heading": "Hi"}})
    assert s.settings.heading == "Hi"
    assert s.settings.overlay_opacity == 40
    assert s.settings.alignment == "left"


def test_overlay_opacity_range_enforced():
    with pytest.raises(ValidationError):
        HeroSection.model_validate({"type": "hero", "settings": {"overlay_opacity": 250}})


def test_product_grid_columns_range_enforced():
    with pytest.raises(ValidationError):
        ProductGridSection.model_validate({"type": "product-grid", "settings": {"columns": 99}})


def test_extra_keys_are_rejected():
    with pytest.raises(ValidationError):
        HeroSection.model_validate({"type": "hero", "settings": {"nope": 1}})


def test_section_optional_id_round_trips():
    s = HeroSection.model_validate({"type": "hero", "id": "hero-1"})
    assert s.id == "hero-1"


def test_slideshow_subblocks():
    layout = PageLayout.model_validate(
        {
            "sections": [
                {
                    "type": "slideshow",
                    "blocks": [
                        {"type": "slide", "settings": {"heading": "A"}},
                        {"type": "slide", "settings": {"heading": "B"}},
                    ],
                }
            ]
        }
    )
    section = layout.sections[0]
    assert getattr(section, "blocks", None) is not None
    assert len(section.blocks) == 2  # type: ignore[union-attr]


def test_faq_subblocks_and_defaults():
    layout = PageLayout.model_validate(
        {
            "sections": [
                {
                    "type": "faq",
                    "settings": {"heading": "Questions"},
                    "blocks": [
                        {"type": "faq-item", "settings": {"question": "Q?", "answer": "A."}}
                    ],
                }
            ]
        }
    )
    section = layout.sections[0]
    assert section.blocks[0].settings.question == "Q?"  # type: ignore[union-attr]
