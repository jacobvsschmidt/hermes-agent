"""REGEL 2/3 start gate (t_ab7f2b5d): a card whose created-event lineage says
``"start": False`` never promotes, claims or runs without an explicit
human/Smeden approval (`hermes kanban approve`). Auto-decomposition still runs
— decomposition is not starting — but the decomposed tree stays parked in
``todo`` until approval. Normal ``start=True`` behavior is untouched.

Regression for the observed bug: a card created bare (triage, start=False) was
auto-decomposed and 28 minutes later promoted + claimed + spawned, breaking the
`kanban create --help` promise "parked in triage and NEVER auto-starts".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_bare(conn, title="hold this"):
    """A card created exactly like the bug report: no --start, no --plan."""
    return kb.create_task(conn, title=title, triage=True)


def _decompose(conn, tid, n=1):
    children = [
        {"title": f"child {i}", "body": "work", "assignee": "ops", "parents": []}
        for i in range(n)
    ]
    return decompose_triage_task(
        conn, tid, root_assignee="orchestrator", children=children,
        author="decomposer",
    )


def test_start_false_root_decompose_does_not_auto_promote(kanban_home):
    """start=False + auto-decompose => nothing promoted/claimed without approval."""
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        child_ids = _decompose(conn, tid, n=2)
    assert child_ids is not None
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        # The root stays parked in todo (fan-in) and the children stay parked
        # in todo — none of them may sit in ready or running.
        assert root.status == "todo"
        for cid in child_ids:
            child = kb.get_task(conn, cid)
            assert child.status == "todo"
            refusal = kb.start_start_refusal(conn, cid)
            assert refusal is not None and "start=False" in refusal
        # One board-visible refusal event per parked card, no spam on re-sweeps.
        assert kb.recompute_ready(conn) == 0
        for cid in child_ids:
            kinds = [e.kind for e in kb.list_events(conn, cid)]
            assert kinds.count("start_start_refused") == 1


def test_start_false_promote_task_refused(kanban_home):
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        _decompose(conn, tid, n=1)  # gate binds within decomposition trees
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        conn.commit()
        ok, err = kb.promote_task(conn, tid, actor="ops")
    assert ok is False
    assert "skip promote: start=False" in err


def test_start_false_claim_refused_even_from_stale_ready(kanban_home):
    """Last line of defense: claim refuses and demotes a stale ready status."""
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        (child_id,) = _decompose(conn, tid, n=1)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child_id,))
        conn.commit()
        claimed = kb.claim_task(conn, child_id)
    assert claimed is None
    with kbc.connect() as conn:
        assert kb.get_task(conn, child_id).status == "todo"
        kinds = [e.kind for e in kb.list_events(conn, child_id)]
        assert "claim_rejected" in kinds


def test_gate_binds_only_to_decomposition_trees(kanban_home):
    """A bare triage card never moves on its own; an explicit human promote IS
    the approval — the gate must not demand a second blessing for it."""
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        conn.commit()
        assert kb.start_start_refusal(conn, tid) is None
        ok, err = kb.promote_task(conn, tid, actor="jacob")
    assert ok is True and err is None


def test_card_that_already_ran_is_exempt_from_the_gate(kanban_home):
    """After a first authorized start, rework/review cycles keep flowing."""
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        (child_id,) = _decompose(conn, tid, n=1)
    with kbc.connect() as conn:
        kb.approve_task_start(conn, child_id, actor="jacob")
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child_id)
        assert claimed is not None
        # Simulate the review cycle handing the card back to ready.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child_id,))
        conn.commit()
        # The gate no longer binds: the card already ran once, so review/rework
        # cycles keep flowing (claim CAS itself is exercised elsewhere).
        assert kb.start_start_refusal(conn, child_id) is None
        ok, err = kb.promote_task(conn, child_id, actor="jacob")
        assert ok is True or "start=False" not in str(err)


def test_approve_releases_start_false_card(kanban_home):
    """start=False + explicit approval => promoted (and claimable)."""
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        (child_id,) = _decompose(conn, tid, n=1)
    # Approval on the decompose root releases the whole tree (children check
    # their start origin).
    with kbc.connect() as conn:
        ok, err = kb.approve_task_start(conn, tid, actor="jacob", note="go")
        assert ok is True and err is None
        assert kb.recompute_ready(conn) >= 1
    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child.status == "ready"
        assert kb.start_start_refusal(conn, child_id) is None
        claimed = kb.claim_task(conn, child_id)
        assert claimed is not None and claimed.status == "running"


def test_approve_single_card_releases_only_that_card(kanban_home):
    with kbc.connect() as conn:
        tid = _create_bare(conn)
    with kbc.connect() as conn:
        child_ids = _decompose(conn, tid, n=2)
    with kbc.connect() as conn:
        kb.approve_task_start(conn, child_ids[0], actor="jacob")
        kb.recompute_ready(conn)
    with kbc.connect() as conn:
        assert kb.get_task(conn, child_ids[0]).status == "ready"
        assert kb.get_task(conn, child_ids[1]).status == "todo"


def test_start_true_behavior_untouched(kanban_home):
    """Normal (start=True) cards decompose and auto-promote exactly as before."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="planned work", triage=True, start_authorized=True,
        )
    with kbc.connect() as conn:
        (child_id,) = _decompose(conn, tid, n=1)
    with kbc.connect() as conn:
        assert kb.get_task(conn, child_id).status == "ready"
        assert kb.start_start_refusal(conn, child_id) is None


def test_approval_releases_dispatcher_startability(kanban_home):
    """The dispatcher's _card_startable honors the approval event end-to-end."""
    with kbc.connect() as conn:
        tid = _create_bare(conn)
        (child_id,) = _decompose(conn, tid, n=1)
    with kbc.connect() as conn:
        can, reason = kbd._card_startable(conn, child_id)
        assert can is False and reason is not None
        kb.approve_task_start(conn, tid, actor="jacob", note="go")
        can, reason = kbd._card_startable(conn, child_id)
        assert can is True and reason is None