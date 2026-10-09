"""REGEL 2/3 flag semantics for ``hermes kanban create`` (t_8073f9ab).

Locks in the fail-closed default: a card created with NO explicit start beacon
is parked in triage (never ``ready``/``running``, so no worker is ever spawned
as a side effect of a bare ``create``). ``--start`` and a resolvable ``--plan``
authorize a start; an unresolvable/mismatched ``--plan`` fails closed to
triage; ``--triage`` combined with a beacon is ambiguous and refused.

These are the exact semantics the dispatcher guard (t_82cedb90) and the e2e
regression test (t_cc2bbab1) rely on.
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

BODY = "MAALING: verify creation status"


@pytest.fixture(autouse=True)
def _no_dispatcher_warning(monkeypatch):
    # A created ready card calls _check_dispatcher_presence; in a hermetic home
    # there is no gateway, and the probe would swallow it anyway — pin it closed
    # so it never touches the real machine's gateway liveness during tests.
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda *a, **k: (True, ""))


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "inbox"
    d.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_PLAN_DIR", str(d))
    return d


def _args(**kw):
    ns = argparse.Namespace()
    defaults = {
        "title": "x", "start": False, "plan": False, "triage": False,
        "initial_status": "running",
    }
    defaults.update(kw)
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# --- unit: _resolve_create_start ---

def test_no_beacon_parks_in_triage():
    triage, initial_status, authorized, reason = kc._resolve_create_start(_args())
    assert triage is True
    assert authorized is False
    assert reason is None


def test_start_authorizes():
    triage, initial_status, authorized, reason = kc._resolve_create_start(_args(start=True))
    assert triage is False
    assert authorized is True
    assert initial_status == "running"


def test_start_wins_over_plan():
    triage, _, authorized, _ = kc._resolve_create_start(_args(start=True, plan=True))
    assert triage is False
    assert authorized is True


def test_triage_with_beacon_is_ambiguous():
    for extra in ({"start": True}, {"plan": True}):
        with pytest.raises(ValueError):
            kc._resolve_create_start(_args(triage=True, **extra))


def test_initial_status_blocked_parks_without_beacon():
    triage, initial_status, authorized, reason = kc._resolve_create_start(
        _args(initial_status="blocked"))
    assert triage is False
    assert initial_status == "blocked"
    assert authorized is False
    assert reason is None


# --- unit: _plan_authorizes (fail-closed plan gate) ---

def test_plan_authorizes_listed_title(plan_dir):
    (plan_dir / f"{kc.time.strftime('%Y-%m-%d')}-PLAN.md").write_text(
        "- my-card\n## Other\n- thing\n", encoding="utf-8")
    ok, reason = kc._plan_authorizes("my-card")
    assert ok is True and reason is None


def test_plan_authorizes_case_and_whitespace_insensitive(plan_dir):
    (plan_dir / f"{kc.time.strftime('%Y-%m-%d')}-PLAN.md").write_text(
        "##   My   Card \n", encoding="utf-8")
    ok, _ = kc._plan_authorizes("my card")
    assert ok is True


def test_plan_refuses_missing_title(plan_dir):
    (plan_dir / f"{kc.time.strftime('%Y-%m-%d')}-PLAN.md").write_text(
        "- only-this\n", encoding="utf-8")
    ok, reason = kc._plan_authorizes("not-in-plan")
    assert ok is False and reason is not None


def test_plan_refuses_missing_file():
    assert kc._plan_authorizes("anything")[0] is False


def test_plan_refuses_empty_title():
    assert kc._plan_authorizes("  ")[0] is False
    assert kc._plan_authorizes(None)[0] is False


# --- e2e: CLI create honors the fail-closed default ---

@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    kb.init_db()
    return home


def _cmd_create(argv, monkeypatch, home):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", *argv])
    rc = kc._cmd_create(args)
    assert rc == 0, f"create failed rc={rc}"
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
        return tasks[-1]  # newest


def test_bare_create_parks_in_triage(hermetic_home, monkeypatch):
    task = _cmd_create(["parked-bare", "--body", BODY], monkeypatch, hermetic_home)
    assert task.status in ("triage", "todo")
    assert task.status not in ("ready", "running")


def test_plan_missing_title_parks_in_triage(hermetic_home, plan_dir):
    (plan_dir / f"{kc.time.strftime('%Y-%m-%d')}-PLAN.md").write_text(
        "- authorized-card\n", encoding="utf-8")
    task = _cmd_create(["unplanned", "--plan", "--body", BODY], None, hermetic_home)
    assert task.status in ("triage", "todo")
    assert task.status not in ("ready", "running")


def test_plan_authorized_starts(hermetic_home, plan_dir):
    (plan_dir / f"{kc.time.strftime('%Y-%m-%d')}-PLAN.md").write_text(
        "- authorized-card\n", encoding="utf-8")
    # No profiles dir in the hermetic home => ready-owner gate does not bind.
    task = _cmd_create(["authorized-card", "--plan", "--body", BODY], None, hermetic_home)
    assert task.status == "ready"


# --- regression t_cc2bbab1: bare create must not move the running counter ---

def _running_count():
    with kbc.connect_closing() as conn:
        return kb.board_stats(conn)["by_status"].get("running", 0)


def test_bare_create_leaves_running_count_unchanged(hermetic_home, monkeypatch):
    """A bare create must never nudge the running count: it stays the same and
    respects the hard ceiling (kanban.max_concurrent_workers, default 3).

    Measures BEFORE and AFTER the create (the exact assertion the e2e regression
    ticket t_cc2bbab1 demands), then cleans up the card it created.
    """
    before = _running_count()

    task = _cmd_create(["regression-check", "--body", BODY], monkeypatch, hermetic_home)
    assert task.status in ("triage", "todo"), f"bare create must park, got {task.status}"
    assert task.status not in ("ready", "running")

    after = _running_count()
    assert after == before, f"running count moved: before={before} after={after}"
    assert after <= 3, f"running count exceeds hard ceiling: {after} > 3"

    # cleanup: archive (and purge) the card this test created
    with kbc.connect_closing() as conn:
        kb.archive_task(conn, task.id)
    assert _running_count() == before