"""Backend support for removing a task — archive semantics + reversibility.

Decided in the parent task (t_ab475998): "Slet en opgave" maps to **Archive**
(DA: *Arkiver*), a reversible soft-delete that preserves every row. This module
exercises the server-side surface end-to-end:

* ``kanban_db.archive_task`` / ``kanban_db.unarchive_task``
  (the reversible round-trip),
* dependency edge cases (a child promoted because ``archived`` counts as
  terminal is re-gated when the parent is reopened),
* ``delete_archived_task`` (two-step hard delete),
* the CLI verb ``hermes kanban unarchive`` and the dashboard PATCH/bulk
  ``status="unarchived"`` path.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dashboard_plugin_api():
    mod_name = "hermes_dashboard_plugin_kanban_archive_test"
    if mod_name not in sys.modules:
        plugin_file = (
            Path(__file__).resolve().parents[2]
            / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
        )
        spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Data layer: reversible archive round-trip
# ---------------------------------------------------------------------------


def test_archive_unarchive_round_trip_preserves_data(kanban_home):
    """archive -> unarchive restores the exact prior active status + data."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        kb.complete_task(conn, parent, result="done")
        tid = kb.create_task(conn, title="worker", assignee="alice", parents=[parent])
        assert kb.get_task(conn, tid).status == "ready"
        kb.add_comment(conn, tid, "user", "keep me")
        before_comments = [c.body for c in kb.list_comments(conn, tid)]

        assert kb.archive_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "archived"
        # Data survives the soft delete.
        assert [c.body for c in kb.list_comments(conn, tid)] == before_comments

        assert kb.unarchive_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready", "parents are still terminal -> ready"
        assert [c.body for c in kb.list_comments(conn, tid)] == before_comments
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "archived" in kinds and "unarchived" in kinds


def test_unarchive_returns_todo_when_a_parent_is_still_open(kanban_home):
    """A task whose parent is *not* terminal comes back gated to ``todo``."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open parent")
        child = kb.create_task(conn, title="child", assignee="alice", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"
        assert kb.archive_task(conn, child) is True
        assert kb.unarchive_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"


def test_unarchive_refuses_non_archived_and_unknown_tasks(kanban_home):
    with kbc.connect() as conn:
        ready = kb.create_task(conn, title="ready", assignee="alice")
        assert kb.get_task(conn, ready).status == "ready"
        assert kb.unarchive_task(conn, ready) is False  # not archived

        assert kb.archive_task(conn, "t_does_not_exist") is False
        assert kb.unarchive_task(conn, "t_does_not_exist") is False

        # Idempotency: a second archive of an already-archived task is refused.
        assert kb.archive_task(conn, ready) is True
        assert kb.archive_task(conn, ready) is False
        assert kb.unarchive_task(conn, ready) is True
        assert kb.unarchive_task(conn, ready) is False  # already active


def test_archive_releases_children_and_unarchive_regates_them(kanban_home):
    """Archived counts as terminal (child promoted); reopening re-gates it.

    This is the dependency edge case: reopening the parent must not leave an
    unclaimed child stuck in ``ready`` with an unsatisfied parent.
    """
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(conn, title="child", assignee="bob", parents=[parent])
        assert kb.get_task(conn, child).status == "todo"

        # Parent archived -> terminal -> child promoted to ready.
        assert kb.archive_task(conn, parent) is True
        assert kb.get_task(conn, child).status == "ready"

        # Reopening the parent re-gates the child.
        assert kb.unarchive_task(conn, parent) is True
        assert kb.get_task(conn, parent).status == "ready"
        assert kb.get_task(conn, child).status == "todo"
        child_events = [e.kind for e in kb.list_events(conn, child)]
        assert "dependency_wait" in child_events


def test_delete_requires_archive_first(kanban_home):
    """Hard delete is two-step: an active task cannot be deleted directly."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="keeper", assignee="alice")
        assert kb.delete_archived_task(conn, tid) is False  # must archive first
        assert kb.get_task(conn, tid) is not None

        assert kb.archive_task(conn, tid) is True
        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None


# ---------------------------------------------------------------------------
# CLI: hermes kanban archive / unarchive
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def test_cli_unarchive_round_trip(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli card", assignee="alice")

    parser = _parser()

    args = parser.parse_args(["kanban", "archive", tid])
    assert kc.kanban_command(args) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"

    args = parser.parse_args(["kanban", "unarchive", tid])
    assert kc.kanban_command(args) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_cli_unarchive_refuses_active_task(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="still active", assignee="alice")
    args = _parser().parse_args(["kanban", "unarchive", tid])
    assert kc.kanban_command(args) == 1


def test_cli_archive_writes_receipt_comment(kanban_home):
    """The real CLI surface must pass the invoking actor -> a receipt is written.

    Regression for t_6476b1e0: ``_cmd_archive`` called ``archive_task`` without
    ``author=`` so the audit comment was never emitted in production.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli receipt", assignee="alice")

    args = _parser().parse_args(["kanban", "archive", tid])
    assert kc.kanban_command(args) == 0

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "archived"
        bodies = [c.body for c in kb.list_comments(conn, tid)]
    receipts = [b for b in bodies if b.startswith("Arkiveret af ")]
    assert receipts, f"CLI archive left no receipt comment, got {bodies!r}"
    assert " kl. " in receipts[-1], receipts[-1]


def test_cli_unarchive_writes_receipt_comment(kanban_home):
    """Unarchive via the CLI also records its audit receipt (t_6476b1e0)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli unreceipt", assignee="alice")
        assert kb.archive_task(conn, tid) is True

    args = _parser().parse_args(["kanban", "unarchive", tid])
    assert kc.kanban_command(args) == 0

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        bodies = [c.body for c in kb.list_comments(conn, tid)]
    receipts = [b for b in bodies if b.startswith("Genåbnet fra arkiv af ")]
    assert receipts, f"CLI unarchive left no receipt comment, got {bodies!r}"
    assert " kl. " in receipts[-1], receipts[-1]


# ---------------------------------------------------------------------------
# Dashboard API: PATCH + bulk status="unarchived"
# ---------------------------------------------------------------------------


def test_dashboard_patch_unarchives(kanban_home):
    api = _dashboard_plugin_api()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="dash card", assignee="alice")
        assert kb.archive_task(conn, tid) is True
        api._patch_status(conn, tid, api.UpdateTaskBody(status="unarchived"), False)
        assert kb.get_task(conn, tid).status == "ready"


def test_dashboard_bulk_unarchives(kanban_home):
    api = _dashboard_plugin_api()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk card", assignee="alice")
        assert kb.archive_task(conn, tid) is True
        entry: dict = {}
        api._bulk_apply_one(
            conn, tid, api.BulkTaskBody(ids=[tid], status="unarchived"), None, entry,
        )
        assert entry.get("ok") is not False, entry
        assert kb.get_task(conn, tid).status == "ready"
