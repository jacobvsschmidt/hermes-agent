"""No card action may fail silently.

Class of bug (2026-10-07): an action that *looks* like it worked but changed
nothing and said nothing — ``hermes kanban delete`` (not a command), and a
"remove" path that hard-deletes instead of the reversible archive. Jacob typed
"Slet denne" as a comment on two cards and they stayed put.

This suite drives every action the dashboard card UI can dispatch, reads the
card's status back from the API, and fails when the status did not change. It
additionally pins three invariants:

  * a refused/failed action surfaces a visible error (never a silent ``200 ok``),
  * removal is a reversible archive — the card survives and can be un-archived,
  * every action leaves a receipt comment on the card ("<handling> af <bruger>").

It is written to FAIL on the pre-fix backend (removal hard-deletes; the UI path
never passes an author, so no receipt is written) and to pass once the UI action
path archives and records its receipt.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/plugins/test_kanban_dashboard_plugin.py)
# ---------------------------------------------------------------------------

def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_actions_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


PREFIX = "/api/plugins/kanban"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create(client, **body):
    r = client.post(f"{PREFIX}/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


def _status(client, tid):
    r = client.get(f"{PREFIX}/tasks/{tid}")
    if r.status_code != 200:
        return None
    return r.json()["task"]["status"]


def _move(client, tid, status, **extra):
    return client.patch(f"{PREFIX}/tasks/{tid}", json={"status": status, **extra})


def _comments(client, tid):
    r = client.get(f"{PREFIX}/tasks/{tid}")
    assert r.status_code == 200, r.text
    return r.json().get("comments", [])


def _reach(client, status):
    """Create a card and drive it (through the public API) into *status*."""
    tid = _create(client, title=f"card at {status}")
    if status == "ready":
        return tid  # parentless create lands ready
    if status == "todo":
        r = _move(client, tid, "todo")
    elif status == "triage":
        r = _move(client, tid, "triage")
    elif status == "archived":
        r = _move(client, tid, "archived")
    else:
        raise AssertionError(f"no path to start status {status!r}")
    assert r.status_code == 200 and _status(client, tid) == status, (status, r.text)
    return tid


# Every status verb the card UI can dispatch (drag targets + dialog buttons +
# the archive/remove action). Each entry: (verb, start-status, expected status).
UI_STATUS_ACTIONS = [
    ("triage", "ready", "triage"),
    ("todo", "ready", "todo"),
    ("ready", "todo", "ready"),
    ("blocked", "ready", "blocked"),
    ("scheduled", "ready", "scheduled"),
    ("done", "ready", "done"),
    ("archived", "ready", "archived"),
    ("unarchived", "archived", "ready"),
]


# ---------------------------------------------------------------------------
# 1. Every UI action must change the card's status (read back from the API)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb,start,expected", UI_STATUS_ACTIONS,
                         ids=[a[0] for a in UI_STATUS_ACTIONS])
def test_every_ui_action_changes_status(client, verb, start, expected):
    tid = _reach(client, start)
    before = _status(client, tid)
    assert before == start

    r = _move(client, tid, verb,
              **({"result": "shipped", "summary": "shipped"} if verb == "done" else {}))
    assert r.status_code == 200, f"{verb}: HTTP {r.status_code}: {r.text}"

    after = _status(client, tid)
    assert after is not None, f"{verb}: card vanished — removal must not be silent"
    assert after == expected, f"{verb}: expected {expected!r}, card is {after!r}"

    if (before == expected):
        pytest.fail(f"{verb}: status did not change (stayed {before!r}) — silent no-op")


def test_bulk_move_changes_every_card(client):
    a = _create(client, title="a")
    b = _create(client, title="b")
    r = client.post(f"{PREFIX}/tasks/bulk", json={"ids": [a, b], "status": "blocked"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert all(x["ok"] for x in results), results
    assert _status(client, a) == "blocked"
    assert _status(client, b) == "blocked"


# ---------------------------------------------------------------------------
# 2. Negative: a failed/refused action is a visible error, never a silent ✓
# ---------------------------------------------------------------------------

def test_unknown_status_is_rejected_not_silently_ok(client):
    tid = _create(client, title="x")
    r = _move(client, tid, "bogus-status")
    assert r.status_code >= 400, f"unknown status must be refused, got {r.status_code}"
    assert _status(client, tid) == "ready", "a refused action must not change status"


def test_running_is_rejected_not_silently_ok(client):
    tid = _create(client, title="x")
    r = _move(client, tid, "running")
    assert r.status_code == 400, r.text
    assert _status(client, tid) == "ready"


def test_refused_transition_names_cause_and_keeps_status(client):
    parent = _create(client, title="open parent")
    child = _create(client, title="gated child", parents=[parent])
    assert _status(client, child) == "todo", "a gated child starts in todo"

    r = _move(client, child, "done")
    assert r.status_code == 409, f"open parent must refuse done (visible error), got {r.status_code}"
    assert "parent" in r.json().get("detail", "").lower(), r.text
    assert _status(client, child) == "todo", "a refused done must not change status"


def test_delete_missing_card_is_404_not_ok(client):
    r = client.delete(f"{PREFIX}/tasks/t_does_not_exist")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 3. Removal is a reversible archive (delete->archive regression)
# ---------------------------------------------------------------------------

def test_delete_archives_and_is_reversible(client):
    """The 'remove' action must ARCHIVE (soft delete): the card survives, is
    retrievable, and can be restored — never silently hard-deleted."""
    tid = _create(client, title="to remove")

    r = client.delete(f"{PREFIX}/tasks/{tid}")
    assert r.status_code == 200, r.text

    detail = client.get(f"{PREFIX}/tasks/{tid}")
    assert detail.status_code == 200, (
        "removal hard-deleted the card — it must be a reversible archive"
    )
    assert detail.json()["task"]["status"] == "archived"

    # Reversible: put it back.
    back = _move(client, tid, "unarchived")
    assert back.status_code == 200, back.text
    assert _status(client, tid) == "ready"


# ---------------------------------------------------------------------------
# 4. Every action leaves a receipt comment on the card
# ---------------------------------------------------------------------------

RECEIPT_ACTIONS = [
    ("archived", "ready"),
    ("unarchived", "archived"),
]


@pytest.mark.parametrize("verb,start", RECEIPT_ACTIONS, ids=[a[0] for a in RECEIPT_ACTIONS])
def test_action_writes_receipt_comment(client, verb, start):
    tid = _reach(client, start)
    before = len(_comments(client, tid))

    r = _move(client, tid, verb)
    assert r.status_code == 200, r.text

    comments = _comments(client, tid)
    assert len(comments) > before, f"{verb}: no receipt comment was written on the card"

    receipt = comments[-1]
    assert receipt["author"], f"{verb}: receipt has no author"
    body = receipt["body"]
    assert receipt["author"] in body, f"{verb}: receipt must name who did it, got {body!r}"
    assert "kl." in body and re.search(r"\d{2}:\d{2}", body), (
        f"{verb}: receipt must say when it happened, got {body!r}"
    )


def test_delete_writes_receipt_comment(client):
    tid = _create(client, title="to remove")
    r = client.delete(f"{PREFIX}/tasks/{tid}")
    assert r.status_code == 200, r.text

    detail = client.get(f"{PREFIX}/tasks/{tid}")
    assert detail.status_code == 200, "removal must archive, not hard-delete"
    comments = detail.json().get("comments", [])
    assert any(c.get("author") and "arkiver" in c["body"].lower() for c in comments), (
        f"removal must leave a receipt comment, got {comments!r}"
    )
