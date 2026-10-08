"""Board integrity (t_e3cf9fcc): a ``needs_input`` escalation must not be
swept away by an automated ``unblock``.

LIVE BUG (measured 2026-10-08 21:32, kanban.db): ~30 cards a reviewer/worker had
parked ``blocked`` with ``kind=needs_input`` ("waiting for a human") were
unblocked in one sweep by a script that looped ``hermes kanban unblock <id>``
with no reason and no actor. The cards respawned in the review lane with no new
information, and the human escalation never reached the operator — a board that
says "waiting for you" while silently removing the wait is a board that lies.

The fix: :func:`kanban_db.unblock_task` refuses to lift a ``needs_input`` block
unless the caller passes an EXPLICIT human authorisation
(``allow_needs_input=True``); the refusal is recorded as a visible
``unblock_refused`` event. The CLI only passes that authorisation for the
conscious ``--force`` flag. Every ``unblocked`` event carries ``actor``/``reason``
when supplied, so no unblock is left without a trace.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _blocked_needs_input(conn, title: str = "needs a human") -> str:
    tid = kb.create_task(conn, title=title)
    kb.claim_task(conn, tid)
    assert kb.block_task(
        conn, tid, reason="escalation: only a human may decide this",
        kind="needs_input", expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _events(conn, tid: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (tid, kind),
    ).fetchall()
    import json
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


# ---------------------------------------------------------------------------
# 1. The escalation survives an automated unblock
# ---------------------------------------------------------------------------


def test_needs_input_survives_automated_unblock(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _blocked_needs_input(conn)

        # The exact shape the offending sweep used: no reason, no actor, no force.
        assert kb.unblock_task(conn, tid) is False
        assert kb.get_task(conn, tid).status == "blocked", "escalation was swept away"

        # The refusal is visible on the card (never a silent no-op).
        refused = _events(conn, tid, "unblock_refused")
        assert len(refused) == 1
        assert refused[0]["kind"] == "needs_input"
        assert "authorisation" in refused[0]["reason"]
        assert _events(conn, tid, "unblocked") == []

        # It stays put across repeated sweep attempts (idempotent immunity).
        for _ in range(3):
            assert kb.unblock_task(conn, tid) is False
        assert kb.get_task(conn, tid).status == "blocked"


def test_recompute_ready_does_not_promote_needs_input(kanban_home: Path) -> None:
    """The dispatcher's own promotion path must also leave it blocked."""
    with kbc.connect() as conn:
        tid = _blocked_needs_input(conn)
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# 2. An explicit human authorisation lifts it, with a trace
# ---------------------------------------------------------------------------


def test_explicit_human_authorisation_unblocks_with_trace(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _blocked_needs_input(conn)

        assert kb.unblock_task(
            conn, tid, allow_needs_input=True, actor="jacob",
            reason="answered on the card, go ahead",
        ) is True
        assert kb.get_task(conn, tid).status != "blocked"

        ev = _events(conn, tid, "unblocked")
        assert len(ev) == 1
        assert ev[0]["actor"] == "jacob"
        assert ev[0]["reason"] == "answered on the card, go ahead"


# ---------------------------------------------------------------------------
# 3. Only needs_input is gated — ordinary blocks still auto-recover
# ---------------------------------------------------------------------------


def test_non_needs_input_block_still_unblocks_freely(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="capability wall")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid, reason="no access to the box", kind="capability",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status != "blocked"


# ---------------------------------------------------------------------------
# 4. CLI: needs_input requires the conscious --force acknowledgement
# ---------------------------------------------------------------------------


def _unblock_args(tid: str, *, reason=None, force=False) -> argparse.Namespace:
    return argparse.Namespace(task_ids=[tid], reason=reason, force=force)


def test_cli_unblock_refuses_needs_input_without_force(kanban_home: Path, capsys) -> None:
    with kbc.connect() as conn:
        tid = _blocked_needs_input(conn)

    rc = kc._cmd_unblock(_unblock_args(tid))
    assert rc != 0
    assert "cannot unblock" in (capsys.readouterr().err + capsys.readouterr().out)

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_cli_unblock_force_lifts_needs_input(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _blocked_needs_input(conn)

    rc = kc._cmd_unblock(_unblock_args(tid, reason="human decided", force=True))
    assert rc == 0

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status != "blocked"
        assert _events(conn, tid, "unblocked")[0]["reason"] == "human decided"
