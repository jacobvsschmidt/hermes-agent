"""Kernel MAALING create gate (t_cd4fdff8): no new card without a measurable test.

Regression for the residual risk of t_dfe6f774. The dashboard's create paths
(flow-drop/prd-drop) already refuse a body without a MAALING section
(``scripts/feeders/board_receipts.py``), but agents mint cards directly through
the ``hermes kanban create`` CLI, the ``kanban_create`` tool, the swarm builder
and the auto-decomposer — none of which the dashboard gate can intercept. The
gate now lives at the LOWEST fail-closed point, ``kanban_db.create_task`` (plus
``specify_triage_task`` for triage promotion), config-gated by
``kanban.require_maaling`` (default off), and the marker is the exact one the
retro metric ``nye_kort_uden_maaling`` counts: the Danish å/Å normalised to aa,
then a case-insensitive ``maaling`` — so ``MAALING``, ``Maaling``, ``måling``
and ``Måling`` all count.

Every test builds a hermetic ``HERMES_HOME`` and flips the config flag, so the
default install stays untouched (the gate is inert unless a board opts in).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_maaling import has_maaling, maaling_refusal


def _home(tmp_path, monkeypatch, *, require: bool):
    """A hermetic HERMES_HOME; arms the gate when ``require`` is True."""
    home = tmp_path / ".hermes"
    home.mkdir()
    if require:
        (home / "config.yaml").write_text(
            "kanban:\n  require_maaling: true\n", encoding="utf-8"
        )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize(
    "text",
    ["MAALING: pytest -k x -> 3 passed", "Måling: ...", "måling: ...", "maaling", "MÅLING"],
)
def test_has_maaling_accepts_both_spellings_any_case(text):
    assert has_maaling(text) is True


@pytest.mark.parametrize("text", ["", None, "no acceptance test here", "goaling is not a measurement"])
def test_has_maaling_rejects_absent_marker(text):
    assert has_maaling(text) is False


def test_gate_is_inert_by_default(tmp_path, monkeypatch):
    """No config flag -> a bodyless/markerless card is created as before."""
    _home(tmp_path, monkeypatch, require=False)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(conn, title="legacy ok", assignee="ops", body="no marker")
    assert tid


def test_create_without_maaling_is_refused_and_names_the_field(tmp_path, monkeypatch):
    """Negative control: the create is refused and the error shows MAALING."""
    _home(tmp_path, monkeypatch, require=True)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        with pytest.raises(ValueError) as exc:
            kb.create_task(conn, title="unmeasured", assignee="ops", body="just do the thing")
    assert "MAALING" in str(exc.value)


def test_create_with_maaling_succeeds(tmp_path, monkeypatch):
    """Positive control: a body carrying MAALING is created unhindered."""
    _home(tmp_path, monkeypatch, require=True)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(
            conn, title="measured", assignee="ops",
            body="MAALING: `pytest -q` -> 0 failures",
        )
        row = conn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["body"].startswith("MAALING")


def test_maaling_refusal_returns_none_when_marker_present(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, require=True)
    refusal = maaling_refusal("nothing here")
    assert refusal and "MAALING" in refusal
    assert maaling_refusal("Måling: proves it") is None


def test_cli_create_without_maaling_prints_the_error(tmp_path, monkeypatch):
    """The CLI surface (the path agents/feeders use) surfaces the refusal."""
    _home(tmp_path, monkeypatch, require=True)
    from hermes_cli import kanban as kc

    with kbc.connect_closing():
        kb.create_board(slug="default", name="Test")
    out = kc.run_slash('create --assignee ops --body "just do it" "unmeasured"')
    assert "MAALING" in out


def test_specify_without_maaling_is_refused(tmp_path, monkeypatch):
    """Triage promotion (specify) lands in todo/ready, so it is gated too."""
    _home(tmp_path, monkeypatch, require=True)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(
            conn, title="triage idea", assignee="ops", triage=True,
            body="MAALING: TBD",
        )
        with pytest.raises(ValueError) as exc:
            kb.specify_triage_task(conn, tid, body="a spec with no marker", author="tester")
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
    assert "MAALING" in str(exc.value)
    assert status == "triage"  # rolled back, not promoted


def test_specify_with_maaling_promotes(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, require=True)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        tid = kb.create_task(
            conn, title="triage idea", assignee="ops", triage=True,
            body="MAALING: TBD",
        )
        ok = kb.specify_triage_task(
            conn, tid, body="**Goal** x\n\nMAALING: `pytest -q` -> 0", author="tester",
        )
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
    assert ok is True
    assert status in ("todo", "ready")


def test_decomposer_prompt_carries_maaling_when_enabled(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, require=True)
    from hermes_cli import kanban_decompose as kd

    assert "MAALING" in kd._system_prompt()


def test_decomposer_prompt_is_plain_when_disabled(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, require=False)
    from hermes_cli import kanban_decompose as kd

    assert "MAALING" not in kd._system_prompt()


def test_specifier_prompt_carries_maaling_when_enabled(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, require=True)
    from hermes_cli import kanban_specify as ksp

    assert "MAALING" in ksp._system_prompt()


def test_decompose_child_without_maaling_is_refused(tmp_path, monkeypatch):
    """Decompose children bypass create_task (raw insert), so they are gated here."""
    _home(tmp_path, monkeypatch, require=True)
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        root = kb.create_task(conn, title="idea", assignee="ops", triage=True, body="MAALING: TBD")
        with pytest.raises(ValueError) as exc:
            decompose_triage_task(
                conn, root, root_assignee="ops",
                children=[{"title": "child", "body": "no marker", "assignee": "ops", "parents": []}],
                author="tester", auto_promote=False,
            )
    assert "MAALING" in str(exc.value)


def test_decompose_child_with_maaling_is_created(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, require=True)
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        root = kb.create_task(conn, title="idea", assignee="ops", triage=True, body="MAALING: TBD")
        child_ids = decompose_triage_task(
            conn, root, root_assignee="ops",
            children=[{
                "title": "child", "body": "MAALING: `pytest -q` -> 0", "assignee": "ops", "parents": [],
            }],
            author="tester", auto_promote=False,
        )
    assert child_ids and len(child_ids) == 1
