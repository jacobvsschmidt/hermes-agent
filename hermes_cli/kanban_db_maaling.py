"""Fail-closed MAALING gate for card creation (t_cd4fdff8).

Every surface that mints a card funnels through ``kanban_db.create_task`` — the
``hermes kanban create`` CLI, the ``kanban_create`` agent tool, the swarm
builder and the auto-decomposer — and every triage promotion runs through
``kanban_db.specify_triage_task``. The dashboard's own create paths
(flow-drop/prd-drop) already refuse a body without a MAALING section
(``scripts/feeders/board_receipts.py``, t_dfe6f774); this is the same contract
at the LOWEST fail-closed point, so an agent can no longer mint a card that
carries no acceptance test.

Config-gated (``kanban.require_maaling``, default off) because the MAALING
convention is a board policy, not a Hermes-wide invariant: an install that opts
in refuses every new card whose body lacks the marker; an install that does not
is untouched. The marker is the SAME one the retro metric
``nye_kort_uden_maaling`` (``scripts/night_learnings.py``) counts, so the gate
and the measurement agree: case-insensitive ``maaling`` after normalising the
Danish ``å`` to ``aa`` — so ``MAALING``, ``Maaling``, ``måling`` and ``Måling``
all count.
"""

from __future__ import annotations

from typing import Optional


def has_maaling(text: Optional[str]) -> bool:
    """True when ``text`` names a measurement (å/Å normalised to aa first).

    Mirrors ``night_learnings.MAALING_UDTRYK`` and
    ``board_receipts.has_maaling``: normalise the Danish å to aa, lowercase, then
    look for ``maaling`` so both spellings (``maaling``/``måling``) in any case
    count.
    """
    normalised = str(text or "").replace("å", "aa").replace("Å", "aa").lower()
    return "maaling" in normalised


def maaling_gate_enabled() -> bool:
    """``kanban.require_maaling`` from the active config; fail-open (False).

    The gate is inert until a board opts in, so a default install is never
    surprised by it. A config that cannot be read leaves the gate OFF — the same
    fail-open stance the ready owner gate takes where discovery is meaningless.
    """
    try:
        from hermes_cli.config import load_config

        return bool((load_config().get("kanban") or {}).get("require_maaling"))
    except Exception:  # health: allow BLE001 -- best-effort config read; unreadable config leaves the gate off (fail-open by design)
        return False


def maaling_refusal(body: Optional[str]) -> Optional[str]:
    """Refusal message when the gate binds and ``body`` has no MAALING, else None.

    Fail-closed: once the board opts in, a card without a measurable acceptance
    test is refused, naming the missing field so a feeder or agent can
    self-correct. ``None`` when the gate is off (the install default) or the
    body already carries the marker.
    """
    if not maaling_gate_enabled():
        return None
    if has_maaling(body):
        return None
    return (
        "body is missing a measurable acceptance test: add a 'MAALING' "
        "(or 'Måling') section naming the test that proves the effect "
        "(required keyword: MAALING/Måling)"
    )


# Seam: a conftest fixture may neutralize the gate by patching this module attr
# (mirrors ``kanban_db.ready_owner_refusal_impl``); board code must call the attr.
maaling_refusal_impl = maaling_refusal


def enforce_maaling(body: Optional[str], *, verb: str = "create task") -> None:
    """Raise ``ValueError`` when the gate binds and ``body`` lacks a MAALING.

    Called by ``kanban_db.create_task`` and ``kanban_db.specify_triage_task`` so
    every card-minting path shares one fail-closed point. The refusal is a
    ``ValueError`` — the same channel the ready owner gate uses — so the CLI
    prints a clean error and the ``kanban_create`` tool returns a ``tool_error``.
    """
    refusal = maaling_refusal(body)
    if refusal:
        raise ValueError(f"cannot {verb}: {refusal}")
