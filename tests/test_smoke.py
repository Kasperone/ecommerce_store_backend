"""Smoke tests — confirm the test harness wires correctly before adding
auth/integration coverage on top."""

import pytest


async def test_health_endpoint(client):
    """``/health`` is the readiness probe and reports the database it checked.

    The dependency-free liveness probe is ``/health/live``; both are covered
    properly in ``tests/integration/test_health.py``.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "up"}


async def test_root_endpoint(client):
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"


@pytest.mark.integration
def test_db_session_can_create_and_query(db_session, user_factory):
    user = user_factory(email="smoke@example.com", first_name="Smoke")
    assert user.id is not None
    assert user.email == "smoke@example.com"
    # Rollback at teardown removes this row — re-running the test should pass.


@pytest.mark.integration
async def test_client_uses_test_db_session(client, user_factory):
    """Sanity check: a row created via the factory is visible to the API."""
    user = user_factory(email="visible@example.com")
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "TestPass123!"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # Verified user, correct password → 200 + access token.
    assert response.status_code == 200
    assert "access_token" in response.json()
