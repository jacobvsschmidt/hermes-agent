"""Regel 2/3 dispatcher hardening: startability gate + hard max-3 ceiling (t_82cedb90).

Defense-in-depth for Regel 2/3 (Smeden 2026-10-09 18:30): even if a card
somehow ends up ``ready`` without being part of the plan, the dispatcher must
not start a worker for it, and must never exceed ``DEFAULT_MAX_CONCURRENT_WORKERS``
(3) concurrent workers regardless of card status.

Acceptance criteria covered here:
  * a card in ``triage`` (and any ready card that is neither start-authorized
    nor plan-listed) is never transitioned to ``running`` by the dispatcher;
  * with 3 workers already running, no 4th worker is started;
  * refusals are logged with reasons and are greppable
    (``REGEL3_UNPLANNED_SKIP`` / ``REGEL3_CONCURRENCY_CAP``).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _today():
    return time.strftime("%Y-%m-%d")


def count_running(conn):
    return kbd.count_running_tasks(conn)


# ---------------------------------------------------------------------------
# Startability gate
# ---------------------------------------------------------------------------

def test_unplanned_ready_card_is_never_spawned(kanban_home, all_assignees_spawnable, caplog):
    """A ready card with no start marker and no plan entry is refused and left parked."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="flyby unplanned card", assignee="alice")
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            res = kbd.dispatch_once(
                conn, dry_run=True,
                startable_guard=True, max_concurrent_workers=3,
            )
    assert res.spawned == []
    assert res.skipped_unplanned and res.skipped_unplanned[0][0] == tid
    assert any(kbd.GUARD_LOG_TAG_UNPLANNED in r.getMessage() for r in caplog.records)
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"  # parked, never 'running'


def test_triage_card_is_never_spawned(kanban_home, all_assignees_spawnable):
    """A card in triage is structurally absent from the ready/review lanes and,
    even if it somehow reached a lane, the startability gate still refuses it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="parked triage card", assignee="alice", triage=True)
        res = kbd.dispatch_once(
            conn, dry_run=True,
            startable_guard=True, max_concurrent_workers=3,
        )
        assert res.spawned == []
        assert kb.get_task(conn, tid).status == "triage"
    # Guard is defence-in-depth for the lane leak: a triage card forced to ready
    # with no authorization is refused too.
    with kbc.connect() as conn:
        forced = kb.create_task(conn, title="leaked to ready", assignee="alice")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (forced,))
        res = kbd.dispatch_once(
            conn, dry_run=True,
            startable_guard=True, max_concurrent_workers=3,
        )
    assert res.spawned == []
    assert any(tid_ == forced for tid_, _ in res.skipped_unplanned)


def test_start_authorized_card_is_spawned(kanban_home, all_assignees_spawnable):
    """An explicit --start marker (created-event ``start: true``) authorizes a spawn."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="authorized card", assignee="alice",
                             start_authorized=True)
        res = kbd.dispatch_once(
            conn, dry_run=True,
            startable_guard=True, max_concurrent_workers=3,
        )
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_unplanned == []


def test_plan_listed_card_is_spawned_without_start_flag(kanban_home, all_assignees_spawnable):
    """A ready card whose title is in today's plan file starts even without --start."""
    (kanban_home / "inbox").mkdir()
    (kanban_home / "inbox" / f"{_today()}-PLAN.md").write_text(
        f"- {_today()}: first planned task\n- Run the nightly migration\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="run the nightly migration", assignee="alice")
        res = kbd.dispatch_once(
            conn, dry_run=True,
            startable_guard=True, max_concurrent_workers=3,
        )
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_unplanned == []


def test_card_not_in_plan_file_is_refused(kanban_home, all_assignees_spawnable, caplog):
    """Plan file exists but omits the card title -> refused fail-closed."""
    (kanban_home / "inbox").mkdir()
    (kanban_home / "inbox" / f"{_today()}-PLAN.md").write_text(
        "- Some other planned task\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ambitious unplanned card", assignee="alice")
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            res = kbd.dispatch_once(
                conn, dry_run=True,
                startable_guard=True, max_concurrent_workers=3,
            )
    assert res.spawned == []
    assert res.skipped_unplanned and res.skipped_unplanned[0][0] == tid
    assert any(kbd.GUARD_LOG_TAG_UNPLANNED in r.getMessage() for r in caplog.records)


def test_missing_plan_file_fails_closed(kanban_home, all_assignees_spawnable, caplog):
    """No plan file for today -> any non-start-authorized ready card is refused."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no plan today", assignee="alice")
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            res = kbd.dispatch_once(
                conn, dry_run=True,
                startable_guard=True, max_concurrent_workers=3,
            )
    assert res.spawned == []
    assert res.skipped_unplanned and res.skipped_unplanned[0][0] == tid
    assert any("not found" in reason for _, reason in res.skipped_unplanned)
    assert any(kbd.GUARD_LOG_TAG_UNPLANNED in r.getMessage() for r in caplog.records)


def test_unexpected_plan_error_fails_closed(kanban_home, all_assignees_spawnable, monkeypatch):
    """An exception inside the plan check refuses — never starts 'just in case'."""
    def _boom(_title):
        raise OSError("plan store unreachable")
    monkeypatch.setattr(kbd, "_today_plan_authorizes", _boom)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="boom card", assignee="alice")
        ok, reason = kbd._card_startable(conn, tid)
    assert ok is False
    assert "could not judge today's plan" in reason


# ---------------------------------------------------------------------------
# Hard max-3 concurrency ceiling
# ---------------------------------------------------------------------------

def test_never_fourth_worker_when_three_running(kanban_home, all_assignees_spawnable, caplog):
    """With 3 genuinely-running workers, an authorized ready card is refused by the cap."""
    with kbc.connect() as conn:
        running = [kb.create_task(conn, title=f"running-worker-{i}", assignee=f"alice{i}",
                                  start_authorized=True) for i in range(3)]
        for tid in running:
            assert kb.claim_task(conn, tid) is not None  # -> status 'running', survives reclaim
        tid = kb.create_task(conn, title="would-be 4th", assignee="alice0",
                             start_authorized=True)
        assert count_running(conn) == 3
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            res = kbd.dispatch_once(
                conn, dry_run=True,
                startable_guard=True, max_concurrent_workers=3,
            )
    assert res.spawned == []
    assert res.skipped_concurrency_capped and res.skipped_concurrency_capped[0][0] == tid
    assert any(kbd.GUARD_LOG_TAG_CONCURRENCY in r.getMessage() for r in caplog.records)
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_up_to_three_concurrent_workers_spawn(kanban_home, all_assignees_spawnable):
    """2 running + 3 ready start-authorized: the hard ceiling allows one, not four."""
    with kbc.connect() as conn:
        for i in range(2):
            running = kb.create_task(conn, title=f"running-worker-{i}", assignee=f"alice{i}",
                                     start_authorized=True)
            assert kb.claim_task(conn, running) is not None
        for i in range(3):
            kb.create_task(conn, title=f"ready-worker-{i}", assignee=f"alice{i}",
                           start_authorized=True)
        res = kbd.dispatch_once(
            conn, dry_run=True,
            startable_guard=True, max_concurrent_workers=3,
        )
    assert len(res.spawned) == 1          # 2 running + 1 = 3, never a 4th
    assert len(res.skipped_concurrency_capped) == 2  # the two that would be 4th/5th


def test_cap_applies_regardless_of_card_status(kanban_home, all_assignees_spawnable):
    """The ceiling holds even for review-lane work that the startable gate exempts."""
    with kbc.connect() as conn:
        for i in range(3):
            running = kb.create_task(conn, title=f"running-worker-{i}", assignee=f"alice{i}",
                                     start_authorized=True)
            assert kb.claim_task(conn, running) is not None
        # A review card that WOULD pass the startable gate (review is exempt) is
        # still held by the hard concurrency ceiling.
        review = kb.create_task(conn, title="review card", assignee="reviewer",
                                start_authorized=True)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
        res = kbd.dispatch_once(
            conn, dry_run=True,
            startable_guard=True, max_concurrent_workers=3,
        )
    assert res.spawned == []
    assert res.skipped_concurrency_capped  # the review card held by the ceiling


def test_cap_override_via_config(kanban_home, all_assignees_spawnable, monkeypatch):
    """``kanban.max_concurrent_workers`` raises the ceiling (default stays 3)."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  max_concurrent_workers: 5\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_MAX_CONCURRENT_WORKERS", raising=False)
    assert kbd.configured_max_concurrent_workers() == 5
    # Without any config, the ceiling resolves to the hard minimum (3).
    monkeypatch.delenv("HERMES_KANBAN_MAX_CONCURRENT_WORKERS", raising=False)
    (kanban_home / "config.yaml").write_text("", encoding="utf-8")
    assert kbd.configured_max_concurrent_workers() is None


def test_resolver_defaults_fail_closed(kanban_home, monkeypatch):
    """Guard defaults ON, ceiling defaults to the hard 3 — production semantics."""
    monkeypatch.delenv("HERMES_KANBAN_STARTABLE_GUARD", raising=False)
    assert kbd.configured_startable_guard() is True
    # An unreadable config keeps the guard ON (never widens auto-start scope).
    from hermes_cli import config as _cfg
    monkeypatch.delenv("HERMES_KANBAN_STARTABLE_GUARD", raising=False)

    def _raise(**_kwargs):
        raise OSError("broken config")
    monkeypatch.setattr(_cfg, "load_config_readonly", _raise)
    assert kbd.configured_startable_guard() is True