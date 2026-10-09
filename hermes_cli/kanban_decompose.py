"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the gateway
dispatcher's auto-decompose path. Reads the profile roster (with
descriptions), asks the auxiliary LLM for a task graph in JSON, then
atomically creates the children, links them under the root, and flips the
root ``triage -> todo``. The root stays alive as parent of every leaf child so
it wakes back up when the graph completes and its assignee (the orchestrator
profile) can judge completion and add more work.

Mirrors ``kanban_specify`` (lazy aux import, lenient parse, never raises on
expected failures). ``fanout=false`` collapses to the ``specify`` behaviour
(tighten + promote, no children), making ``decompose`` a strict superset.
Unknown assignees are rewritten to ``default_assignee`` — a child NEVER ends
up with ``assignee=None``.

Dependency semantics (the SAB v2-migration lesson, ``t_4fc1c39a``): the child
graph must mirror REAL data dependencies. A sequential/phase pipeline
(install -> implement -> post/integrate -> review -> verify) is emitted as a
parent CHAIN, never as flat siblings, so the dispatcher cannot start a review
or verify card before the implementation it checks exists. The system prompt
below encodes this rule; the DB layer (``kanban_db_graph``) enforces it by
leaving a ``todo`` child un-promoted until every parent is ``done``.

Ownership semantics (the split-migration lesson, ``t_783d9174``): a coherent
change to a deployed service must end in ONE integration/deploy card whose
parents are every implementation child, and ONLY that card may write to the
production host. Implementation children land their work in the repository on
the one feature branch (commit -> PR); the deploy card deploys from that
committed branch. A direct, uncommitted edit on a prod host is drift — the
prompt below tells the decomposer to flag and reconcile it, never to build on
it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import profiles as profiles_mod
from hermes_cli.kanban_specify import (
    _call_aux, _extract_json_blob, _load_triage_task, _task_prompt_fields, _title_body,
)
from hermes_cli.kanban_specify import _profile_author as _specify_author

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Model real dependencies, not convenience order. Before emitting each
    child, ask: "does this task consume an artifact, decision, or code that
    another child produces?" If yes, that other child MUST be listed in this
    child's "parents".
  - Prefer parallelism ONLY for work that is genuinely independent: if two
    tasks can run at the same time without one consuming the other's output,
    give them no parents so the dispatcher fans them out at once.
  - Recognize SEQUENTIAL / PHASE pipelines and encode them as a parent CHAIN
    (a "spine"), NEVER as flat siblings. When the work has ordered phases
    where each phase consumes the previous phase's output — e.g.
    install/setup -> implement/change -> integrate/post -> review -> verify —
    the later task MUST list the earlier one in "parents" (chain:
    tasks[1].parents=[0], tasks[2].parents=[1], tasks[3].parents=[2], ...).
    A migration, deploy, or any "do X, then Y, then Z" flow is sequential.
  - A verification, review, or live-verify task is ALWAYS downstream of the
    task it checks: its "parents" MUST include the implementation task (directly
    or through the chain), so the dispatcher cannot start it before the code /
    artifact exists. NEVER emit a review/verify task as a sibling with no
    parents. A verify card that runs before its implementation is physically
    impossible to complete.
  - When unsure whether two tasks are independent, prefer the dependency edge.
    A child that waits slightly too long is recoverable; a review/verify that
    runs before its implementation is not.
  - If you write a prerequisite into a child's body — a marker line such as
    "FORUDSÆTNING: <other task> er landet" / "Prerequisite: <other task> has
    landed" / "Afhænger af: <other task>" — you MUST also list the task that
    produces that prerequisite in this child's "parents". A body that declares
    a dependency while carrying no parent edge is a CONTRACT VIOLATION: the
    dispatcher would run the child in parallel with the work it presupposes.
    Emit the marker and the parent edge together, always. (This is enforced:
    the system repairs a marker with no edge, but you should never rely on it.)
  - ONE COHERENT CHANGE TO A DEPLOYED SERVICE MUST END IN AN INTEGRATION/DEPLOY
    CARD. When two or more children are parts of the SAME change to one running
    service (e.g. a new order-signer in one card and a new order-client in
    another, both for the same live order path), emit an explicit
    integration/deploy card whose "parents" list EVERY implementation child
    (parents: [i, j, ...]). The change must land as ONE coordinated unit, never
    as separate pieces that drift apart, and the integration card waits until
    all of them are done.
  - "ONE HOST WRITER" — ONLY the integration/deploy card may write to the
    production host or run a deploy. No implementation child touches the live
    host; it lands its change in the repository on the ONE feature branch
    (commit -> PR). The deploy card deploys FROM that committed branch, so the
    running service and the repository never disagree.
  - NEVER emit a task that edits a production host directly. An edit applied on
    the host that is not in the repository is DRIFT, not a fix: the next
    repo-based deploy silently reverts it. If you find (or a child reports) an
    uncommitted host edit, emit a task to RECONCILE it — commit it into the
    feature branch (or revert it) — and never build further on the uncommitted
    state. A change that lives only on one host is invisible to review and lost
    on the next deploy.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""

# Appended to the system prompt when the board opts into the MAALING gate
# (kanban.require_maaling, t_cd4fdff8): every child body — and the single-task
# body — must carry the marker, or create_task/specify_triage_task refuse it.
_MAALING_INSTRUCTION = """

BOARD RULE — MAALING: the board refuses any card whose body lacks a measurable
acceptance test. EVERY child body (and the single-task body when fanout=false)
MUST contain a section headed MAALING (or Måling) followed by the concrete test
that proves the change worked: the exact command/metric to run and the expected
result. A body without that marker is rejected at creation.
"""


def _system_prompt() -> str:
    """The decomposer prompt, plus the MAALING contract when the board opts in."""
    from hermes_cli.kanban_db_maaling import maaling_gate_enabled

    return _SYSTEM_PROMPT + _MAALING_INSTRUCTION if maaling_gate_enabled() else _SYSTEM_PROMPT


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# A child that *declares* a prerequisite in its body ("FORUDSÆTNING: … er
# landet", "Prerequisite: … has landed", "Afhænger af …") must carry the
# producing sibling in its ``parents``. The LLM can encode the dependency in
# prose yet still emit a flat, dependency-free sibling, which silently
# parallelises work whose premises do not exist yet (t_e45b2d7e). The marker is
# treated as authoritative: the graph is repaired so the declared edge is real.
_PREREQ_RE = re.compile(
    r"(?im)^\s*[>*\-\s]*(?:forudsætning(?:er)?|præmis(?:ser)?|"
    r"prerequisite(?:s)?|prereq(?:s)?|afhængighed(?:er)?|"
    r"dependency|dependencies|depends\s+on|afhænger\s+af)\b"
)
# Plural / "the parent tasks" phrasing — the child depends on EVERY preceding
# sibling (e.g. "Forældre-taskene (klassifikation + logning) er landet").
_PREREQ_PLURAL_RE = re.compile(
    r"(?i)\b(?:forældre|taskene|begge|både|alle|parents|parent\s+tasks|"
    r"siblings|both|all)\b|\+"
)
_TOKEN_RE = re.compile(r"[a-zæøå0-9_]{4,}")


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return _specify_author("decomposer")


def _resolve_profile_from_cfg(cfg: dict, key: str, *, fallback: Optional[str] = None) -> str:
    """``kanban.<key>`` if it names an existing profile, else ``fallback``
    (the root task's own assignee) if that does, else the active default
    profile — so a task is never stranded for lack of an owner.
    ``orchestrator_profile`` owns the root after fan-out; ``default_assignee``
    catches children the decomposer can't route.

    The root's assignee sits before the active profile because the decomposer
    runs inside whatever profile hosts the dispatcher — an operator's
    credential-less incognito profile, say — and that profile must never
    silently become the owner of work the card was assigned away from (#114294).
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get(key) or "").strip()
    for candidate in (explicit, (fallback or "").strip()):
        if candidate:
            try:
                if profiles_mod.profile_exists(candidate):
                    return candidate
            except Exception:
                pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """``(roster_for_prompt, valid_assignee_names)``; entries are
    ``{name, description, has_description}``."""
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return [], set()
    roster = []
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
    return roster, {p.name for p in all_profiles}


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    return "\n".join(
        f"  - {entry['name']}{'' if entry['has_description'] else ' ⚠ undescribed'}: {entry['description']}"
        for entry in roster
    )


def _normalize_assignee_choice(assignee: object, *, default_assignee: str, valid_names: set[str]) -> str:
    """A valid assignee, else ``default_assignee`` — promoted work is never
    left unassigned."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    return chosen if chosen in valid_names else default_assignee


@dataclass
class _Routing:
    """Config-derived routing context for one decomposition."""

    orchestrator: str
    default_assignee: str
    auto_promote: bool
    roster: list[dict]
    valid_names: set[str]


def _load_routing(*, root_assignee: Optional[str] = None) -> _Routing:
    from hermes_cli.config import load_config_readonly
    try:
        cfg = load_config_readonly()
    except Exception:  # decompose_task promises ok=False, never a raise, on config trouble
        cfg = {}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    roster, valid_names = _build_roster()
    return _Routing(
        orchestrator=_resolve_profile_from_cfg(cfg, "orchestrator_profile", fallback=root_assignee),
        default_assignee=_resolve_profile_from_cfg(cfg, "default_assignee", fallback=root_assignee),
        auto_promote=bool(kanban_cfg.get("auto_promote_children", True)),
        roster=roster,
        valid_names=valid_names,
    )


def _apply_single(task: kb.Task, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    """``fanout=false``: single-task spec promotion (same effect as specify)."""
    title_val, body_val = _title_body(parsed)
    assignee_val = None
    if not task.assignee:
        assignee_val = _normalize_assignee_choice(
            parsed.get("assignee"), default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
    if title_val is None and body_val is None:
        return DecomposeOutcome(task.id, False, "decomposer returned fanout=false with no title/body")
    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn, task.id, title=title_val, body=body_val, assignee=assignee_val, author=author,
        )
    if not ok:
        return DecomposeOutcome(task.id, False, "task moved out of triage before promotion")
    return DecomposeOutcome(task.id, True, "single task (no fanout)", fanout=False, new_title=title_val)


def _clean_children(task_id: str, raw_tasks: list, routing: _Routing) -> tuple[list[dict], str]:
    """Validate/normalise the LLM's ``tasks`` list; ``(children, "")`` or ``([], reason)``.
    Unknown assignees route to the default; never assignee=None."""
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return [], f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee, default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
        if isinstance(assignee, str) and assignee.strip() and assignee.strip() not in routing.valid_names:
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, routing.default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        children.append({
            "title": title.strip()[:200],
            "body": body.strip() if isinstance(body, str) else "",
            "assignee": chosen,
            # Drop non-int, out-of-range and self parent indices.
            "parents": [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx],
        })
    return children, ""


def _declares_prerequisite(body: Optional[str]) -> bool:
    """True when the child body carries a prerequisite marker line."""
    return bool(body) and bool(_PREREQ_RE.search(body))


def _significant_tokens(text: str) -> set[str]:
    """Lowercased tokens of >= 4 chars, used to match a prereq to its producer."""
    return set(_TOKEN_RE.findall((text or "").lower()))


def _lexical_prereq_parents(idx: int, children: list[dict]) -> list[int]:
    """Best preceding-sibling match(es) for a singular prereq reference.

    Scores each earlier sibling by shared significant tokens between this
    child's body and the sibling's title+body; returns the highest-scoring
    sibling(s) (indices), or ``[]`` when nothing shares a token. Restricted to
    preceding siblings so a repaired edge always points strictly leftwards and
    can never introduce a cycle.
    """
    tokens = _significant_tokens(children[idx].get("body") or "")
    if not tokens:
        return []
    best: list[int] = []
    best_score = 0
    for j in range(idx):
        other = children[j]
        cand = _significant_tokens(f"{other.get('title') or ''} {other.get('body') or ''}")
        score = len(tokens & cand)
        if score > best_score:
            best_score, best = score, [j]
        elif score == best_score and score > 0:
            best.append(j)
    return best


def _enforce_declared_prereq_edges(children: list[dict]) -> list[dict]:
    """Turn body-declared prerequisites into real ``parents`` edges (t_e45b2d7e).

    A declared prerequisite is authoritative: a child that carries the marker
    but has no parent edge gets one added, so the dispatcher can never start a
    dependent sibling (a test/verify card) before the code/artifact it
    presupposes has landed. Plural / "the parent tasks" phrasing depends on
    every preceding sibling; a singular reference is resolved by lexical match
    against the siblings' titles/bodies, falling back to the nearest preceding
    sibling. A child with no preceding sibling to depend on is left untouched
    (the premise lies outside this graph) and logged.
    """
    total = len(children)
    for idx, child in enumerate(children):
        body = child.get("body")
        if not _declares_prerequisite(body):
            continue
        if child.get("parents"):
            continue  # the LLM already encoded an edge — trust it
        targets: list[int] = []
        if _PREREQ_PLURAL_RE.search(body or ""):
            targets = list(range(idx))
        if not targets:
            targets = _lexical_prereq_parents(idx, children)
        if not targets and idx > 0:
            targets = [idx - 1]
        if targets:
            child["parents"] = sorted(set(targets))
        else:
            logger.warning(
                "decompose: child %d declares a prerequisite but has no "
                "preceding sibling to depend on (siblings=%d); leaving as-is",
                idx, total,
            )
    return children


def _apply_fanout(task_id: str, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(task_id, False, "decomposer returned fanout=true with empty tasks list")
    children, reason = _clean_children(task_id, raw_tasks, routing)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    children = _enforce_declared_prereq_edges(children)
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn,
                task_id,
                root_assignee=routing.orchestrator,
                children=children,
                author=author,
                auto_promote=routing.auto_promote,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return DecomposeOutcome(task_id, False, "task already decomposed or moved out of triage")
    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children", fanout=True, child_ids=child_ids,
    )


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, no aux client, API error, malformed/empty reply) surface
    as ``ok=False``."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, reason)

    routing = _load_routing(root_assignee=task.assignee)
    raw, reason = _call_aux(
        "decompose", task_id, aux_task="kanban_decomposer", system=_system_prompt(),
        user=_USER_TEMPLATE.format(
            **_task_prompt_fields(task),
            roster=_format_roster(routing.roster),
            default_assignee=routing.default_assignee,
        ),
        max_tokens=4000, timeout=timeout or 180, log=logger,
    )
    if raw is None:
        return DecomposeOutcome(task_id, False, reason)

    parsed = _extract_json_blob(raw, _FENCE_RE)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    audit_author = author or _profile_author()
    if not parsed.get("fanout"):
        return _apply_single(task, parsed, routing, audit_author)
    return _apply_fanout(task_id, parsed, routing, audit_author)


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column."""
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
    return [row.id for row in rows]
