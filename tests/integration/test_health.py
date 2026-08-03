"""Health / readiness probe tests.

``/health`` used to return a hardcoded ``{"status": "ok"}``. That is worse than
having no probe: the platform believes an instance with a dead database is fit
to serve, so it keeps routing traffic to it instead of draining it. The tests
here pin the two things a probe pair has to get right — readiness reports the
dependency, liveness does not depend on it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.integration


async def test_readiness_reports_ok_when_the_database_is_reachable(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "up"}


async def test_readiness_returns_503_when_the_database_is_unreachable(client, monkeypatch):
    """The whole point: a failing dependency must surface as 503 so the load
    balancer takes this replica out of rotation."""
    import app.main as main_mod

    def _boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(main_mod.engine, "connect", _boom)

    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "down"}


async def test_liveness_is_independent_of_the_database(client, monkeypatch):
    """Liveness must stay 200 during a database outage.

    If it failed too, the orchestrator would restart every replica on a database
    blip — turning a recoverable outage into a crash loop, and restarting
    processes that were never the problem.
    """
    import app.main as main_mod

    def _boom():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(main_mod.engine, "connect", _boom)

    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_dockerfile_healthcheck_targets_the_liveness_probe():
    """The container HEALTHCHECK must NOT be the readiness probe.

    Docker restarts an unhealthy container; pointing it at a DB-dependent check
    is the crash-loop above. This asserts the wiring because it is a one-word
    edit away from being wrong and nothing else would catch it.
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent.parent / "Dockerfile").read_text()
    healthcheck_line = next(
        line for line in dockerfile.splitlines() if "curl" in line and "health" in line
    )
    assert "/health/live" in healthcheck_line
