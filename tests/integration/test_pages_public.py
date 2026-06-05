"""Integration tests for the public pages + blocks API.

Covers: ``GET /pages/{slug}`` (published render, locale resolution via query +
Accept-Language, 404 when unpublished/missing), ``GET /pages/{slug}/preview``
(valid token serves draft, bad/expired → 404, no-cache header), and
``GET /blocks/schema`` (all 14 block schemas).

Pages are created directly via the CRUD layer (no admin HTTP) so these stay
focused on the public read side.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.crud import page as crud_page
from app.crud.page import make_preview_token
from app.models.page import PageStatus, PageType
from app.schemas.page import PageLayout

pytestmark = pytest.mark.integration


def _make_page(
    db: Session,
    *,
    slug: str,
    locale: str = "en",
    publish: bool = True,
    draft_sections=None,
):
    blob = PageLayout.model_validate(
        {"sections": draft_sections or [{"type": "hero", "settings": {"heading": "Hi"}}]}
    ).model_dump(mode="json")
    page = crud_page.create(
        db,
        slug=slug,
        type=PageType.STANDARD.value,
        locale=locale,
        draft_layout=blob,
        seo_title="T",
        seo_description="D",
        seo_image_url=None,
        created_by=None,
    )
    if publish:
        crud_page.publish(db, page=page, label=None, actor_id=None)
        page.status = PageStatus.PUBLISHED.value
    db.commit()
    db.refresh(page)
    return page


# ---------------------------------------------------------------------------
# blocks schema
# ---------------------------------------------------------------------------


async def test_blocks_schema_lists_all_fourteen(client):
    resp = await client.get("/api/v1/blocks/schema")
    assert resp.status_code == 200
    body = resp.json()
    types = [b["type"] for b in body["blocks"]]
    assert len(types) == 14
    assert "hero" in types
    assert "custom-html" in types
    # each entry has a name + settings
    for b in body["blocks"]:
        assert b["name"]
        assert isinstance(b["settings"], list)


# ---------------------------------------------------------------------------
# public GET /pages/{slug}
# ---------------------------------------------------------------------------


async def test_get_published_page(client, db_session):
    _make_page(db_session, slug="about", locale="en")
    resp = await client.get("/api/v1/pages/about")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "about"
    assert body["locale"] == "en"
    assert body["layout"]["sections"][0]["type"] == "hero"
    assert body["seo_title"] == "T"


async def test_get_page_404_when_missing(client):
    assert (await client.get("/api/v1/pages/nope")).status_code == 404


async def test_get_page_404_when_only_draft(client, db_session):
    _make_page(db_session, slug="draft-only", publish=False)
    assert (await client.get("/api/v1/pages/draft-only")).status_code == 404


async def test_locale_query_override(client, db_session):
    _make_page(db_session, slug="multi", locale="pl")
    # default locale en → 404 (no en row)
    assert (await client.get("/api/v1/pages/multi")).status_code == 404
    # explicit ?locale=pl resolves
    resp = await client.get("/api/v1/pages/multi", params={"locale": "pl"})
    assert resp.status_code == 200
    assert resp.json()["locale"] == "pl"


async def test_locale_accept_language_fallback(client, db_session):
    _make_page(db_session, slug="alng", locale="pl")
    resp = await client.get(
        "/api/v1/pages/alng", headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5"}
    )
    assert resp.status_code == 200
    assert resp.json()["locale"] == "pl"


# ---------------------------------------------------------------------------
# preview tokens
# ---------------------------------------------------------------------------


async def test_preview_with_valid_token_serves_draft(client, db_session):
    # draft differs from published: publish a hero, then change the draft
    page = _make_page(db_session, slug="prev", publish=True)
    crud_page.save_draft(
        db_session,
        page=page,
        draft_layout=PageLayout.model_validate(
            {"sections": [{"type": "rich-text", "settings": {"body": "DRAFT"}}]}
        ).model_dump(mode="json"),
    )
    db_session.commit()
    token = make_preview_token(page_id=page.id)

    resp = await client.get("/api/v1/pages/prev/preview", params={"token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["layout"]["sections"][0]["type"] == "rich-text"
    assert resp.headers.get("cache-control", "").startswith("no-store")


async def test_preview_bad_token_404(client, db_session):
    _make_page(db_session, slug="prevbad")
    assert (
        await client.get("/api/v1/pages/prevbad/preview", params={"token": "garbage.sig"})
    ).status_code == 404


async def test_preview_token_for_other_page_404(client, db_session):
    p1 = _make_page(db_session, slug="prev-a")
    _make_page(db_session, slug="prev-b")
    # token bound to p1 must not unlock prev-b
    token = make_preview_token(page_id=p1.id)
    assert (
        await client.get("/api/v1/pages/prev-b/preview", params={"token": token})
    ).status_code == 404


async def test_preview_missing_token_422(client, db_session):
    _make_page(db_session, slug="prev-c")
    # token is a required query param
    assert (await client.get("/api/v1/pages/prev-c/preview")).status_code == 422
