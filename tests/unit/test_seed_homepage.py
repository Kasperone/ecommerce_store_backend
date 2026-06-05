"""Unit tests for the homepage seed layout.

The seed ships an image-free, minimal homepage: no hero/slideshow and no image
URLs at all (the storefront renders generated placeholders for empty image
slots, and an admin builds the hero from the editor).
"""

import json

import pytest

from app.scripts import seed_homepage_page as seed


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_layout_has_no_slideshow_section(locale):
    layout = seed._build_layout(locale)
    types = [s["type"] for s in layout["sections"]]
    assert "slideshow" not in types
    assert types == ["promo-strip", "collection-list", "product-grid"]


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_layout_ships_no_image_urls(locale):
    layout = seed._build_layout(locale)
    blob = json.dumps(layout)
    # No seeded image references of any kind — nothing pre-existing in the system.
    assert "image_url" not in blob
    assert "cms/home/hero" not in blob
    assert ".jpg" not in blob


def test_seed_module_has_no_bundled_image_assets():
    """The seed must not ship image bytes in the repo."""
    assets = seed.__file__.rsplit("/", 1)[0] + "/assets"
    import os

    if os.path.isdir(assets):
        found = [f for _, _, files in os.walk(assets) for f in files]
        assert not found, f"unexpected bundled seed assets: {found}"
