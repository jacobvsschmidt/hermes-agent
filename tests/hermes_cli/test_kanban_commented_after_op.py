"""Regression: a refused block/schedule/unblock must not write its
``PREFIX: reason`` comment (t_69bbb9d5).

``hermes kanban block`` used to write its ``BLOCKED: <reason>`` comment BEFORE
running the block op. When the op was refused (e.g. blocking a ``todo`` card --
``block_task`` only accepts ``running``/``ready``) the comment was still added.
Combined with the dropbox retry loop that meant one identical comment per retry,
so card ``t_d2130b2a`` accumulated nine identical ``BLOCKED: ...`` comments for
a block that never happened. The comment must now be written only when the op
actually succeeds.
"""

from __future__ import annotations

import argparse
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


def _block_args(tid: str, reason: str) -> argparse.Namespace:
    return argparse.Namespace(task_id=tid, ids=[], reason=[reason], kind=None)


def _comments(tid: str) -> list[str]:
    with kbc.connect() as conn:
        return [c.body for c in kb.list_comments(conn, tid)]


def test_commented_writes_comment_only_after_success():
    """The wrapper itself must call the op first and comment only on truthy."""
    calls: list[str] = []
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="wrapper card", assignee="alice")
        ran = kc._commented(conn, "why", "tester", "BLOCKED",
                            lambda t: calls.append(t) or False)
        assert ran(tid) is False
        assert calls == [tid]
        assert [c.body for c in kb.list_comments(conn, tid)] == []


def test_refused_block_leaves_no_comment(kanban_home):
    """Blocking a ``todo`` card is refused -> no ``BLOCKED:`` comment is written."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="todo child", assignee="alice",
                               parents=[parent])
        status = kb.get_task(conn, child).status
    assert status == "todo", "child must be parent-gated into todo"

    rc = kc._cmd_block(_block_args(child, "cannot happen"))

    assert rc == 1, "blocking a todo card must be refused"
    assert _comments(child) == [], (
        "a refused block must not leave a BLOCKED comment on the card"
    )


def test_successful_block_writes_comment(kanban_home):
    """A block that actually lands still records its ``BLOCKED: <reason>`` comment."""
    with kbc.connect() as conn:
        ready = kb.create_task(conn, title="ready card", assignee="alice")
        assert kb.get_task(conn, ready).status == "ready"

    rc = kc._cmd_block(_block_args(ready, "needs input"))

    assert rc == 0
    comments = _comments(ready)
    assert comments == ["BLOCKED: needs input"], comments
