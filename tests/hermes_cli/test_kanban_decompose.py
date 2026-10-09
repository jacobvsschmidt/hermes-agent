"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    # start_authorized=True: mechanics test, not the REGEL 2/3 start gate
    # (see test_kanban_start_gate.py — an unauthorized root parks in todo).
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True, start_authorized=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_children_inherit_root_assignee_when_unrouted(kanban_home):
    """Unrouted children fall back to the ROOT task's assignee, not
    the decomposer's active profile (#114294). The active profile here is ``private``
    (an incognito profile with no credentials), so the old fallback spawned
    workers that deadlocked on capability blockers."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="zdr", triage=True, start_authorized=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    # get_active_profile_name() is mocked to names[0] = "private" — the
    # global default chain would resolve there without kanban.default_assignee.
    patches = _patch_list_profiles(["private", "zdr"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "zdr"
    assert c1.assignee == "zdr"
    # Same class for the root: no ``orchestrator_profile`` must not hand the
    # orchestration card to the dispatcher's own (here: incognito) profile.
    assert root.assignee == "zdr"


def test_decompose_explicit_default_assignee_wins_over_root_assignee(kanban_home):
    """An explicitly configured ``kanban.default_assignee`` stays
    authoritative for unroutable children; the root task's assignee only
    fills in when no explicit default is set (explicit config → card
    assignee → active profile)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="engineer", triage=True, start_authorized=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["engineer", "docs", "private"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "docs"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "docs"
    assert c1.assignee == "docs"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_load_routing_falls_back_to_defaults_when_config_unreadable(kanban_home, monkeypatch):
    """decompose_task promises ok=False on expected failures; a config read that raises (missing
    profile home, HomeInitializationError) must not escape _load_routing as an exception."""
    from hermes_cli import config as config_mod

    def _boom():
        raise FileNotFoundError("profile home is gone")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    routing = decomp._load_routing()
    assert routing.default_assignee == "default" and routing.auto_promote is True


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False


def test_decompose_sequential_pipeline_chains_parents(kanban_home):
    """Regression for t_4fc1c39a: a sequential phase pipeline (the SAB CLOB
    v2-migration shape) must fan out as a parent CHAIN, so the review/verify
    cards do NOT become ``ready`` before the implementation card is ``done``.

    Reproduces the original defect: the auto-decomposer emitted 5 flat
    siblings with no dependency edges, so verify/review ran on a prod host
    where the migration did not exist yet.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="migrate order path to CLOB v2", triage=True, start_authorized=True)

    # install -> implement/signer -> post/integrate -> review -> verify
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "sequential migration pipeline",
        "tasks": [
            {"title": "install v2 client", "body": "install", "assignee": "infra", "parents": []},
            {"title": "build v2 signer", "body": "sign", "assignee": "engineer", "parents": [0]},
            {"title": "post orders via v2", "body": "post", "assignee": "engineer", "parents": [1]},
            {"title": "review migration diff", "body": "review", "assignee": "reviewer", "parents": [2]},
            {"title": "live-verify v2 order path", "body": "verify", "assignee": "ops", "parents": [3]},
        ],
    })

    patches = _patch_list_profiles(["orch", "infra", "engineer", "reviewer", "ops"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    ids = outcome.child_ids
    assert ids and len(ids) == 5

    with kbc.connect() as conn:
        heads = [kb.get_task(conn, i) for i in ids]
    # Only the head of the pipeline is runnable; every downstream card waits.
    assert heads[0].status == "ready"
    assert [t.status for t in heads[1:]] == ["todo"] * 4
    # The verify card is NOT ready while the implementation is not done.
    assert heads[4].status == "todo"
    assert heads[4].assignee == "ops"


def test_verify_not_ready_until_implementation_done(kanban_home):
    """End-to-end gating: advance the chain one card at a time and prove the
    review/verify cards only promote AFTER their parent completes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="seq pipeline", triage=True, start_authorized=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "install -> impl -> review -> verify",
        "tasks": [
            {"title": "install", "body": "x", "assignee": "infra", "parents": []},
            {"title": "implement", "body": "x", "assignee": "engineer", "parents": [0]},
            {"title": "review", "body": "x", "assignee": "reviewer", "parents": [1]},
            {"title": "verify", "body": "x", "assignee": "ops", "parents": [2]},
        ],
    })

    patches = _patch_list_profiles(["orch", "infra", "engineer", "reviewer", "ops"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    install, impl, review, verify = outcome.child_ids

    def status(conn, cid):
        return kb.get_task(conn, cid).status

    with kbc.connect() as conn:
        assert status(conn, install) == "ready"
        assert status(conn, impl) == "todo"
        assert status(conn, review) == "todo"
        assert status(conn, verify) == "todo"

    # Complete install -> only implement promotes; review/verify still wait.
    with kbc.connect() as conn:
        assert kb.complete_task(conn, install, result="installed")
    with kbc.connect() as conn:
        assert status(conn, impl) == "ready"
        assert status(conn, review) == "todo"
        assert status(conn, verify) == "todo"  # NOT ready before implementation done

    # Complete implement -> review promotes, verify still waits.
    with kbc.connect() as conn:
        assert kb.complete_task(conn, impl, result="implemented")
    with kbc.connect() as conn:
        assert status(conn, review) == "ready"
        assert status(conn, verify) == "todo"

    # Complete review -> verify finally promotes (only now).
    with kbc.connect() as conn:
        assert kb.complete_task(conn, review, result="reviewed")
    with kbc.connect() as conn:
        assert status(conn, verify) == "ready"


def test_system_prompt_requires_dependency_chains_for_pipelines():
    """Guard: the decomposer prompt must instruct the LLM to encode
    sequential pipelines as parent chains and never emit a review/verify
    sibling without parents (t_4fc1c39a)."""
    p = decomp._SYSTEM_PROMPT.lower()
    assert "parent chain" in p
    assert "sequential" in p
    assert "verify" in p and "review" in p


def test_decompose_integration_card_gated_on_all_impls(kanban_home):
    """Regression for t_783d9174: one coherent change to a deployed service
    (a new order-signer AND a new order-client for the same live order path)
    must end in an integration/deploy card whose parents are EVERY
    implementation child. Prove that integration card does NOT become
    ``ready`` until all implementation cards are ``done`` — the acceptance
    criterion for this card.

    Reproduces the original defect: the two halves of the same change drifted
    into different trees (repo=signer, prod host=client) and no card owned the
    integration, so nothing reconciled them.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="migrate live order path to v2", triage=True, start_authorized=True)

    # Two independent impl cards (signer / client) feed ONE integration/deploy
    # card whose parents are both of them.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "one coherent change -> two impls + one integration/deploy card",
        "tasks": [
            {"title": "build v2 signer", "body": "sign", "assignee": "engineer", "parents": []},
            {"title": "move client to v2", "body": "post", "assignee": "engineer", "parents": []},
            {"title": "integrate + deploy v2 on prod", "body": "deploy", "assignee": "ops", "parents": [0, 1]},
        ],
    })

    patches = _patch_list_profiles(["orch", "engineer", "ops"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    signer, client, integrate = outcome.child_ids

    def status(conn, cid):
        return kb.get_task(conn, cid).status

    with kbc.connect() as conn:
        # Both impls run in parallel; the integration/deploy card waits.
        assert status(conn, signer) == "ready"
        assert status(conn, client) == "ready"
        assert status(conn, integrate) == "todo"
        assert kb.get_task(conn, integrate).assignee == "ops"

    # Complete only ONE impl -> integration must STILL wait.
    with kbc.connect() as conn:
        assert kb.complete_task(conn, signer, result="signer built")
    with kbc.connect() as conn:
        assert status(conn, integrate) == "todo"

    # Complete the second impl -> only now does integration/deploy promote.
    with kbc.connect() as conn:
        assert kb.complete_task(conn, client, result="client moved")
    with kbc.connect() as conn:
        assert status(conn, integrate) == "ready"


def test_system_prompt_requires_integration_deploy_and_single_host_writer():
    """Guard for t_783d9174: the prompt must require an explicit
    integration/deploy card for a coherent change to a deployed service, make
    it the ONLY host writer, and forbid building on an uncommitted prod-host
    edit (drift)."""
    p = decomp._SYSTEM_PROMPT.lower()
    assert "integration/deploy" in p
    assert "one host writer" in p
    assert "prod host" in p or "production host" in p
    assert "drift" in p


def test_system_prompt_requires_prereq_edges():
    """Guard for t_e45b2d7e: the prompt must tell the decomposer that a
    prerequisite marker in a child body MUST come with a matching parent edge."""
    p = decomp._SYSTEM_PROMPT.lower()
    assert "forudsætning" in p
    assert "prerequisite" in p
    assert "parents" in p


# --- t_e45b2d7e: declared prerequisites become real parent edges ------------

def _child(title, body, **kw):
    base = {"title": title, "body": body, "assignee": None, "parents": []}
    base.update(kw)
    return base


def test_enforce_prereq_edges_plural_depends_on_all_preceding():
    """A plural reference ("Forældre-taskene (A + B) er landet") with no parent
    edge depends on EVERY preceding sibling."""
    children = [
        _child("classifier", "add error_type invalid_maker_amount + bucket"),
        _child("logging", "log signerings-parametre post-fejl: price size tick"),
        _child(
            "tests",
            "FORUDSÆTNING: Forældre-taskene (klassifikation + logning) er landet.\n\nARBEJDE: unit-tests.",
        ),
    ]
    out = decomp._enforce_declared_prereq_edges(children)
    assert out[2]["parents"] == [0, 1]
    # Untouched siblings keep no parents (still parallel).
    assert out[0]["parents"] == [] and out[1]["parents"] == []


def test_enforce_prereq_edges_singular_lexical_match():
    """A singular reference ("Logning af signerings-parametre (forælder)") is
    resolved to the preceding sibling whose title/body shares its tokens."""
    children = [
        _child("classifier", "add error_type invalid_maker_amount + bucket"),
        _child("Log signerings-parametre ved place_order post-fejl", "log price size tick makerAmount"),
        _child(
            "reproducer",
            "FORUDSÆTNING: Logning af signerings-parametre (forælder) er landet, "
            "så de loggede price, size, tick og makerAmount er tilgængelige.",
        ),
    ]
    out = decomp._enforce_declared_prereq_edges(children)
    assert out[2]["parents"] == [1]


def test_enforce_prereq_edges_keeps_llm_supplied_parents():
    """When the LLM already encoded an edge, the repair never overrides it."""
    children = [
        _child("a", "x"),
        _child("b", "y"),
        _child("c", "FORUDSÆTNING: forælder er landet", parents=[0]),
    ]
    out = decomp._enforce_declared_prereq_edges(children)
    assert out[2]["parents"] == [0]


def test_enforce_prereq_edges_falls_back_to_nearest_preceding():
    """A marker with no lexical overlap still gets the nearest preceding
    sibling so the child is never emitted as a parallel sibling."""
    children = [
        _child("a", "alpha work"),
        _child("b", "FORUDSÆTNING: noget helt andet er landet"),
    ]
    out = decomp._enforce_declared_prereq_edges(children)
    assert out[1]["parents"] == [0]


def test_enforce_prereq_edges_no_preceding_sibling_is_left_alone():
    children = [_child("only", "FORUDSÆTNING: ekstern forudsætning er landet")]
    out = decomp._enforce_declared_prereq_edges(children)
    assert out[0]["parents"] == []


def test_decompose_prereq_marker_becomes_parent_edge(kanban_home):
    """Regression for t_e45b2d7e (the SAB 'invalid maker amount' split): the
    decomposer emitted four FLAT siblings even though two of them declared a
    prerequisite on the other two, so the test card ran in parallel and had to
    re-implement the classifier + logging itself. After the fix the declared
    prerequisites become parent edges and the dependent cards stay ``todo``
    until their producers are ``done``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="[SAB] new error class invalid maker amount", triage=True, start_authorized=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "classifier + logging in parallel, then tests + reproducer",
        "tasks": [
            {"title": "classifier + alarm-bucket", "body": "add error_type invalid_maker_amount",
             "assignee": "engineer", "parents": []},
            {"title": "Log signerings-parametre ved place_order post-fejl",
             "body": "log price size tick makerAmount", "assignee": "engineer", "parents": []},
            {"title": "unit-tests",
             "body": "FORUDSÆTNING: Forældre-taskene (klassifikation + logning) er landet.\n\nARBEJDE: tests.",
             "assignee": "engineer", "parents": []},
            {"title": "reproducer mod CLOB",
             "body": "FORUDSÆTNING: Logning af signerings-parametre (forælder) er landet, "
                     "så de loggede price, size, tick og makerAmount er tilgængelige.",
             "assignee": "engineer", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orch", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    ids = outcome.child_ids
    assert ids and len(ids) == 4
    classifier, logging, tests, reproducer = ids

    def status(conn, cid):
        return kb.get_task(conn, cid).status

    with kbc.connect() as conn:
        # The two producers run in parallel; the dependent cards WAIT.
        assert status(conn, classifier) == "ready"
        assert status(conn, logging) == "ready"
        assert status(conn, tests) == "todo"       # would be "ready" without the fix
        assert status(conn, reproducer) == "todo"  # would be "ready" without the fix

    # Complete classifier only -> tests still wait for the logging card.
    with kbc.connect() as conn:
        assert kb.complete_task(conn, classifier, result="done")
    with kbc.connect() as conn:
        assert status(conn, tests) == "todo"
        assert status(conn, reproducer) == "todo"

    # Complete logging -> both dependent cards finally promote.
    with kbc.connect() as conn:
        assert kb.complete_task(conn, logging, result="done")
    with kbc.connect() as conn:
        assert status(conn, tests) == "ready"
        assert status(conn, reproducer) == "ready"





