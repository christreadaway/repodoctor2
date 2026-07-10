"""
Codebase Tracker — schema, validation, and visual metadata.

Implements the data contract from CODEBASE_TRACKER_PRD.md §6 (Data
requirements) and the integrity invariants from §5.5. The renderer
(templates/tracker.html) reads validated tracker dicts produced here.

A tracker dict has this top-level shape:

    {
      "owner": "christreadaway",
      "repo": "audioscribe",
      "generated_at": "2026-05-19T16:00:00Z",
      "branch_at_verification": "main",
      "ai_model": "claude-haiku-4-5-20251001",
      "modules":          [Module, ...],
      "infra_gaps":       [InfraGap, ...],
      "features":         [FeatureIdea, ...],
      "external_systems": [ExternalSystem, ...],
      "questions":        [Question, ...],
      "next_actions":     [NextAction, ...],
      "recent_changes":   [RecentChange, ...],
      "build_sequence":   [str, ...],
      "rollout_sequence": [str, ...]
    }

IDs are stable across regenerations (PRD §5.1): once M7 exists it stays
M7 forever. The generator passes the prior tracker into the model so
existing IDs are preserved; new rows get the next unused integer per
prefix.
"""

from __future__ import annotations

import re
from typing import Iterable

# ----------------------------------------------------------------------
# Enum values (PRD §6 Enums)
# ----------------------------------------------------------------------

MODULE_STATUSES = ("functional", "prototype", "visual", "missing")
PRIORITIES = ("P0", "P1", "P2", "P3", "—")
FEATURE_STATUSES = (
    "Proposed", "In Discussion", "In Progress", "Shipped", "Abandoned"
)
EFFORTS = ("XS", "S", "M", "L", "XL")
NEXT_ACTION_STATUSES = (
    "todo", "in_progress", "awaiting_deploy", "shipped",
    "blocked", "dismissed",
)
CHANGE_KINDS = ("shipped", "unblocked", "doc", "fix", "blocked")
EXTERNAL_MODES = ("Core", "Integrate", "Replace", "Optional")

# Per-prefix ID regex (PRD §5.1)
ID_PATTERNS = {
    "M": re.compile(r"^M\d+$"),
    "I": re.compile(r"^I\d+$"),
    "F": re.compile(r"^F\d+$"),
    "E": re.compile(r"^E\d+$"),
    "Q": re.compile(r"^Q\d+$"),
    "N": re.compile(r"^N\d+$"),
}

ANY_ID_PATTERN = re.compile(r"^[MIFEQN]\d+$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------
# Visual metadata — keyed lookups consumed by the Jinja template.
# Each entry: { label, chip (css class), description }.
# ----------------------------------------------------------------------

STATUS_META = {
    "functional":  {"label": "Functional", "chip": "chip-functional"},
    "prototype":   {"label": "Prototype",  "chip": "chip-prototype"},
    "visual":      {"label": "Visual",     "chip": "chip-visual"},
    "missing":     {"label": "Missing",    "chip": "chip-missing"},
}

PRIORITY_META = {
    "P0": {"label": "P0", "chip": "chip-p0"},
    "P1": {"label": "P1", "chip": "chip-p1"},
    "P2": {"label": "P2", "chip": "chip-p2"},
    "P3": {"label": "P3", "chip": "chip-p3"},
    "—":  {"label": "—",  "chip": "chip-none"},
}

FEATURE_STATUS_META = {
    "Shipped":       {"label": "Shipped",       "chip": "chip-functional"},
    "In Progress":   {"label": "In Progress",   "chip": "chip-prototype"},
    "In Discussion": {"label": "In Discussion", "chip": "chip-visual"},
    "Proposed":      {"label": "Proposed",      "chip": "chip-p2"},
    "Abandoned":     {"label": "Abandoned",     "chip": "chip-missing"},
}

EFFORT_META = {
    "XS": {"label": "XS", "hint": "< 1 hour"},
    "S":  {"label": "S",  "hint": "≤ ½ day"},
    "M":  {"label": "M",  "hint": "1–2 days"},
    "L":  {"label": "L",  "hint": "~1 week"},
    "XL": {"label": "XL", "hint": "multi-week"},
}

NEXT_ACTION_STATUS_META = {
    "todo":            {"label": "Todo",            "chip": "chip-p2"},
    "in_progress":     {"label": "In progress",     "chip": "chip-prototype"},
    "awaiting_deploy": {"label": "Awaiting deploy", "chip": "chip-visual"},
    "shipped":         {"label": "Shipped",         "chip": "chip-functional"},
    "blocked":         {"label": "Blocked",         "chip": "chip-missing"},
    "dismissed":       {"label": "Dismissed",       "chip": "chip-p3"},
}

CHANGE_KIND_META = {
    "shipped":   {"label": "shipped",   "chip": "chip-functional"},
    "unblocked": {"label": "unblocked", "chip": "chip-prototype"},
    "doc":       {"label": "doc",       "chip": "chip-visual"},
    "fix":       {"label": "fix",       "chip": "chip-p1"},
    "blocked":   {"label": "blocked",   "chip": "chip-missing"},
}

EXTERNAL_MODE_META = {
    "Core":      {"label": "Core",      "chip": "chip-functional"},
    "Integrate": {"label": "Integrate", "chip": "chip-visual"},
    "Replace":   {"label": "Replace",   "chip": "chip-p1"},
    "Optional":  {"label": "Optional",  "chip": "chip-p3"},
}


# ----------------------------------------------------------------------
# Empty-tracker scaffold
# ----------------------------------------------------------------------

EMPTY_TRACKER = {
    "owner": "",
    "repo": "",
    "generated_at": None,
    "branch_at_verification": "",
    "ai_model": "",
    "modules": [],
    "infra_gaps": [],
    "features": [],
    "external_systems": [],
    "questions": [],
    "next_actions": [],
    "recent_changes": [],
    "build_sequence": [],
    "rollout_sequence": [],
}


def empty_tracker(owner: str = "", repo: str = "") -> dict:
    """Return a fresh empty tracker shell."""
    t = {k: ([] if isinstance(v, list) else v) for k, v in EMPTY_TRACKER.items()}
    t["owner"] = owner
    t["repo"] = repo
    return t


# ----------------------------------------------------------------------
# Validation (PRD §5.5 — invariants enforced by tests AND save-time guard)
# ----------------------------------------------------------------------

class TrackerValidationError(Exception):
    """Raised on save when a tracker fails any §5.5 invariant."""


def _check(errors: list[str], ok: bool, msg: str) -> None:
    if not ok:
        errors.append(msg)


def _ids_of(rows: Iterable[dict]) -> set[str]:
    return {r.get("id", "") for r in rows if isinstance(r, dict)}


def validate_tracker(tracker: dict) -> list[str]:
    """Return a list of integrity-error strings. Empty list = valid."""
    errors: list[str] = []
    if not isinstance(tracker, dict):
        return ["tracker root must be an object"]

    def _dict_rows(key: str) -> list[dict]:
        """Rows for a section, flagging non-dict entries instead of letting
        row.get() raise AttributeError — a crash here would bypass the
        generator's validation-retry loop entirely."""
        rows = tracker.get(key) or []
        if not isinstance(rows, list):
            errors.append(f"{key}: must be a list, got {type(rows).__name__}")
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                errors.append(f"{key}: row is not an object: {row!r:.80}")
                continue
            out.append(row)
        return out

    modules = _dict_rows("modules")
    infra_gaps = _dict_rows("infra_gaps")
    features = _dict_rows("features")
    external_systems = _dict_rows("external_systems")
    questions = _dict_rows("questions")
    next_actions = _dict_rows("next_actions")
    recent_changes = _dict_rows("recent_changes")

    # Per-row ID format + uniqueness
    for prefix, rows, label in (
        ("M", modules, "modules"),
        ("I", infra_gaps, "infra_gaps"),
        ("F", features, "features"),
        ("E", external_systems, "external_systems"),
        ("Q", questions, "questions"),
        ("N", next_actions, "next_actions"),
    ):
        seen: set[str] = set()
        for row in rows:
            rid = row.get("id", "")
            _check(errors, bool(ID_PATTERNS[prefix].match(rid or "")),
                   f"{label}: bad ID '{rid}' (must match ^{prefix}\\d+$)")
            _check(errors, rid not in seen, f"{label}: duplicate ID '{rid}'")
            seen.add(rid)

    # Module-specific enums
    for m in modules:
        _check(errors, m.get("status") in MODULE_STATUSES,
               f"module {m.get('id')}: status '{m.get('status')}' not in {MODULE_STATUSES}")
        _check(errors, m.get("priority") in PRIORITIES,
               f"module {m.get('id')}: priority '{m.get('priority')}' not in {PRIORITIES}")
        _check(errors, isinstance(m.get("routes"), list),
               f"module {m.get('id')}: routes must be a list")

    module_ids = _ids_of(modules)
    action_ids = _ids_of(next_actions)
    # related_ids / depends_on may point at ANY row type in this tracker.
    # The PRD §5.5 narrows related_ids to M/F/I, but in practice the model
    # also references Q (questions an action answers) and E (external
    # systems an action touches) — both semantically reasonable. We accept
    # any in-tracker ID rather than reject useful cross-references.
    valid_any_id = (
        module_ids | _ids_of(infra_gaps) | _ids_of(features)
        | _ids_of(external_systems) | _ids_of(questions) | action_ids
    )

    # Infra gaps: blocks point at real modules
    for g in infra_gaps:
        _check(errors, g.get("priority") in PRIORITIES,
               f"infra_gap {g.get('id')}: priority '{g.get('priority')}' not in {PRIORITIES}")
        for blocked in g.get("blocks", []) or []:
            _check(errors, blocked in module_ids,
                   f"infra_gap {g.get('id')}: blocks '{blocked}' is not a real module ID")

    # Features: modules[] point at real modules; both priorities valid
    for f in features:
        _check(errors, f.get("build_priority") in PRIORITIES,
               f"feature {f.get('id')}: build_priority '{f.get('build_priority')}' not in {PRIORITIES}")
        _check(errors, f.get("roll_priority") in PRIORITIES,
               f"feature {f.get('id')}: roll_priority '{f.get('roll_priority')}' not in {PRIORITIES}")
        _check(errors, f.get("status") in FEATURE_STATUSES,
               f"feature {f.get('id')}: status '{f.get('status')}' not in {FEATURE_STATUSES}")
        for mid in f.get("modules", []) or []:
            _check(errors, mid in module_ids,
                   f"feature {f.get('id')}: modules '{mid}' is not a real module ID")

    # External systems: mode in allowed list
    for es in external_systems:
        _check(errors, es.get("mode") in EXTERNAL_MODES,
               f"external_system {es.get('id')}: mode '{es.get('mode')}' not in {EXTERNAL_MODES}")

    # Next actions: enums + relatedIds + dependsOn (no cycles, no self)
    for n in next_actions:
        _check(errors, n.get("effort") in EFFORTS,
               f"next_action {n.get('id')}: effort '{n.get('effort')}' not in {EFFORTS}")
        _check(errors, n.get("priority") in PRIORITIES,
               f"next_action {n.get('id')}: priority '{n.get('priority')}' not in {PRIORITIES}")
        status = n.get("status") or "todo"
        _check(errors, status in NEXT_ACTION_STATUSES,
               f"next_action {n.get('id')}: status '{status}' not in {NEXT_ACTION_STATUSES}")
        prompt = n.get("prompt") or ""
        _check(errors, isinstance(prompt, str) and len(prompt.strip()) >= 50,
               f"next_action {n.get('id')}: prompt must be ≥50 chars (got {len(prompt.strip())})")
        for rid in n.get("related_ids", []) or []:
            _check(errors, rid in valid_any_id,
                   f"next_action {n.get('id')}: related_ids '{rid}' is not a real ID in this tracker")
        for dep in n.get("depends_on", []) or []:
            _check(errors, dep != n.get("id"),
                   f"next_action {n.get('id')}: depends_on cannot self-reference")
            # depends_on may reference any row type — e.g. an N can depend on
            # an I being resolved or an M being built. Cycle detection below
            # still only walks N→N edges since other rows can't form cycles.
            _check(errors, dep in valid_any_id,
                   f"next_action {n.get('id')}: depends_on '{dep}' is not a real ID in this tracker")

    _check(errors, _no_cycles(next_actions),
           "next_actions: depends_on contains a cycle")

    # Recent changes: date format + kind + relatedIds.
    # Note: we DO NOT enforce strict newest-first order in validation —
    # the model occasionally interleaves dates. Sort order is fixed by
    # sort_recent_changes() before the tracker is saved.
    for c in recent_changes:
        date = c.get("date", "")
        _check(errors, bool(DATE_PATTERN.match(date or "")),
               f"recent_change '{c.get('title', '')}': bad date '{date}' (must be YYYY-MM-DD)")
        _check(errors, c.get("kind") in CHANGE_KINDS,
               f"recent_change '{c.get('title', '')}': kind '{c.get('kind')}' not in {CHANGE_KINDS}")
        for rid in c.get("related_ids", []) or []:
            _check(errors, rid in valid_any_id,
                   f"recent_change '{c.get('title', '')}': related_ids '{rid}' is not a real ID in this tracker")

    return errors


def sort_recent_changes(tracker: dict) -> None:
    """Sort tracker['recent_changes'] newest-first in place.
    Called by the generator before saving so the model's ordering doesn't
    matter — we just normalise it. Entries with no date sink to the end."""
    rows = [c for c in (tracker.get("recent_changes") or []) if isinstance(c, dict)]
    rows.sort(key=lambda c: c.get("date") or "0000-00-00", reverse=True)
    tracker["recent_changes"] = rows


def _no_cycles(next_actions: list[dict]) -> bool:
    """Return False iff depends_on forms a cycle."""
    graph: dict[str, list[str]] = {}
    for n in next_actions:
        nid = n.get("id")
        if not nid:
            continue
        graph[nid] = list(n.get("depends_on") or [])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in graph}

    def visit(nid: str) -> bool:
        if color.get(nid) == GRAY:
            return False
        if color.get(nid) == BLACK:
            return True
        color[nid] = GRAY
        for dep in graph.get(nid, []):
            if dep in graph and not visit(dep):
                return False
        color[nid] = BLACK
        return True

    return all(visit(nid) for nid in graph)


# ----------------------------------------------------------------------
# ID minting (preserve existing IDs across regeneration — PRD §5.1)
# ----------------------------------------------------------------------

def next_id(prefix: str, existing_ids: Iterable[str]) -> str:
    """Return the next unused integer for `prefix`, scanning all existing
    rows. NEVER reuses a deleted number — picks max+1 even if gaps exist
    in the sequence."""
    pattern = ID_PATTERNS.get(prefix)
    if not pattern:
        raise ValueError(f"Unknown prefix: {prefix}")
    max_n = 0
    for eid in existing_ids:
        if not eid or not pattern.match(eid):
            continue
        try:
            n = int(eid[1:])
        except ValueError:
            continue
        if n > max_n:
            max_n = n
    return f"{prefix}{max_n + 1}"


def collect_existing_ids(tracker: dict) -> dict[str, list[str]]:
    """Return {prefix: [ids]} from a tracker. Used by the generator to
    instruct the AI which IDs must be preserved."""
    return {
        "M": [m.get("id") for m in tracker.get("modules", []) if m.get("id")],
        "I": [g.get("id") for g in tracker.get("infra_gaps", []) if g.get("id")],
        "F": [f.get("id") for f in tracker.get("features", []) if f.get("id")],
        "E": [e.get("id") for e in tracker.get("external_systems", []) if e.get("id")],
        "Q": [q.get("id") for q in tracker.get("questions", []) if q.get("id")],
        "N": [n.get("id") for n in tracker.get("next_actions", []) if n.get("id")],
    }
