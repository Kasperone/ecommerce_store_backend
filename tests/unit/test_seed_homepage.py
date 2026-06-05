"""Unit tests for the homepage seed's upload-if-missing hero-image logic.

``_ensure_hero_images`` pushes the bundled hero images to R2 only when the object
is absent, skips ones that already exist, and no-ops when R2 isn't configured.
The R2 client is a plain ``MagicMock`` monkeypatched onto the seed module — the
branch logic is what's under test, not boto3 itself.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.scripts import seed_homepage_page as seed


def _not_found() -> ClientError:
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


def test_ensure_hero_images_uploads_each_missing_object(monkeypatch):
    client = MagicMock()
    client.head_object.side_effect = _not_found()
    monkeypatch.setattr(seed, "get_r2_client", lambda: client)
    monkeypatch.setattr("app.core.config.settings.R2_BUCKET_NAME", "test-bucket")

    seed._ensure_hero_images()

    assert client.put_object.call_count == len(seed._HERO_FILENAMES)
    keys = {c.kwargs["Key"] for c in client.put_object.call_args_list}
    assert keys == {seed._hero_key(fn) for fn in seed._HERO_FILENAMES.values()}
    for call in client.put_object.call_args_list:
        assert call.kwargs["ContentType"] == "image/jpeg"
        assert len(call.kwargs["Body"]) > 0  # real bundled bytes


def test_ensure_hero_images_skips_existing_objects(monkeypatch):
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 123}
    monkeypatch.setattr(seed, "get_r2_client", lambda: client)
    monkeypatch.setattr("app.core.config.settings.R2_BUCKET_NAME", "test-bucket")

    seed._ensure_hero_images()

    client.put_object.assert_not_called()


def test_ensure_hero_images_noops_when_r2_unconfigured(monkeypatch):
    def _raise() -> None:
        raise HTTPException(status_code=503)

    monkeypatch.setattr(seed, "get_r2_client", _raise)

    seed._ensure_hero_images()  # must not raise


def test_ensure_hero_images_reraises_non_404_errors(monkeypatch):
    client = MagicMock()
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
    )
    monkeypatch.setattr(seed, "get_r2_client", lambda: client)
    monkeypatch.setattr("app.core.config.settings.R2_BUCKET_NAME", "test-bucket")

    with pytest.raises(ClientError):
        seed._ensure_hero_images()
    client.put_object.assert_not_called()


def test_bundled_hero_assets_exist_and_are_git_sized():
    """The bundled bytes must ship with the repo and stay under the 1 MB
    large-file hook ceiling."""
    for filename in seed._HERO_FILENAMES.values():
        path = seed._HERO_ASSET_DIR / filename
        assert path.is_file(), f"missing bundled asset: {path}"
        assert path.stat().st_size < 1_000_000, f"{filename} exceeds 1 MB git ceiling"
