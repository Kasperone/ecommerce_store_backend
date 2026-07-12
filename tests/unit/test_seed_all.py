"""Tests for the fresh-install orchestrator (app/scripts/seed_all.py).

Pure sequencing/skip-logic tests — every heavy step is monkeypatched so no
real alembic/DB-write/Meilisearch work runs. The individual steps' behaviour
is covered by their own suites (test_seed_shipping, test_seed_homepage,
test_create_admin).
"""

from __future__ import annotations

import pytest

from app.scripts import seed_all


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """Monkeypatch every step to a recorder; return the call log."""
    log: list[str] = []
    monkeypatch.setattr(seed_all, "step_migrations", lambda: log.append("migrations"))
    monkeypatch.setattr(
        seed_all, "step_store_config", lambda: (log.append("store_config"), "exists")[1]
    )
    monkeypatch.setattr(seed_all, "step_shipping", lambda: log.append("shipping"))
    monkeypatch.setattr(seed_all, "step_homepage", lambda: log.append("homepage"))
    monkeypatch.setattr(seed_all, "step_admin", lambda: (log.append("admin"), None)[1])
    monkeypatch.setattr(seed_all, "step_search", lambda: (log.append("search"), None)[1])
    return log


def test_happy_path_runs_all_steps_in_order(calls):
    assert seed_all.main([]) == 0
    assert calls == ["migrations", "store_config", "shipping", "homepage", "admin", "search"]


def test_skip_flags(calls):
    assert seed_all.main(["--skip-migrations", "--skip-search"]) == 0
    assert calls == ["store_config", "shipping", "homepage", "admin"]


def test_search_failure_is_soft(calls, monkeypatch):
    def boom():
        calls.append("search")
        raise ConnectionError("meili down")

    monkeypatch.setattr(seed_all, "step_search", boom)
    assert seed_all.main([]) == 0  # exit code stays 0
    assert calls[-1] == "search"


def test_fatal_step_fails_fast(calls, monkeypatch):
    def boom():
        raise RuntimeError("seed_shipping exited 1")

    monkeypatch.setattr(seed_all, "step_shipping", boom)
    assert seed_all.main([]) == 1
    # Fail-fast: later steps never ran.
    assert calls == ["migrations", "store_config"]


def test_migration_failure_fails_fast(calls, monkeypatch):
    monkeypatch.setattr(
        seed_all, "step_migrations", lambda: (_ for _ in ()).throw(RuntimeError("alembic"))
    )
    assert seed_all.main([]) == 1
    assert calls == []


def test_admin_failure_is_fatal(calls, monkeypatch):
    monkeypatch.setattr(
        seed_all, "step_admin", lambda: (_ for _ in ()).throw(RuntimeError("create_admin exited 2"))
    )
    assert seed_all.main([]) == 1
    assert "search" not in calls


# ---------------------------------------------------------------------------
# step_admin env gating (create_admin itself is monkeypatched)
# ---------------------------------------------------------------------------


def test_step_admin_skips_without_env(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    reason = seed_all.step_admin()
    assert reason is not None
    assert "ADMIN_EMAIL" in reason


def test_step_admin_skips_with_email_but_no_password(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert seed_all.step_admin() is not None


def test_step_admin_runs_create_admin_with_env(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminBoot123")
    monkeypatch.setenv("ADMIN_NAME", "Jane Doe")
    seen: dict[str, list[str]] = {}

    def fake_create_admin_main(argv: list[str]) -> int:
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(seed_all, "create_admin_main", fake_create_admin_main)
    assert seed_all.step_admin() is None
    assert seen["argv"] == ["--email", "admin@example.com", "--name", "Jane Doe"]


def test_step_admin_raises_on_create_admin_failure(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "AdminBoot123")
    monkeypatch.delenv("ADMIN_NAME", raising=False)
    monkeypatch.setattr(seed_all, "create_admin_main", lambda argv: 2)
    with pytest.raises(RuntimeError, match="exited 2"):
        seed_all.step_admin()


# ---------------------------------------------------------------------------
# step_search config gating (reindex itself is monkeypatched)
# ---------------------------------------------------------------------------


def test_step_search_skips_when_unconfigured(monkeypatch):
    monkeypatch.setattr(seed_all, "meili_configured", lambda: False)
    reason = seed_all.step_search()
    assert reason is not None
    assert "Meilisearch" in reason


def test_step_search_reindexes_when_configured(monkeypatch):
    monkeypatch.setattr(seed_all, "meili_configured", lambda: True)
    ran: list[bool] = []
    monkeypatch.setattr(seed_all.reindex_search, "reindex", lambda: ran.append(True))
    assert seed_all.step_search() is None
    assert ran == [True]
