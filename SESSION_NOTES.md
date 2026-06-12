# REPODOCTOR2 - Session History & Project Status

**Repository:** `repodoctor2`
**Total Sessions Logged:** 15
**Date Range:** 2025-02-14 to 2026-06-12
**Last Updated:** 2026-06-12 (Session 15)

This file contains a complete history of Claude Code sessions for this repository and current project status. Sessions are listed in reverse chronological order (most recent first).

---

## Session 15 — 2026-06-12 (Chat Briefing — portfolio snapshot for Claude Chat)

### What We Built

A new **Briefing** screen at `/briefing` (top-nav link between What's Next and Tracker) that summarizes every project comprehensively and composes ONE Markdown document to paste into a Claude chat session — answering "where am I across all my projects?" in a single read. Modeled on parentpoint's CHAT_BRIEFING.md format.

Each project gets an AI-generated **chat brief** with: what it is (the business problem, not the tech), stack, stage (Idea / Requirements / Building / Testing / Live / Paused, with one sentence of evidence), where we are (current-state narrative), what's built (grouped by audience when docs allow), what's left (sequenced), open decisions (owner calls), and constraints a chat session must respect (privacy/security/operational rules). The screen merges each brief with hard facts the app already has: last push, branch count, docs X/5 (+ which are missing), code size, languages, groups, and the repo's tracker (open next actions P0-first with status notes, open questions).

- **COPY FOR CLAUDE CHAT** — one button copies the whole portfolio document (purpose preamble + At-a-Glance table + one section per project, newest-push-first)
- **DOWNLOAD .MD** — same document as a file at `/briefing/export.md`, for attaching instead of pasting
- **Per-card COPY SECTION** — just that project's section
- **Smart generation** — UPDATE STALE + MISSING only generates briefs for repos with no brief or pushed-to since their brief was generated; REGENERATE ALL (with confirm) forces everything in view. Group tabs filter both the screen and what gets generated/exported.
- Repos without a brief still render and export using the Projects-page summary or GitHub description as fallback, marked "No AI brief yet"
- STALE badge on cards when the repo was pushed after its brief was generated; same note in the export footer
- Full-screen generation overlay (reuses the Tracker overlay pattern) with elapsed timer

### Technical Details

- **New module `briefing.py`:** input gathering (docs fetched recursively via the GitHub API + README fallback + compact tracker facts), Haiku generation with strict-JSON system prompt, `normalize_brief()` (stage enum enforcement, bullet caps: 10 built / 10 left / 5 decisions / 4 constraints), staleness check, project assembly, and Markdown composition
- **Briefs cached** in `data/briefs.json` keyed by repo name with `_generated_at` (mirrors `project_summaries.json`)
- **Event log** at `data/logs/briefing.log` — one JSON object per line (`generate_start` / `generate_done` / `generate_error` with token counts), same grep-able format as `tracker.log`; the log writer in `models.py` was generalized to `_append_log_event()` / `_tail_log()` serving both logs
- **Routes:** `GET /briefing`, `POST /briefing/generate` (`force=1` to regenerate all), `GET /briefing/export.md` (download with date- and group-stamped filename)
- **Refactor:** the group-filter resolution duplicated across Projects and What's Next was extracted into `_resolve_active_group()` and reused by Briefing (generation/export read the group without persisting it)
- **Costs:** ~$0.005–$0.01 per project on Haiku (2,500-token output budget); token usage feeds the session cost footer
- **Tests:** 32 new in `tests/test_briefing.py` (normalization, staleness, tracker-action extraction, assembly, Markdown content, mocked input gathering, storage round-trip, route auth) — 176 total pass
- `.gitignore` now covers `data/briefs.json`, `data/trackers/`, `data/logs/`

---

## Session 14 — 2026-05-19 (Codebase Tracker + Firestore relocated to Settings)

### What We Built

A per-repo Codebase Tracker page at `/tracker`, accessible from a new top-nav link. Pick any of your scanned repos from a dropdown, click Generate, and Claude reads the repo's docs + file tree + last 30 days of commits and produces a structured tracker with eight tabs:

- **Overview** — what's open right now (P0 next actions) + what changed recently + recommended build & rollout sequences
- **Recent** — newest-first changelog with kind chips (shipped / unblocked / doc / fix / blocked)
- **Next Actions** — copy-paste-ready Claude Code prompts, filterable by priority + status (hides shipped by default)
- **Modules** — grouped by category, status chip (functional / prototype / visual / missing), with route paths
- **Infra Gaps** — pieces of infrastructure that block ≥2 modules
- **Features** — proposed/shipped feature specs with separate build vs. rollout priorities
- **External Systems** — third-party deps with mode (Core / Integrate / Replace / Optional)
- **Questions** — open questions the code can't answer

Every row carries a stable monotonic ID (`M*`, `I*`, `F*`, `E*`, `Q*`, `N*`) preserved across regenerations. The generator passes the prior tracker into the model with explicit "LOAD-BEARING IDS" instructions; new rows get max+1 per prefix.

### Codes — what each prefix means

- **M\*** — Module. A real surface in the app (a route, a domain, a page).
- **I\*** — Infra gap. Missing infrastructure that blocks two or more modules.
- **F\*** — Feature spec. Something proposed or recently shipped, with a doc.
- **E\*** — External system. Third-party services (Anthropic, Firebase, Stripe).
- **Q\*** — Open question. A decision the code can't make on its own.
- **N\*** — Next action. One Claude Code session's worth of work, with copy-paste prompt.

### Technical Details

- **8 files added / changed:**
  - New: `tracker_data.py` (schema constants + §5.5 integrity validation + monotonic ID minting)
  - New: `tracker_generator.py` (Anthropic prompt construction, retry loop, prior-tracker context for ID preservation)
  - New: `templates/tracker.html` (single 8-tab template handling index / view / debug modes)
  - New: `static/js/tracker.js` (tab switching, filters, copy-prompt with server-side event logging)
  - New: `tests/test_tracker.py` (39 tests covering every PRD §5.5 invariant + storage round-trip + path safety + template render edge cases)
  - Modified: `models.py` (per-repo tracker storage at `data/trackers/<owner>__<repo>.json` + structured event log at `data/logs/tracker.log`)
  - Modified: `app.py` (5 new routes: `/tracker`, `/tracker/<owner>/<name>`, `/tracker/<owner>/<name>/generate`, `/tracker/<owner>/<name>/debug`, `/api/tracker/<owner>/<name>/copy-event`)
  - Modified: `templates/base.html` (nav link), `templates/settings.html` (Tools card), `static/css/style.css` (~500 lines of tracker styles)

- **AI model:** uses configured `ai_model` preference (Haiku 4.5 default, $0.02-$0.05 per generation depending on repo size). Surfaced in the tracker toolbar before clicking Generate.

- **Validation runs at save time AND in unit tests.** Invalid AI output triggers one retry with the validation errors fed back to the model; if it still fails, generation fails cleanly with a flash message instead of saving a corrupt tracker.

- **Firestore detection auto-runs during generation.** When `firestore_detector` finds Firebase signals in a repo, the detection (status, project ID, indicators, missing config) gets fed to the prompt with explicit instructions to emit an E-row for Firestore + I-rows for any missing config + N-rows with copy-paste fix prompts. No more manual Firestore page needed for per-project setup — it's surfaced automatically in each project's tracker.

- **Firestore page moved from main nav to Settings → Tools.** The page still works at `/firestore` for the fleet view, just no longer crowding the nav.

- **Logging infrastructure** per CLAUDE.md mandate: `data/logs/tracker.log` captures generation start/done/error, validation pass/fail, copy-prompt events. The debug surface at `/tracker/<owner>/<name>/debug` shows a live integrity check + tail-100 of the log + a "Copy for Claude Code" button that formats the buffer as a markdown block for paste-back debugging.

### Bugs Found and Fixed (Mid-Session Audit)

After initial implementation, a comprehensive test pass surfaced three bugs:

1. **(Critical)** Template rendered status counts with a direct dict subscript — any module status outside the four known values (e.g. typoed AI response) crashed the page with UndefinedError → 500. Fixed by guarded `.get()` lookup; unknown statuses simply don't contribute to the headline counts and the validation banner still flags them.
2. **(UX)** `/tracker` landing page didn't auto-route to the most recently generated tracker. Now redirects when at least one tracker is saved.
3. **(UX)** Repo dropdown only listed scanned repos. After a fresh Flask restart with no in-memory scan, you couldn't reach a saved tracker via the dropdown. Now merges saved trackers in.

Test count grew from 28 to 39 covering the fixes + storage round-trip + path-traversal safety + Firestore prompt inclusion / omission.

### Dashboard Cellpadding Fix (Pre-Tracker)

Also fixed a long-standing dashboard spacing bug surfaced this session: the summary-stats div (`40 Repositories / 48 Total Branches / 29 Missing Required Files`) and the file-legend chip row beneath it had no CSS — they rendered as raw blocks with no gap, colliding visually. Added a proper flex card with explicit margins.

### Current Status

- ✅ Tracker generates from docs + code + commits + (optional) prior tracker
- ✅ 8 tabs render with color-coded chips (red/orange/amber/sky/slate departing from all-green palette per user feedback)
- ✅ ID stability across regenerations
- ✅ Firestore auto-detection plumbed into tracker generation
- ✅ Debug surface with copy-for-Claude-Code formatter
- ✅ 39/39 tracker tests passing (5 skipped — anthropic SDK not in remote env, runs locally)
- ✅ Dashboard cellpadding bug fixed
- ⚠️ Netlify version still behind Flask (per Session 13 backlog)

### Branch Info

- Branch: `claude/add-project-tracker-L4awW`
- Status: pushed, second merge to `main` needed after Session 14b production hardening

---

## Session 14d — 2026-05-19 (Tracker UX: clickable stats, block/dismiss, Shipped tab, row prompts)

First real-use feedback after the tracker rendered cleanly for parentpoint. Chris flagged four enhancements that turned the tracker from a read-only view into something he could actually drive work from.

### 1. Clickable headline stats

Every chip in the page header (Functional / Prototype / Visual / Missing / P0 open / Infra gaps) is now a button. Click it and the tracker jumps to the relevant tab AND pre-applies the matching filter — so "9 P0 open" lands on Next Actions filtered to P0 + Open. No more "I see there are 9 P0s but where are they."

### 2. Block / dismiss next actions

The Next Actions list was including items Chris was actually blocked on (waiting for a decision, or where the AI misinterpreted something). Now each card carries a status form: dropdown (Todo / In progress / Awaiting deploy / Shipped / Blocked / Dismissed) + an optional note input + UPDATE button. The new statuses get distinct card treatments — blocked cards get a red left-rule + tinted background, dismissed cards ghost-fade. New filter chips for "Blocked only" and "Dismissed". The default Open filter now excludes both shipped and dismissed.

The AI is told to preserve user-set statuses (blocked / dismissed / in_progress / awaiting_deploy) AND the existing status_note on regeneration. So when Chris marks N12 as "blocked — waiting on Chris to decide custody model," the next regenerate keeps that exact state. Otherwise the AI is free to flip todo→in_progress→shipped based on session notes evidence.

### 3. Shipped tab

Shipped next-actions move out of the Next Actions panel into a dedicated Shipped tab with its own count badge. The Next Actions count badge reflects open work only, which is what Chris actually cares about at a glance.

### 4. Per-row Claude Code prompts on every actionable tab

Modules / Infra Gaps / Features / External Systems each had no actionable next step. Now every row carries the same COPY PROMPT / SHOW PROMPT button pair Next Actions already had. The prompt is generated server-side by Jinja macros (`module_prompt`, `infra_prompt`, `feature_prompt`, `external_prompt`) — context-aware, references the row's ID + current state + the rows it touches, lists numbered steps, closes with an acceptance criterion. Same clipboard + server-side copy-event logging path Next Actions uses.

### Technical details

- `NEXT_ACTION_STATUSES` extended to 6 values (added `blocked`, `dismissed`).
- New chip classes `chip-tone-blocked` / `chip-tone-dismissed` + per-card body treatments (`.tracker-next-card.status-blocked`, `.status-dismissed`).
- New Flask route `POST /tracker/<owner>/<name>/action/<action_id>/status` updates one action's status + status_note, re-saves the tracker, logs `action_status_update` to the tracker log.
- System prompt extended with rule 2a: "preserve user-set statuses." `_compact_prior` now includes `status_note` in keep_fields so the AI sees the human's reason on the next generation.
- Filter logic scoped per-panel via `chip.closest('.tracker-panel')` so the new BLOCKED / DISMISSED filter chips on Next Actions don't fight the status filters on Modules.
- Next Actions tab uses a Jinja `render_next_card(n)` macro so the same renderer powers both the Next Actions tab and the Shipped tab without duplication.

### Tests

- 47 unit tests pass (up from 43). 6 of those skip in the remote test env because anthropic SDK isn't installed there.
- New tests: `blocked` and `dismissed` accepted by validator, unknown statuses still rejected, both new statuses registered in NEXT_ACTION_STATUS_META with correct labels.
- Render stress test exercised a tracker containing all 6 next-action statuses + verified every new feature renders (clickable stats, shipped tab, status form, blocked note display, all four row-prompt macros).

### Commits

- `cf58ee9` — clickable stats + block/dismiss + Shipped tab + row prompts

### Lessons

- A view-only tracker is half a product. The minute the user can act on rows, "make progress" stops being friction.
- Status enums on user-editable rows are user-facing. Naming them well (Todo / Blocked / Dismissed) > raw enum values (todo / blocked / dismissed) — used both via the META lookup.
- AI regeneration must respect user decisions. Without rule 2a in the system prompt, the next regenerate would clobber every status_note Chris had set.

---

## Session 14c — 2026-05-19 (Validation leniency + type guards)

The Session 14b streaming + 32K bump fixed truncation but exposed a different problem on parentpoint: the validator was too strict for what Claude actually emits. Three categories of failure surfaced, all of them my over-strict reads of the PRD, not real data problems.

### What Was Wrong

1. **`next_action.related_ids` rejected Q-IDs.** Claude was emitting `"related_ids": ["Q1"]` to mean "this action answers question Q1," which is semantically sensible. The PRD §5.5 says M/F/I-only, but in practice the model has a richer mental model. Validation kept failing on perfectly good output.

2. **`next_action.depends_on` rejected anything but N-IDs.** Same story — Claude emits `"depends_on": ["I3"]` to mean "can't ship N5 until I3 is fixed." Architecturally that's true; the validator was just narrow.

3. **`recent_changes` order rejected when the model interleaved dates.** When grouping commits into themes, Claude sometimes emits 2026-05-19, 2026-05-08, 2026-05-12 (i.e., not strictly newest-first). The validator treated this as an error. It's a cosmetic issue — fixable by sorting server-side.

### What We Fixed

1. **`related_ids` and `recent_changes.related_ids`** now accept any in-tracker ID type (M / I / F / E / Q / N). Still rejects IDs that don't exist anywhere in the tracker.
2. **`depends_on`** accepts any row type, not just N. Cycle detection still walks only N→N edges (other row types can't form action cycles).
3. **`recent_changes` order** is no longer a validation failure. New `sort_recent_changes()` helper sorts newest-first in place; the generator calls it after parsing and before validating, so save data is always clean.

### Bonus Robustness (audit-found)

A comprehensive audit pass also caught two robustness gaps unrelated to the validation leniency work:

4. **Type-guard AI output.** If Claude ever returned a non-list for a list-typed section (`modules: 42` or `modules: "string"`), the validator would crash on iteration. Now each section is type-checked; a non-list value falls back to the empty default and is logged at warn level. A non-dict top-level response (rare but possible) fails the attempt cleanly with a clear error.

5. **Type-guard `_compact_prior`.** The prior-tracker compaction (which feeds existing IDs to the next generation so they stay stable) was iterating whatever shape was in the loaded tracker file. A hand-edited tracker file with a malformed section would crash here. Now defensively skips non-list sections and non-dict rows.

### Tests

- 43 unit tests pass (up from 39 in Session 14, +1 from 14b). 6 of those skip in the remote test env because the anthropic SDK isn't installed there.
- New tests cover: Q-IDs in related_ids, mixed-type depends_on, cycle detection across mixed-type graphs, sort idempotency, sort with missing dates, type-guard on malformed sections, _compact_prior on malformed input.
- Comprehensive audit script (29 checks) exercises validator edge cases, lenient cases, cycle detection, sort helper, and a full storage roundtrip — all green.
- Template stress test renders a 20-module / 15-action / 18-change tracker (the prompt caps in 14b) cleanly at 91K chars.

### Commits

- `a43a2a2` — validation leniency (related_ids accepts Q/E, depends_on accepts any type, recent_changes auto-sort)
- `78bf2da` — type guards on AI output and `_compact_prior`

### Lessons

- A strict validator wastes API tokens on retries the model can't satisfy. Lenient where the data is semantically reasonable; strict where it's actually broken.
- "Cosmetic" issues (sort order, formatting) should be auto-fixed server-side, not bounced back at the user.
- AI outputs can be malformed in low-frequency but real ways. Type-guarding the parse step costs nothing and prevents 500s.

---

## Session 14b — 2026-05-19 (Production hardening after first real generation)

After merging Session 14 to main and trying the tracker against parentpoint, three real-world failures surfaced. None of them showed up in unit tests because they were all about the AI's behavior under load.

### What Broke

1. **Output truncation.** First generation against parentpoint came back with "Unbalanced braces in AI response" — the JSON cut off mid-content. Root cause: `max_tokens=8000` was way too tight for a repo with a dense PRODUCT_SPEC and many modules. The model wanted to emit ~12K tokens of output.
2. **Streaming-required error.** Bumping `max_tokens` to 32K triggered "Streaming is required for operations that may take longer than 10 minutes." The Anthropic SDK refuses non-streaming calls at high token counts because the HTTP connection could time out.
3. **Silent loading state.** Clicking GENERATE TRACKER did nothing visible for 20-90 seconds. The button had a `data-loading-text` attribute but no JS to read it. Users wondered if anything was happening.

### What We Fixed

1. **Bumped output budget 8K → 16K → 32K** in two steps (Haiku 4.5 supports up to 64K; 32K is safe headroom).
2. **Added hard scope caps to the system prompt** — max 25 modules, 8 infra_gaps, 12 features, 12 external_systems, 15 questions, 15 next_actions, 20 recent_changes. Plus a priority order for when the model can't fit everything: P0/P1 actions over P2/P3, non-functional modules over functional, infra blocking more modules over fewer. Prose fields capped at 1-3 sentences.
3. **Switched to the streaming API.** `client.messages.stream()` accumulates chunks via `stream.text_stream`, then pulls usage + stop_reason off `stream.get_final_message()`. Same cost as non-streaming — just a transport change.
4. **Per-generation model override dropdown** on the toolbar. Pick Haiku / Sonnet / Opus for one specific generation without touching global Settings. Whitelisted server-side so a bad form submission falls back to the default. Useful when parentpoint specifically needs Sonnet while everything else stays on cheaper Haiku.
5. **Fail-fast on truncation.** Detect `stop_reason == "max_tokens"` after the stream completes; raise immediately instead of retrying (the next call would truncate the same way). Error message includes token counts so the user sees how close they got, and points at the model dropdown.
6. **Loading overlay.** Centered card with an animated cyan spinner, "ANALYZING…" with pulsing dots, repo name, model in use, live elapsed-time counter, and a "Typical: 20-90 seconds. Don't close this tab." footer. Triggers on form submit, stays until the page reloads with the result.

### Commits

- `ea1560a` — truncation detection + slimmer input budgets + 16K cap
- `86ccd83` — hard scope caps in prompt + 32K cap
- `773bb53` — per-generation model override dropdown
- `8939c27` — streaming API
- (pending) — loading overlay + docs update

### Lessons

- AI-driven features need production debugging time, not just unit tests. Real repos surface output sizes that synthetic test data never will.
- A `data-loading-text` attribute without JS to read it is a lie. Either wire it up everywhere or remove it.
- The Anthropic SDK's streaming threshold isn't obvious — bumping max_tokens past ~16K silently changes the required API surface. Worth documenting.

---

## Session 13 — 2026-05-15 (Exclude henry branches from dashboard branch count)

### What We Built
Same-day follow-up to Session 12. Chris asked to stop counting branches whose name contains "henry" toward the dashboard's branch count. Henry branches still need to be discoverable by the Henry page, and still visible in the per-repo expandable branch list — they just shouldn't inflate the headline number.

1. **New `non_henry_branch_count` field on every scanned repo.** `scan_repo_lite` now also computes `henry_branch_count` (case-insensitive substring match, default branch never qualifies as henry even if its name happens to contain "henry") and `non_henry_branch_count = total - henry_count`.
2. **Dashboard switched to the new field.** The per-repo "Branches" column, the cross-repo "Total Branches" summary stat, and the table sort order all read `non_henry_branch_count`. `total_branch_count` and `branch_names` are untouched so the Henry page still finds henry branches by iterating `branch_names`.
3. **Expandable branch list now marks henry branches visibly.** Faded + italic styling (`.branch-henry`), trailing `(henry)` suffix, and a tooltip explaining "Excluded from branch count (henry branch)" so users aren't confused when they expand the row and count more branches than the column shows.

### Technical Details
- **Case-insensitive match** — `"henry" in bname.lower()` matches `Henry-A`, `HENRY-B`, `feat-henry-c`, etc. Tests cover all three casing variants.
- **Default-branch carve-out** — if the default branch is itself named `henry-main`, it stays counted. Same logic as the existing `_find_henry_branches` helper, so the dashboard count and Henry page agree on what "counts as henry" means.
- **Error-fallback symmetry** — the per-repo error dict in `/scan` (rendered when `scan_repo_lite` raises a non-auth exception) gained `henry_branch_count: 0` and `non_henry_branch_count: 0` so the template doesn't see undefined fields on error rows.
- **5 new tests** in `TestHenryBranchExclusion` cover: basic exclusion, case-insensitive match, default-branch-named-henry edge case, zero-henry no-op, and end-to-end `/scan` summary stat.
- **Template defensiveness** — `repo.non_henry_branch_count if repo.non_henry_branch_count is defined else repo.total_branch_count` so the dashboard still renders against pre-this-session cached scan data (Chris's in-memory `_scan_results` from before he restarts the app).

### Current Status
- ✅ Per-repo Branches column excludes henry branches
- ✅ Cross-repo Total Branches summary stat excludes henry branches
- ✅ Henry branches still appear in the expandable list, marked as `(henry)` and faded
- ✅ Henry page still works (uses `branch_names`, which is unchanged)
- ✅ 97/97 tests passing (92 from Session 12 + 5 new for henry exclusion)

### Branch Info
- Working branch: `claude/fix-flask-display-issue-1yxlq` (same branch as Session 12)
- Pushed: yes — single commit on top of Session 12's commits
- Not merged to `main` yet — Chris merges via local clone

### Decisions Made
- **New field rather than mutating `total_branch_count`** — Henry page and the expandable list both depend on the full count being known. Adding a parallel field keeps every caller's intent explicit.
- **Visible-but-faded styling for henry branches in the expanded list** rather than hiding them entirely. The user might still want to see them; hiding would just create a different confusion ("where did my branches go?").
- **Tooltip on the count cell** mentions how many henry branches are hidden. Cheap to add, prevents the "the expand shows more branches than the count" surprise.

### Next Steps
1. **Chris merges `claude/fix-flask-display-issue-1yxlq` into `main`** on his local PC.
2. **Pull main on the PC** and re-scan so the new field gets populated for his real repos.
3. **Confirm the count looks right** for repos that have henry branches. If the henry keyword should be expanded (e.g. to other contributor names), promote `HENRY_KEYWORD` to a preference.

### Questions / Blockers
None. The change is additive and non-breaking.

---

## Session 12 — 2026-05-15 (Surface GitHub 401s with remediation, fix silent-failure crashes)

### What We Built
Chris's dashboard came up blank after launching the app. Werkzeug logs showed `GET /user HTTP/1.1 401` and `GET /user/repos … 401` — GitHub had revoked or expired his saved PAT, but the app silently treated 401 as "no repos" and rendered an empty table with no indication of what went wrong. This session replaced the silent-failure paths with a centralized auth-error pipeline and fixed a handful of related edge-case crashes uncovered by a parallel bug audit.

1. **Auth-error pipeline (the main fix).** `github_client._get` now raises a new `GitHubAuthError` on 401 by default. `verify_token` opts out via `raise_on_auth_error=False` because it's a probe. Every other client method (`get_repos`, `get_branches`, `get_pulls`, `get_file_content`, `get_commits_since`, …) automatically surfaces 401s without per-method changes. A global `@app.errorhandler(gh.GitHubAuthError)` in `app.py` flashes a 4-step remediation message (generate new PAT → logout → reset credentials → re-enter) and redirects to the dashboard.
2. **Login verifies the saved PAT.** When Chris unlocks with an existing password, `/login` now calls `verify_token` BEFORE granting access. A 401 or missing `repo` scope keeps the user on the login page with a specific error instead of letting them through to an empty dashboard.
3. **New `/login/reset` endpoint + RESET CREDENTIALS button.** Accessible without authentication so a user locked out by a revoked PAT can recover from the login screen in one click. Deletes `config/credentials.enc` and falls through to first-time setup.
4. **Inner try/except blocks re-raise `GitHubAuthError`.** `/scan` and `/henry/generate` had `except Exception` blocks that would have converted the auth error into "this repo failed, continuing…" per-repo dummy rows. Now they re-raise, letting the global handler abort the whole operation with the right message.
5. **Henry commit-metadata defensive access.** A parallel Explore agent flagged that `lc["commit"]["committer"]["date"]` and `c["commit"]["message"].split("\n")[0]` access keys without `.get()` checks; rare-but-possible GitHub commits with null author/committer/message would KeyError. Switched to defensive `.get()` chains with sensible fallbacks ("Unknown", "(no message)").
6. **`models._load_json` corruption recovery.** If `preferences.json` / `groups.json` / etc. is corrupted (disk full mid-write, manual edit, etc.), the file is now renamed to `.corrupt` and an empty dict is returned. Previous behavior was to crash every request until the user manually deleted the file.

### Technical Details
- **`_get(raise_on_auth_error=True)`** — flag-based opt-out keeps the API minimal. The check is right after the rate-limit retry so the centralized log message and exception include the URL that failed.
- **Global error handler** — uses `session.get("authenticated")` to decide whether to redirect to `/` or `/login`. Locked-out users land on login with the remedy already flashed.
- **`GITHUB_AUTH_REMEDY` constant** — single source of truth for the remediation message, used by both the global handler and any explicit catches.
- **9 new tests in `TestGitHubAuthErrorHandling`** — exercise `get_repos` raising 401, `verify_token` returning None on 401, login rejecting bad PAT, login rejecting missing `repo` scope, `/login/reset` end-to-end, `/scan` redirecting with remedy, `/repo/<owner>/<name>` redirecting on 401, non-401 errors still returning [] gracefully.
- **Parallel bug audit** — spawned an Explore agent in parallel with test-writing. Returned 15 ranked bugs; we acted on the 3 that were both real and reachable from the main user flow (commit metadata KeyError, JSON corruption crash, plus the auth-error path already in progress). Skipped: CSRF on `/login/reset` (localhost-only app, would need Flask-WTF dependency); auto-escape "XSS" finding (false positive — Jinja2 escapes by default).

### Current Status
- ✅ Login rejects invalid/revoked saved PATs with a clear remediation message
- ✅ All GitHub client methods surface 401s through one centralized error handler
- ✅ RESET CREDENTIALS button on the login page works without authentication
- ✅ Henry generation no longer crashes on commits with null author/committer metadata
- ✅ Corrupted JSON files in `~/.repodoctor/` don't take the app down
- ✅ 92/92 tests passing (83 existing + 9 new for auth-error handling)
- ⚠️ Working branch is `claude/fix-flask-display-issue-1yxlq` (cloud-trigger forced) — Chris needs to merge to `main` on his machine

### Branch Info
- Working branch: `claude/fix-flask-display-issue-1yxlq`
- Pushed: yes (two commits — initial 401 fix + centralization/crash fixes)
- Not merged to `main` yet — Chris merges via his local clone

### Decisions Made
- **Centralized 401 handling in `_get`** rather than per-method 401 checks. Single point of behavior, opt-out only for the probe path.
- **Global Flask error handler** rather than per-route try/except. Routes added later automatically inherit the right behavior; the remediation message is consistent.
- **Reset endpoint accessible without auth** because the user is by definition locked out when they need it. Localhost-only deployment makes CSRF risk minimal; documented for revisit if the app ever leaves localhost.
- **Skipped the CSRF and XSS findings from the audit** — CSRF requires a new dependency (Flask-WTF) for a localhost tool; the XSS finding was a false positive because Jinja2 autoescapes by default.

### Next Steps
1. **Chris's immediate fix:** generate a new PAT on github.com/settings/tokens with `repo` scope, then either delete `config/credentials.enc` manually (this version) or click RESET CREDENTIALS on the login page (after merging).
2. Chris merges `claude/fix-flask-display-issue-1yxlq` into `main` on his local PC, then pulls (PowerShell one-liner provided in chat).
3. Future polish: the parallel audit also flagged `_save_json` having no disk-full/perm error handling and the Anthropic key being unverified at login. Both are real but low-frequency; revisit if they actually bite.

### Questions / Blockers
None — the auth-error path is the headline fix and is fully covered by tests. Chris just needs to rotate his PAT.

---

## Session 11 — 2026-05-11 (Beacon visibility, AI JSON parsing, Henry rendering, Unassigned projects)

### What We Built
Chris reported three issues with the live dashboard and asked for one new feature on the Manage Groups panel; all four were resolved this session.

1. **`BUSINESS_SPEC.md` now satisfies the `PRODUCT_SPEC.md` slot.** The `check_required_files` matcher in `github_client.py` was rewritten to accept multiple stems per required file. `business_spec` is a second accepted alias for `product_spec`. Beacon's spec file is now detected (3/5 → was 2/5) and its contents are pulled into the AI summary context. When both files exist, `PRODUCT_SPEC.md` still wins because the matcher sorts by `(depth, path-length)` and `PRODUCT_SPEC.md` is shorter.
2. **Robust AI-JSON extraction (`ai_analyzer.extract_json_object`).** The old logic in `app.generate_project_summaries` ran `if raw.startswith("\`\`\`"): split[1]` — which produced an empty string when the model returned an empty fenced block. That's what triggered Chris's `Expecting value: line 1 column 1 (char 0)` on beacon. The new helper finds the first `{`, walks brace depth (respecting string literals and escapes), and returns the JSON object — ignoring leading prose, code fences, and trailing commentary. Henry's `analyze_branch` flow uses the same helper, which fixes the cards that displayed raw JSON + a `**Key observations:**` postscript whenever the model wrapped output in fences. Empty/whitespace responses now raise a clear `ValueError` that the caller handles with a meaningful fallback message instead of dumping the raw text into `plain_english_summary`.
3. **Unassigned-projects list on Manage Groups.** `projects()` now computes `unassigned_repos` (every repo whose name doesn't appear in any group's member list) and the template renders them as readonly chips at the bottom of the Manage Groups panel. When every project belongs to at least one group, the section shows an empty-state message instead.

### Technical Details
- **Multi-stem matcher** — `required` dict in `github_client.check_required_files` changed from `display_name → stem` to `display_name → [stems]`. The match loop concatenates path matches across all stems, then applies the existing `(depth, len(path))` sort, so the existing root-preferred / shortest-path tiebreakers still work unchanged.
- **`extract_json_object` state machine** — tracks `depth`, `in_string`, and `escape` flags. Backslashes inside string literals consume the next character regardless of what it is; closing brace at `depth == 0` terminates the slice and hands it to `json.loads`. Smoke-tested against five real-world response shapes: clean JSON, fenced JSON + trailing commentary, fenced JSON only, leading prose, and braces inside string values.
- **Unassigned set** — `assigned = {name for member_list in groups.values() for name in member_list}`. Iterates `all_repos` (already sorted by `updated_at` desc) and filters by membership. Cost is O(R + total-group-members); fine for the scales we care about.
- **Fallback messages** — when `extract_json_object` fails in `analyze_branch`, `plain_english_summary` is set to `"AI response could not be parsed — see Claude Code instructions for manual review."` instead of the prior behavior (dumping the raw response into the summary, which is exactly what Chris saw on the Henry cards).

### Current Status
- ✅ Beacon's `BUSINESS_SPEC.md` is recognized as a product spec (rescan + regenerate summaries to see it)
- ✅ Project-summary JSON parsing no longer crashes on fenced/empty responses
- ✅ Henry cards parse fenced-with-commentary responses; no more raw-JSON dumps
- ✅ Manage Groups panel shows an "Unassigned projects" section with readonly chips
- ✅ 83/83 tests passing (3 new for `BUSINESS_SPEC.md` aliasing, 3 new for unassigned-projects rendering)
- ⚠️ Push to `main` is blocked by the sandbox git server (HTTP 403). All work was pushed to `claude/fix-beacon-visibility-LXO09`; Chris needs to merge that branch into main on his own machine.

### Branch Info
- Working branch in this session: `claude/fix-beacon-visibility-LXO09` (sandbox forced this — local `main` push returned 403)
- Local `main` has the same commits and is up to date with the feature branch
- Not merged yet — Chris merges to `main` via his local clone or the GitHub UI

### Decisions Made
- **`BUSINESS_SPEC.md` aliases `PRODUCT_SPEC.md`** rather than becoming a separate 6th required slot. Same intent, fewer columns to render, and a repo with one or the other still scores correctly.
- **`extract_json_object` lives in `ai_analyzer.py`** (where the henry path already uses it) and is imported by `app.py` rather than duplicated. Single source of truth for "parse JSON from a flaky model response."
- **Unassigned chips are readonly** (no checkbox) — the existing per-group editor forms are the right place to assign a repo to a group. The unassigned list is just a visibility surface, not another edit path.

### Next Steps
1. Chris merges `claude/fix-beacon-visibility-LXO09` into `main` from his local machine.
2. Rescan from My Repos so beacon picks up the `BUSINESS_SPEC.md` detection, then regenerate Projects and Henry summaries.
3. The mid-session comprehensive bug audit was paused before I got past `models.py`. Items flagged but not yet acted on: `models._load_json` doesn't catch corrupt JSON (any data file crash takes the app down); no file-locking on concurrent JSON writes (browser-tab race conditions); `security.decrypt_credentials` only catches `InvalidToken`, not generic corruption. Worth a follow-up session.

### Questions / Blockers
- The sandbox git server refuses `git push origin main` with HTTP 403. Either an ACL needs adjustment or Chris just keeps merging feature branches manually.

---

## Session 10 — 2026-04-24 (Projects sort, Stats groups, persistence, bug audit)

### What We Built
Chris surfaced five UX/stability issues with the April 24 feature drop; I addressed all of them, hardened the group-storage layer, added 12 new tests, and did a comprehensive bug audit that caught one real migration bug.

1. **Projects list sorts by most recently updated (desc)** — `app.py` `projects()` now sorts the repo list by `updated_at` desc before group filtering. Repos with missing or null `updated_at` sink to the bottom deterministically.
2. **Stats: filter by group + "By group" rollup** — new group tab bar on `/stats` (same UX as Projects) plus a "By repo / By group" toggle that aggregates commits or code‑size across each group. Repos in no group roll up under "Ungrouped" so nothing disappears. The mode toggle is hidden server-side when no groups exist or when a single group is already selected (the rollup would be a one-row degenerate view).
3. **Groups persist across codebase wipes** — storage moved from `config/groups.json` (inside the repo) to `~/.repodoctor/groups.json`. A one-shot migration copies any existing legacy file the first time `get_groups()` runs. The old path stays gitignored.
4. **"Lines Added" removed** — was broken (always showed 0 on cold repos because `/stats/code_frequency` is async). Removed the tab, the LOC pro-rating logic, and the now-unused `get_code_frequency` client helper.
5. **200-commit cap lifted** — `_collect_repo_activity` now pages through up to 5000 commits per repo (was capped at 200); the truncation flag is gone so the bar scales with the real count.
6. **Default group seeding on login (temporary)** — `models.seed_default_groups_if_missing()` hard-codes Chris's 5 groups (School, Church, Catholic Games, Infrastructure, Fun) as a one-shot recovery after a codebase wipe. Called from `_init_session` after password verification. Only adds groups that don't already exist, so hand-edits are preserved and re-runs are no-ops.

### Technical Details

- **Sort key**: `lambda r: r.get("updated_at") or ""` — coerces None and missing to empty string so the `reverse=True` sort pushes them last.
- **Stats group filter**: tab bar is server-rendered with the active tab highlighted via `active_group` query param; rollup is client-side in `stats.html` (`rollupByGroup`), so switching views doesn't re-hit GitHub.
- **Groups persistence**: `GROUPS_PATH = os.path.join(os.path.expanduser("~"), ".repodoctor", "groups.json")`. `_save_json` now creates `os.path.dirname(path)` rather than relying on a hard-coded `_ensure_dirs()`, so any storage path just works.
- **Migration bug caught by tests**: `_migrate_legacy_groups` originally called `os.makedirs(USER_DATA_DIR)` — but `USER_DATA_DIR` is fixed to `~/.repodoctor`, so if `GROUPS_PATH` is ever moved (or monkey-patched in tests), migration silently failed. Fixed to `os.makedirs(os.path.dirname(GROUPS_PATH), exist_ok=True)`.
- **Seeding**: `DEFAULT_USER_GROUPS` is a flat dict of group name → sorted repo-name list. `seed_default_groups_if_missing()` returns the list of groups added, which `_init_session` logs via `log_action` so the activity log shows the recovery event.

### Current Status
- ✅ All 5 reported issues fixed (projects sort, stats groups, groups persistence, LOC removed, commit cap lifted)
- ✅ Chris's 5 default groups seeded on login (temporary; flagged in CLAUDE.md for removal)
- ✅ 72/72 tests passing (12 new tests across `TestDefaultGroupSeeding`, `TestLegacyGroupsMigration`, `TestProjectsSortAndStatsFilter`)
- ✅ 1 real bug caught + fixed (legacy-groups migration path mismatch)
- ✅ Test suite doesn't pollute real `~/.repodoctor/` or `config/groups.json`

### Branch Info
- Branch: `claude/sort-projects-group-stats-yXxvE`
- 3 commits pushed to origin
- Not merged yet — awaiting Chris's OK and verification on his local machines

### Decisions Made
- **Seeding is setdefault-per-name**, not all-or-nothing. If Chris has already created some groups but not all, only the missing ones get added.
- **Mode toggle hidden** (server-side) when a specific group is active — rolling up a single group is a useless one-row chart.
- **Removed `get_code_frequency`** entirely rather than leaving it as dead code; its only caller was the deleted Lines Added view.
- **Kept the old `config/groups.json` entry in `.gitignore`** with an updated comment — harmless belt-and-braces in case a pre-migration clone is lying around somewhere.

### Next Steps
1. Chris pulls `claude/sort-projects-group-stats-yXxvE`, logs in locally, and verifies the 5 fixes behave as expected on his real data.
2. Merge to main once verified.
3. Next product rebuild: remove `DEFAULT_USER_GROUPS`, `seed_default_groups_if_missing()`, and its call in `_init_session` — see "Tech Debt" section in `CLAUDE.md`.
4. Still outstanding from earlier sessions: port these features to the Netlify version, parallelize the initial scan.

### Questions / Blockers
None.

---

## Current Project Status

**Overall Progress:** 92%

**What's Working:**
- Secure credential storage (Fernet + PBKDF2 encryption)
- GitHub PAT authentication with scope verification
- Full repo scanning — branch counts, required file checks, **code size (bytes via /languages)**
- Dashboard table with sortable columns (now includes **Size column, numeric-aware sort**), retro terminal UI, sticky headers
- Required files detection — 5 files: CLAUDE.md, LICENSE, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md
- **Recursive spec-file search** — finds spec files anywhere in the tree, skipping vendor dirs, preferring root-level then shallowest path
- Clickable repo detail pages with Product Spec, Project Status, and Session Notes panels
- "Current?" column — detects if docs are fresh (within 7 days of last commit)
- **Project Groups** — create/rename/delete named groups, filter Projects page by group; active group persists in preferences
- **Stats view** — Commits / Code Size / Lines Added bar charts with 1d/3d/1w/2w/1m/2m period selector; empty-period repos collapse below alphabetical divider
- **What's Next view** — aggregated next-step bullets across all repos from AI summaries
- **Refreshed top nav** — bracketed brand on left, menu center, pulsing green user pill + logout chip on right, glowing underline for active page
- 30-minute session auto-lock
- Activity log with color-coded messages
- Netlify deployment — Node.js Express app as serverless function (lagging behind Flask)
- AI project summaries via Claude Haiku (now also picks up CLAUDE.md content and subfolder specs)
- 60/60 tests passing

**What's Broken:** Nothing currently broken

**Tech Stack:**
- **Local dev:** Python + Flask
- **Deployed (Netlify):** Node.js + Express + serverless-http
- **AI:** Anthropic Claude API (Haiku for summaries)
- **GitHub:** REST API v3 with Personal Access Token

**Next Steps:**
1. Merge `claude/add-project-grouping-sq2Hy` to main
2. Pull main to PC (PowerShell) and Mac (Terminal) — see bottom of this file for commands
3. Re-scan repos to see PROJECT_STATUS column populate, Size column, recursive specs, and stats charts
4. Port groups + recursive search + stats + whats-next to the Netlify version
5. Parallelize initial scan (languages call added API traffic)

**Blockers:**
- Free Netlify tier has 10s function timeout (26s on paid). Large GitHub accounts may still time out.
- Netlify version is behind Flask version (April 24 features not ported yet).
- First `/stats` visit on a cold repo may show 0 for Lines Added until GitHub finishes computing `/stats/code_frequency` async; click REFRESH after ~10-20s.

---


## 2026-04-24 (evening) — Stats + What's Next + Nav Refresh + Size Metric

### What Was Accomplished
- **Stats page (`/stats`)** with three bar-chart views and a 1d/3d/1w/2w/1m/2m period selector:
  1. **Commits** — age-bucketed counts from `/commits?since=...`; `N+` suffix when truncated (200 commits max)
  2. **Code Size** — byte sum from `/languages`, formatted as B / KB / MB
  3. **Lines Added** — weekly buckets from `/stats/code_frequency` with explicit overlap pro-rating so sub-week periods estimate cleanly
- **What's Next page (`/whats-next`)** — aggregates AI-generated `next_steps` bullets from every repo's summary into a single card grid, sorted alphabetically, each linking to repo detail.
- **Code Size as a first-class metric** — scan now makes one extra call to `/languages` per repo and stores `code_size_bytes` + `languages` breakdown on each repo. Surfaces as a sortable dashboard column (handles unit suffixes correctly via `data-sort-value`) and powers the Stats "Code Size" view.
- **Refreshed top nav** — fresh CSS for `.top-nav` with subtle gradient, blur backdrop, pulsing green user pill on the right, glowing underline for the active link. Added "Stats" and "What's Next" menu items.

### Technical Details
**New routes in `app.py`:**
- `GET /stats` — fetches per-repo commits + code_frequency in parallel via `ThreadPoolExecutor(max_workers=8)`, caches keyed by scan identity, re-renders instantly on revisit. `?refresh=1` forces recompute.
- `GET /whats-next` — reads `project_summaries.json`, groups by repo, sorts alphabetically.

**New methods in `github_client.py`:**
- `get_language_bytes(owner, repo)` → `{lang: bytes}` (`/languages`)
- `get_commits_since(owner, repo, since_iso, ref, max_pages=3)` → list of commits (`/commits?since=...`)
- `get_code_frequency(owner, repo)` → list of `[week_ts, adds, dels]` rows (`/stats/code_frequency`); returns `None` on 202/204

**Overlap math for LOC pro-rating (in `_collect_repo_activity`):**
- Each week bucket spans `[age_days-7, age_days]` days old
- For each period `[0, pdays]`, overlap = `max(0, min(pdays, bucket_hi) - bucket_lo)`
- Fraction = overlap / 7; apply to `additions`

**Files Modified:**
- `app.py` — added `/stats`, `/whats-next`, `_collect_repo_activity`, stats cache, languages fallback in scan error path, cache invalidation on rescan
- `github_client.py` — three new client methods + added `get_language_bytes()` call to `scan_repo_lite` with `code_size_bytes` and `languages` on returned dict
- `templates/base.html` — rewrote nav layout; added Stats / What's Next menu items; brand now has bracketed wrapper
- `templates/dashboard.html` — new Size column (position 12), colspan bumped 12→13, sort script prefers `data-sort-value` when present
- `templates/stats.html` — new file, CSS bar chart driven by embedded JSON, client-side view+period switching
- `templates/whats_next.html` — new file, sorted card grid with per-repo next-steps
- `static/css/style.css` — rewrote `.top-nav` and children, new `.stats-*` + `.whatsnext-*` + `.size-value` styles

### Current Status
- ✅ All 6 user-requested items shipped
- ✅ 60/60 tests still passing
- ✅ End-to-end tested with mocked GitHub client (commits bucketing, LOC pro-rating, size formatting at all scales)
- ✅ Boundary math for LOC verified: 1w period with 1-week-old bucket correctly yields ~full week

### Branch Info
- Working branch: `claude/add-project-grouping-sq2Hy`
- Ready to merge to main: **Yes** — feature-complete, tested, pushed. Branch name is now misleading (scope expanded beyond grouping) but stable.

### Decisions Made
- "Lines of code" = weekly additions from `code_frequency`. Exact daily resolution is not achievable without per-commit `/stats` calls; sub-week periods are pro-rated with explicit overlap math.
- "Code size" = byte sum from `/languages`. Not literal LOC; labeled clearly as "Code Size" and in tooltips.
- Stats data cached **in memory only**, keyed by scan identity. Deliberate: user controls freshness via explicit scan or `?refresh=1`.
- What's Next reads from `project_summaries.json` (cheap, already cached) rather than re-parsing spec files across every repo (expensive).
- Nav kept entirely in base.html + CSS — no JS dependency, no CDN, retro aesthetic preserved.

### Next Steps
1. Merge `claude/add-project-grouping-sq2Hy` to main
2. Follow the **Pull Instructions** at the bottom of this file to sync both machines
3. Port this work to the Netlify Node.js app

---


## 2026-04-24 — Project Groups + Recursive Spec Search


## 2026-04-24 — Project Groups + Recursive Spec Search

### What Was Accomplished
- **Project Groups feature:** named groups of repos on the Projects page, tab bar filter at top (All / GroupName), plus a collapsible "Manage Groups" panel for create/rename/delete/assign-repos. Active group persists in preferences so filter survives navigation.
- **Recursive spec-file search:** switched from root-only to GitHub's git-trees recursive API. Spec files (PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md, CLAUDE.md, LICENSE) are now found anywhere in the tree, with root-level matches preferred and vendor dirs (node_modules, dist, .venv, target, etc.) excluded.
- **Re-introduced PROJECT_STATUS.md as a required file** — now recognized alongside the other specs, with its own column on the dashboard and its own panel on the repo detail page.
- **Comprehensive testing + bug fixes** — ran end-to-end Flask tests of groups flow, mocked-tree tests of recursive search, fixed 6 bugs found.

### Technical Details
**New storage:**
- `config/groups.json` — per-machine `{group_name: [repo_names]}` mapping (gitignored)
- `active_group` key added to preferences

**Files Modified:**
- `app.py` — `/projects` filters by active group; new routes `/projects/groups/save` and `/projects/groups/delete`; `repo_detail` and `generate_project_summaries` now use recursive paths
- `github_client.py` — new `get_all_file_paths` using `/git/trees/{ref}?recursive=1`; `check_required_files` rewritten for recursive search with vendor-dir skip list; `get_file_content` now URL-encodes paths (spaces, `#`, etc.)
- `models.py` — new `get_groups`, `set_group`, `rename_group`, `delete_group`, `save_groups`; `active_group` added to DEFAULT_PREFS
- `templates/projects.html` — new group tab bar, collapsible Manage Groups panel
- `templates/dashboard.html` — added PROJECT_STATUS column + legend entry, fixed missing-files stat to use `files_total` dynamically, fixed colspans 11→12
- `templates/repo_detail.html` — added Project Status panel
- `static/css/style.css` — styles for `.group-bar`, `.group-tab`, `.manage-groups`, `.group-editor`, etc.
- `.gitignore` — ignore `config/groups.json`
- `tests/test_app.py` — rewrote TestRequiredFiles for recursive/5-file world; added TestGroups class (10 new tests)

### Bugs Found During Testing and Fixed
| Severity | Bug | Fix |
|---|---|---|
| Critical | `rename_group` silently clobbered an existing target group | Refuse rename if target name already exists, with user-facing error |
| High | `get_file_content` didn't URL-encode paths; `#` truncated the URL | `urllib.parse.quote(path, safe="/")` |
| Medium | DELETE button used editable `group_name` field, could target wrong group if user was mid-edit | Route prefers hidden `original_name` |
| Medium | Dashboard missing PROJECT_STATUS column + legend entry | Added both |
| Low | Missing-files stat hardcoded `< 4` | Namespace counter using `files_present < files_total` |
| Low | Score-good threshold hardcoded `>= 4` | Uses `files_total - 1` |

### Current Status
- ✅ Groups feature live (create / rename / delete / assign / filter)
- ✅ Recursive spec search live — finds specs in subfolders
- ✅ PROJECT_STATUS.md back as a required file
- ✅ 60/60 tests passing
- ❌ Netlify version NOT yet updated with these changes

### Branch Info
- Working branch: `claude/add-project-grouping-sq2Hy`
- Ready to merge to main: **Yes** — feature branch clean, pushed, tests green

### Next Steps
1. Merge `claude/add-project-grouping-sq2Hy` to main
2. Re-scan repos to verify recursive search finds subfolder specs
3. Port changes to Netlify version (github-client.js + api.js + views)

### Decisions Made
- Gitignore `config/groups.json` — it's per-machine user state, same as `scan_history.json`
- Groups are NOT scoped per-user — single-user app
- Vendor/build dirs hardcoded in `_SKIP_PATH_SEGMENTS` rather than configurable (KISS)
- Root-level file wins over deeper matches; ties broken by shortest path string

---


## 2026-03-25 — Dashboard File Requirements Update

### What Was Accomplished
- Reduced required files from 6 to 4: CLAUDE.md, LICENSE, PRODUCT_SPEC.md, SESSION_NOTES.md
- Removed BUSINESS_SPEC.md and PROJECT_STATUS.md from all checks (content consolidated into PRODUCT_SPEC.md and SESSION_NOTES.md)
- Renamed "PROD" column heading to "SPEC" on the dashboard
- Fixed 2 pre-existing test failures (stale hardcoded dates)
- Added 5 new tests for the required files configuration
- Updated both Flask and Netlify versions consistently
- Consolidated .md files: merged business spec into PRODUCT_SPEC.md, merged project status into SESSION_NOTES.md
- Deleted BUSINESS_SPEC.md and PROJECT_STATUS.md

### Technical Details
**Files Modified:**
- `github_client.py` — Removed business_spec and project_status from required files dict
- `app.py` — Removed BUSINESS_SPEC and PROJECT_STATUS from spec_files and summary generation
- `templates/dashboard.html` — Removed BIZ/STATUS columns, renamed PROD→SPEC, updated colspan 13→11
- `templates/repo_detail.html` — Removed Business Spec and Project Status panels
- `netlify/functions/api/lib/github-client.js` — Same required files update
- `netlify/functions/api/api.js` — Same spec_files and threshold update
- `netlify/functions/api/views/dashboard.html` — Same column changes
- `netlify/functions/api/views/repo_detail.html` — Same panel removal
- `tests/test_app.py` — Fixed stale test dates, added TestRequiredFiles class
- `PRODUCT_SPEC.md` — Incorporated business context, updated to version 7.0
- `SESSION_NOTES.md` — Incorporated project status section

**Files Deleted:**
- `BUSINESS_SPEC.md` — Content merged into PRODUCT_SPEC.md section 2.1
- `PROJECT_STATUS.md` — Content merged into SESSION_NOTES.md header

### Current Status
- ✅ Dashboard shows 4 required files (CLAUDE, LICENSE, SPEC, NOTES)
- ✅ All 47 tests passing
- ✅ Both Flask and Netlify versions updated consistently

### Branch Info
- Working branch: `claude/update-dashboard-requirements-3qOHK`
- Ready to merge to main: Yes

### Next Steps
1. Merge to main
2. Re-scan repos to verify scores show X/4
3. Update other repos to drop BUSINESS_SPEC.md and PROJECT_STATUS.md

---


## 2026-03-11 — Serverless Cold Start & Timeout Fixes

### What Was Accomplished
- Fixed "Not authenticated with GitHub" error after serverless cold starts
- Fixed "Inactivity Timeout" error when scanning repos or generating summaries
- Parallelized repo scanning (batches of 10) and summary generation (batches of 5)
- Set max function timeout (26s) in netlify.toml

### Technical Details
**Files Modified:**
- `netlify/functions/api/api.js` — Added `tryEnvCredentials()` call in `requireAuth` middleware to restore GitHub client from env vars after cold starts. Refactored `/scan` route to process repos in parallel batches of 10 using `Promise.all`. Refactored `/projects/generate` route to process repos in parallel batches of 5 using `Promise.all` (extracted `generateOneSummary` helper).
- `netlify.toml` — Added `[functions."api"]` section with `timeout = 26` (max allowed on Netlify paid plans).

**Key Decisions:**
- Batch size of 10 for scan (lightweight GitHub API calls) vs 5 for generate (heavier — GitHub + Anthropic API calls per repo)
- Restore credentials from env vars on every authenticated request, not just at login — essential for serverless where in-memory state is ephemeral

### Current Status
- ✅ Cold start auth loss fixed
- ✅ Scan route parallelized
- ✅ Summary generation parallelized
- ✅ Max timeout configured
- ✅ Module loads without errors
- 🚧 Needs live verification on Netlify

### Branch Info
- Working branch: `claude/deploy-netlify-F7VHx`
- Ready to merge to main: After live verification

### Decisions Made
- Parallel batching over streaming/background functions (simpler, stays within serverless model)
- 26s timeout is max allowed — if repos still time out, would need Netlify Background Functions (15min limit but no response to client)

### Next Steps
1. Deploy and verify scan + summary generation work on live Netlify site
2. If timeouts persist with many repos, consider Netlify Background Functions
3. Merge to main once verified

### Questions/Blockers
- Free tier has 10s timeout (26s only on paid). May need paid plan for large GitHub accounts.

---


## 2026-03-11 — Netlify Deployment (Node.js Refactor)

### What Was Accomplished
- Deployed RepoDoctor to Netlify as a serverless app
- Discovered Netlify Functions only support JS/TS/Go — NOT Python
- Rewrote the entire Flask backend to Express.js (Node.js) for Netlify Functions
- Ported all active routes: login, dashboard, scan, repo detail, settings, projects, mac setup
- Ported all backend modules: GitHub client, models (in-memory), spec cleaner
- Adapted all Jinja2 templates to Nunjucks (Jinja2-compatible JS engine)
- UI is 100% preserved — same retro terminal CSS, same vanilla JS, same layout
- Added site password gate via SITE_PASSWORD env var for deployed version
- All credentials handled via Netlify environment variables (GITHUB_PAT, ANTHROPIC_API_KEY, SITE_PASSWORD, FLASK_SECRET_KEY)

### Technical Details
**Files Created:**
- `package.json` — Node.js dependencies (express, serverless-http, nunjucks, cookie-session)
- `netlify.toml` — Build config, function directory, redirects
- `netlify/functions/api/api.js` — Express app with all routes, wrapped in serverless-http
- `netlify/functions/api/lib/github-client.js` — GitHub REST API client using native fetch
- `netlify/functions/api/lib/models.js` — In-memory data storage (ephemeral in serverless)
- `netlify/functions/api/lib/spec-cleaner.js` — Markdown cleaning + What's Next extraction
- `netlify/functions/api/views/*.html` — All 7 templates adapted from Jinja2 to Nunjucks

**Files Modified:**
- `.gitignore` — Added node_modules/, .netlify/
- `app.py` — Reverted serverless env var changes (Python app is for local dev only)
- `requirements.txt` — Removed awsgi dependency

**Files Removed:**
- `netlify_handler.py` — Old Python serverless wrapper (doesn't work on Netlify)
- `netlify_build.sh` — Old Python build script

**Key Decisions:**
- Used Nunjucks template engine (nearly 1:1 compatible with Jinja2) to minimize template changes
- Used native fetch (Node 18+) for GitHub and Anthropic API calls to keep bundle small
- In-memory data storage — scan results, preferences, specs reset on cold starts (acceptable for serverless)
- Used cookie-session for auth state (persists across requests via signed cookies)
- Used node_bundler = "nft" (Node File Trace) for better template file inclusion in function bundle
- Anthropic API called directly via fetch (no SDK) to minimize function bundle size

### Current Status
- ✅ All templates render correctly (tested locally)
- ✅ Express handler returns 200 for /login and 302 redirect for unauthenticated /
- ✅ All 7 templates adapted and tested
- ✅ GitHub client ported with all methods
- ✅ Spec cleaner ported with markdown cleaning + What's Next extraction
- ✅ Committed and pushed to `claude/deploy-netlify-F7VHx`
- ✅ Fixed static asset serving — CSS/JS now served from CDN via dist/static/ build step
- 🚧 Needs Netlify rebuild to verify live deployment with CSS fix

### Branch Info
- Working branch: `claude/deploy-netlify-F7VHx`
- Ready to merge to main: After verifying Netlify deployment works

### Decisions Made
- Netlify over Render/Railway/Fly.io (user preference)
- Node.js refactor over Python workarounds (Netlify doesn't support Python functions)
- Preserved 100% of the UI — no CSS/JS changes

### Next Steps
1. Verify the Netlify deployment works at repodoctor2.netlify.app
2. Set environment variables in Netlify dashboard (GITHUB_PAT, ANTHROPIC_API_KEY, SITE_PASSWORD, FLASK_SECRET_KEY)
3. Test scan + repo detail on live site
4. Merge to main once verified

### Questions/Blockers
- Netlify Functions have a 10-second timeout (26s on paid plans). Scanning many repos may time out.
- Data is ephemeral in serverless — scan results reset on cold starts. Consider Netlify Blobs for persistence if needed.

---


## 2026-03-08 — Sticky Headers + Staleness Threshold Fix

### What Was Accomplished
- Made dashboard table headers persistent/sticky so column labels stay visible when scrolling down through repos
- Fixed the "Current?" column staleness logic — was using a 4-hour threshold which was far too aggressive. A single commit hours after doc updates would mark docs as stale. Changed to 7-day threshold so docs are only flagged stale if they haven't been touched within a week of the latest repo activity
- This directly addresses the "longwayhome" repo incorrectly showing as not updated

### Technical Details
**Files Modified:**
- `static/css/style.css` — Added `position: sticky; top: 0; z-index: 10;` to `.repo-table thead`, set background opacity to 1 (was 0.8, which caused content to bleed through). Added `max-height: 75vh` and `overflow-y: auto` to `.repo-table-wrap` so sticky works within the scrollable container.
- `github_client.py` — Changed `staleness_threshold` from `timedelta(hours=4)` to `timedelta(days=7)`. Updated comments to match.

**Key Decisions:**
- 7-day threshold balances catching genuinely stale docs vs false positives from normal development cadence
- Used `max-height: 75vh` so the table scrolls within the viewport rather than the whole page
- Set thead background to fully opaque so table rows don't show through the sticky header

### Current Status
- ✅ Sticky headers working
- ✅ Staleness threshold relaxed to 7 days
- ✅ Committed and pushed to `claude/fix-updated-status-6y3UR`

### Branch Info
- Working branch: `claude/fix-updated-status-6y3UR`
- Ready to merge to main: Yes

### Next Steps
1. Merge to main and re-scan to verify longwayhome shows correct status
2. Consider making the staleness threshold configurable in settings
3. Test sticky headers on mobile/small screens

---


## 2026-03-07 — "Updated?" Column + Case-Sensitivity Fix

### What Was Accomplished
- Added a new "Updated?" column to the main dashboard table
- The column intelligently detects whether PRODUCT_SPEC.md and SESSION_NOTES.md were committed within 24 hours of the most recent repo commit
- Shows YES (green) if docs are fresh, NO (red) if stale, or — if the doc files don't exist
- Helps Chris catch when he forgets to update session docs at the end of a Claude Code chat
- Fixed case-sensitivity bug: `check_required_files` now returns actual filenames from disk so that commit lookups and file detection work regardless of casing (e.g. `product_spec.md` vs `PRODUCT_SPEC.md`)

### Technical Details
**Files Modified:**
- `github_client.py` — Added `get_last_commit_for_path()` and `get_last_commit_date()` methods. Refactored `check_required_files()` to return a tuple of `(results, actual_names)` — the `actual_names` dict maps display names to real filenames on disk, enabling case-insensitive commit timestamp lookups. Updated `scan_repo_lite()` to use actual filenames when querying the GitHub commits API.
- `templates/dashboard.html` — Added "Updated?" column header (sortable, with tooltip), added cell rendering with YES/NO/— display, updated colspan values from 12 to 13.

**Key Decisions:**
- 24-hour threshold chosen as the window for "close enough" — if docs were updated within 24h of the last code commit, they're considered fresh
- Uses `abs()` on the time difference so it works regardless of commit order (docs could be committed slightly before or after code)
- Reuses existing `file-ok` and `file-missing` CSS classes for consistent green/red styling
- Only makes the extra API calls if at least one of the two doc files exists in the repo (avoids wasting API calls)
- `check_required_files` builds a `stem_to_actual` map preserving the real filename for each stem match, so downstream code can reference files by their actual name on GitHub

### Current Status
- ✅ "Updated?" column working end-to-end
- ✅ Case-insensitive file matching for both detection and commit lookups
- ✅ Committed and pushed to `claude/mac-shortcut-projects-page-wA27C`
- 🚧 Needs merge to main

### Branch Info
- Working branch: `claude/mac-shortcut-projects-page-wA27C`
- Ready to merge to main: Yes

### Next Steps
1. Merge PR to main and test with a full repo scan
2. Consider adding a tooltip or hover detail showing the actual timestamps
3. Consider automating the session-end doc update process so the "Updated?" column is always YES

---


## 2026-03-02 — Flexible File Matching, Column Reorder, Repo Detail Pages

### What Was Accomplished
- Fixed file matching so required-file checks are case-insensitive and accept any extension (e.g. BUSINESS_SPEC.pdf now counts as a hit)
- Replaced 6 individual GitHub API calls per repo with a single root directory listing — faster and more reliable
- Reordered dashboard columns: CLAUDE, LICENSE, BIZ, PROD, STATUS, NOTES
- Made repo names clickable — links to a new detail page instead of GitHub
- Built repo detail page showing: file status grid, full contents of BUSINESS_SPEC, PRODUCT_SPEC, PROJECT_STATUS, and SESSION_NOTES fetched live from GitHub, plus branch list
- Added `get_root_files()` and `get_file_content()` methods to github_client.py

### Technical Details
**Files Modified:**
- `github_client.py` — Replaced `check_file_exists()` with `get_root_files()` for flexible stem-based matching; added `get_file_content()` for fetching file text via base64 decode
- `app.py` — Added `/repo/<owner>/<name>` route with file content fetching; added created_at/updated_at to error fallback data
- `templates/dashboard.html` — Reordered columns (CLAUDE, LICENSE, BIZ, PROD, STATUS, NOTES); repo names link to detail page
- `templates/repo_detail.html` — Rewritten as spec summary view with file status grid, collapsible spec panels, branch list
- `static/css/style.css` — Added styles for breadcrumb, repo info bar, file status grid, spec panels

**Key Decisions:**
- File matching uses stem comparison: strip extension, lowercase, then match. So `BUSINESS_SPEC.pdf`, `business_spec.md`, `Business_Spec.txt` all match `business_spec`
- Single directory listing per repo instead of 6 HEAD/GET requests — reduces API calls by ~83%
- Spec file content truncated at 10,000 chars to avoid overloading the detail page

### Current Status
- ✅ Flexible file matching (case-insensitive, any extension)
- ✅ Dashboard column reorder
- ✅ Clickable repo detail pages with spec content
- ✅ All changes committed and pushed to `claude/practical-allen-3anQ3`
- 🚧 Needs merge to main

### Branch Info
- Working branch: `claude/practical-allen-3anQ3`
- Ready to merge to main: Yes

### Next Steps
1. Merge PR to main and redeploy
2. Re-scan repos to verify BUSINESS_SPEC.pdf now shows as hit for missionIQ
3. Add LICENSE, PROJECT_STATUS.md to repos that are missing them

---


## 2025-02-14 — Pdf Implementation
**Source:** `repodoctor2-2025-02-14-pdf-implementation.txt`

### What Was Accomplished
- Push to main is restricted. Local merge done. You can merge on GitHub via PR or push from permitted account.

### Technical Details
**Files Modified/Created:**
- `ai_analyzer.py`
- `app.js`
- `app.py`
- `github_client.py`
- `models.py`
- `security.py`
- `style.css`
- `test_app.py`

**Key Commands:**
- `git clone`
- `pip install`
- `python app.py`

**URLs Referenced:**
- http://127.0.0.1:5001
- http://localhost:5001
- https://github.com/christreadaway/repodoctor2.git
- https://github.com/christreadaway/repodoctor2/pull/new/claude/build-pdf-implementation-3vrlm

### Issues/Notes
- [Attempted to push to main - authentication failed]
- [Attempted to push main - authentication error]

---


## Pull Instructions — Syncing PC and Mac After Merge

After merging `claude/add-project-grouping-sq2Hy` to `main` on GitHub:

### On Mac (Terminal)
```
cd ~/repodoctor2
git checkout main
git fetch origin
git pull origin main
git branch -d claude/add-project-grouping-sq2Hy   # optional cleanup of local feature branch
pip install -r requirements.txt                   # in case deps changed
python3 app.py
```

### On PC (PowerShell)
```
cd $HOME\repodoctor2
git checkout main
git fetch origin
git pull origin main
git branch -d claude/add-project-grouping-sq2Hy   # optional cleanup of local feature branch
pip install -r requirements.txt                   # in case deps changed
python app.py
```

### If you have uncommitted local changes
Stash before pulling so nothing is lost:
```
git stash
git pull origin main
git stash pop                                      # reapply your local work
```

### After pulling — first-time setup touches on each machine
- `config/groups.json` is gitignored — groups you create on Mac will NOT sync to PC (and vice versa). That's by design; they're per-machine state.
- `config/preferences.json` IS tracked, so `active_group` / model choice / excluded repos sync between machines.
- Run a fresh **SCAN** from the dashboard so the new `code_size_bytes` + recursive spec detection populate.
- Visit `/stats` once; the first view computes stats across all repos (takes a few seconds; cached after that).

---
