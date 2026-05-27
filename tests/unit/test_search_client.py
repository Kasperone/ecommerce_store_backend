"""Unit tests for the Meilisearch client gate (``app.core.search``).

Mirrors the R2 ``get_r2_client`` contract: the FastAPI dependency 503s when
unconfigured so dev/CI/tests boot without an engine, while ``meili_configured``
is the non-raising predicate the sync layer uses to no-op.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import search


def test_meili_configured_false_when_unset(monkeypatch):
    monkeypatch.setattr(search.settings, "MEILISEARCH_URL", "")
    monkeypatch.setattr(search.settings, "MEILISEARCH_MASTER_KEY", "")
    assert search.meili_configured() is False


def test_meili_configured_false_when_only_url_set(monkeypatch):
    monkeypatch.setattr(search.settings, "MEILISEARCH_URL", "http://localhost:7700")
    monkeypatch.setattr(search.settings, "MEILISEARCH_MASTER_KEY", "")
    assert search.meili_configured() is False


def test_meili_configured_true_when_both_set(monkeypatch):
    monkeypatch.setattr(search.settings, "MEILISEARCH_URL", "http://localhost:7700")
    monkeypatch.setattr(search.settings, "MEILISEARCH_MASTER_KEY", "key")
    assert search.meili_configured() is True


def test_get_meili_client_raises_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(search.settings, "MEILISEARCH_URL", "")
    monkeypatch.setattr(search.settings, "MEILISEARCH_MASTER_KEY", "")
    with pytest.raises(HTTPException) as exc_info:
        search.get_meili_client()
    assert exc_info.value.status_code == 503
    assert "Search not configured" in str(exc_info.value.detail)


def test_get_meili_client_returns_client_when_configured(monkeypatch):
    # build_client is pure config — no network call — so this is offline-safe.
    monkeypatch.setattr(search.settings, "MEILISEARCH_URL", "http://localhost:7700")
    monkeypatch.setattr(search.settings, "MEILISEARCH_MASTER_KEY", "key")
    client = search.get_meili_client()
    assert client.config.url == "http://localhost:7700"
