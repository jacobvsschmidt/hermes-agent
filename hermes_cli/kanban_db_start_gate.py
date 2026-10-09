"""REGEL 2/3 start gate (t_ab7f2b5d) — promote/claim approval gate.

Extracted from ``kanban_db.py`` (topical sibling per the health ratchet): a
card created without explicit start authorization records ``"start": False``
on its created event (and ``kanban create --help`` promises it "NEVER
auto-starts"). Decomposition is allowed to run, but nothing may promote, claim
or spawn such a card (or any child decomposed from it) until a human / Smeden
approval is recorded via ``hermes kanban approve <task_id>``.

Imports ``kanban_db`` lazily inside function bodies to avoid the import cycle
(``kanban_db`` re-exports this module's public functions).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional


def _json_dict(value: Any) -> dict:
    try:
        v = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else (value or {})
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError) as exc:  # malformed payload JSON: log and treat as empty
        import logging
        logging.getLogger(__name__).debug("start gate: unparsable event payload: %s", exc)
        return {}


def _row_get(row, col, default=None):
    try:
        return row[col]
    except (IndexError, KeyError, TypeError):
        return default


def _kb():
    from hermes_cli import kanban_db as kb
    return kb


# --- REGEL 2/3 start gate (t_ab7f2b5d) --------------------------------------
# A card created without explicit start authorization records ``"start": False``
# on its created event (and `kanban create --help` promises it "NEVER
# auto-starts"). Decomposition is allowed to run, but nothing may promote,
# claim or spawn such a card (or any child decomposed from it) until a human /
# Smeden approval is recorded via `hermes kanban approve <task_id>`.

START_APPROVAL_EVENT = "start_approved"
_START_REFUSAL_EVENT = "start_start_refused"
_START_ORIGIN_MAX_DEPTH = 8


def _start_flag_task_id(
    conn: sqlite3.Connection, task_id: str, _depth: int = 0,
) -> Optional[str]:
    """Return the task id whose created event carries the ``start`` flag.

    Decomposed children carry ``from_decompose_of`` on their created event and
    inherit the root's start intent; follow that chain (bounded) up to the
    origin card. ``None`` when no created event in the chain states a flag.
    """
    if _depth > _START_ORIGIN_MAX_DEPTH:
        return None
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload")) if row else {}
    if "start" in payload:
        return task_id
    origin = payload.get("from_decompose_of")
    if isinstance(origin, str) and origin:
        return _start_flag_task_id(conn, origin, _depth + 1)
    return None


def _has_decompose_lineage(conn, task_id: str) -> bool:
    """Whether this card belongs to an auto-decomposition tree: it is itself a
    decompose root (a ``decomposed`` event) or was created by one. The REGEL 2/3
    start gate binds only here — a bare triage card never moves toward running
    on its own, and an explicit human ``promote``/``specify`` IS the approval
    for everything outside a decomposition tree."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload")) if row else {}
    if isinstance(payload.get("from_decompose_of"), str) and payload.get("from_decompose_of"):
        return True
    return bool(conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decomposed' LIMIT 1",
        (task_id,),
    ).fetchone())


def _has_been_started(conn, task_id: str) -> bool:
    """Whether the card has legitimately run at least once. After a first
    authorized start the create-time flag no longer governs the card — rework
    and review cycles must keep flowing."""
    return bool(conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'claimed' LIMIT 1",
        (task_id,),
    ).fetchone())


def start_start_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """REGEL 2/3 guard: refusal reason when this card may not start yet.

    Returns ``None`` when the card is authorized to start: no ``start`` flag in
    its created-event lineage, the flag is True, an explicit ``start_approved``
    event exists on the card itself or on its start origin, the card already
    ran once, or the card is not part of a decomposition tree (its only exits
    from triage are explicit human actions).
    """
    if _has_been_started(conn, task_id) or not _has_decompose_lineage(conn, task_id):
        return None
    origin_id = _start_flag_task_id(conn, task_id)
    if origin_id is None:
        return None
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (origin_id,),
    ).fetchone()
    if not row or _json_dict(_row_get(row, "payload")).get("start") is not False:
        return None
    for candidate in (task_id, origin_id if origin_id is not None else task_id):
        approved = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
            (candidate, START_APPROVAL_EVENT),
        ).fetchone()
        if approved:
            return None
    return (
        "start=False, awaiting approval "
        f"(approve with `hermes kanban approve {task_id}`)"
    )


def start_approval_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether an explicit ``start_approved`` event exists on this card or on
    its start origin. Shared by the promote/claim gate and the dispatcher's
    ``_card_startable`` so an approval releases a card end-to-end."""
    candidates = [task_id]
    origin_id = _start_flag_task_id(conn, task_id)
    if origin_id is not None and origin_id != task_id:
        candidates.append(origin_id)
    for candidate in candidates:
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
            (candidate, START_APPROVAL_EVENT),
        ).fetchone():
            return True
    return False


def approve_task_start(
    conn: sqlite3.Connection, task_id: str, *, actor: Optional[str] = None,
    note: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Record an explicit human/Smeden start approval on a card.

    Approving the start origin releases the whole decomposed tree (children
    check their origin); approving a single card releases only that card.
    """
    if _kb()._task_status(conn, task_id) is None:
        return False, f"task {task_id} not found"
    payload: dict[str, Any] = {"actor": actor}
    if note:
        payload["note"] = note
    with _kb().write_txn(conn):
        _kb()._append_event(conn, task_id, START_APPROVAL_EVENT, payload)
    return True, None


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Operator promotion ``todo``/``blocked`` -> ``ready`` with an audit event.
    Refused while a parent is unfinished; ``dry_run`` only validates.
    Returns ``(ok, reason)``."""
    cur_status = _kb()._task_status(conn, task_id)
    if cur_status is None:
        return False, f"task {task_id} not found"

    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    # REGEL 2/3 start gate (t_ab7f2b5d): a start=False card is never promoted
    # without explicit approval, whichever writer drives the promotion.
    start_refusal = start_start_refusal(conn, task_id)
    if start_refusal:
        return False, f"skip promote: {start_refusal}"

    # No override: claim_task demotes ready -> todo on an undone parent whichever
    # writer set 'ready', so a forced promotion would only report a success the
    # first claim silently reverts (#106195). The dependency itself is the knob.
    parents = conn.execute(
        "SELECT t.id, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?", (task_id,),
    ).fetchall()
    unsatisfied = [p["id"] for p in parents if p["status"] not in ("done", "archived")]
    if unsatisfied:
        return False, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents or drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`)"
        )

    if dry_run:
        return True, None

    with _kb().write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')", (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _kb()._append_event(conn, task_id, "promoted_manual", {"actor": actor, "reason": reason})

    return True, None
