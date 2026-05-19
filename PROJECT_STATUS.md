# RepoDoctor2 — Project Status

**Last Updated:** 2026-05-19 (Session 14)
**Current Branch:** `claude/add-project-tracker-L4awW` (pushed) — needs merge to `main`
**Overall Progress:** ~96%

---

## Snapshot

RepoDoctor2 is a Flask web app that visualizes all your GitHub repos: branch counts, required-spec-file presence, code size, AI-generated project summaries, named groups for filtering, a Stats view with Commits / Code Size bar charts across time periods (aggregatable by group), and an aggregated What's Next view. Runs locally; also deployed on Netlify as a Node.js serverless app (Netlify lags Flask).

---

## What's Working

### Codebase Tracker (NEW 2026-05-19 Session 14)
- Per-repo deep view at `/tracker` accessible from a dedicated nav link. Dropdown picks any scanned repo; landing page auto-redirects to the most recently generated tracker when one exists.
- Eight tabs — Overview / Recent / Next Actions / Modules / Infra Gaps / Features / External Systems / Questions — each with color-coded chips (red P0, orange P1, amber prototype, sky visual, green functional, red missing) that depart from the all-green palette.
- Stable monotonic IDs (`M*`, `I*`, `F*`, `E*`, `Q*`, `N*`) preserved across regenerations. The generator passes the prior tracker into Claude with "LOAD-BEARING IDS" instructions; new rows get max+1 per prefix.
- AI generation reads PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, CLAUDE.md, README, the file tree (vendor dirs excluded), the last 30 days of commits, and the prior tracker if present. Uses the configured AI model (Haiku 4.5 default — ~$0.02-$0.05 per generation). Surfaced in the toolbar before clicking Generate.
- §5.5 invariants validated at save time AND in unit tests (16 distinct rules: ID format + uniqueness, status / priority / effort / kind enums, infra blocks point at real modules, feature modules point at real modules, next-action `related_ids` point at real M/I/F/E/N IDs, `depends_on` is acyclic and never self-references, prompt ≥ 50 chars, recent_changes newest-first, dates `YYYY-MM-DD`). Invalid AI output triggers one retry with the validation errors fed back; second failure errors cleanly instead of saving corrupt data.
- Copy/paste-ready Claude Code prompt on every next-action card, with optional inline preview before copying. Server-side event log at `data/logs/tracker.log` (one JSON event per line) captures every generation, validation pass/fail, render warning, and copy-prompt event.
- Debug surface at `/tracker/<owner>/<name>/debug` shows a live integrity check, tracker meta, and the tail-100 event log with a **Copy for Claude Code** button formatting the buffer as a markdown block for paste-back debugging.
- **Firestore auto-detection plumbed in:** when `firestore_detector` finds Firebase signals in a repo, the detection results (status, project ID, indicators, missing config) get fed to the prompt with instructions to emit a Firestore row in `external_systems` plus `infra_gaps` + `next_actions` rows for any missing config. The fleet-level Firestore page moved from main nav to **Settings → Tools** (still works at `/firestore`).
- 39 new unit tests covering every invariant + storage round-trip + path-traversal safety + Firestore prompt inclusion.

### Core
- **Auth & security:** Fernet + PBKDF2 credential encryption, 30-minute session auto-lock, GitHub PAT scope verification
- **GitHub auth-error handling (NEW 2026-05-15):** every 401 from GitHub now surfaces a clear, actionable remediation message instead of silently producing an empty dashboard. Login refuses to grant access if the saved PAT is invalid/revoked/expired. A new RESET CREDENTIALS button on the login page lets locked-out users recover in one click.
- **Scan:** repo list + branch counts + required-file detection + code size (bytes per language)
- **Required files (5):** CLAUDE.md, LICENSE, PRODUCT_SPEC.md (or `BUSINESS_SPEC.md` — accepted as an alias), PROJECT_STATUS.md, SESSION_NOTES.md — case-insensitive, any extension, searched recursively (root preferred, vendor dirs skipped)
- **Corrupted-JSON tolerance (NEW 2026-05-15):** if `preferences.json` / `groups.json` / etc. gets corrupted, the file is renamed to `.corrupt` and the app starts fresh instead of crashing every request.

### Dashboard (My Repos)
- Sortable columns, sticky headers, per-file Y/- indicators, X/5 score
- Size column (B / KB / MB) — numeric sort honors unit suffixes
- "Current?" freshness column, expandable branch lists
- **Henry branches excluded from the count (NEW 2026-05-15 Session 13):** per-repo Branches column and cross-repo Total Branches summary use `non_henry_branch_count`. Henry branches still appear in the expandable list, marked faded + italic with a `(henry)` suffix and tooltip explaining the exclusion.

### Projects
- AI-generated summaries via Claude Haiku (~$0.001/project)
- Robust JSON parsing on the model response — handles fenced blocks, leading prose, and trailing commentary; empty/whitespace responses now raise a clear error rather than crashing
- **Sorted by most recently updated (desc)** by default; missing dates sink to the bottom
- **Project Groups** — tab-bar filter (All / named groups) + collapsible Manage Groups panel for create/rename/delete/assign
- **Unassigned-projects section** at the bottom of Manage Groups — readonly chips listing every repo that isn't a member of any group, or an "Every project belongs to at least one group" empty-state
- **5 default groups seeded on login** (School, Church, Catholic Games, Infrastructure, Fun) — temporary one-shot recovery; removal flagged in `CLAUDE.md`

### Henry Branches
- Per-branch AI summary (Haiku) of changes vs. default branch — what was done, screen-by-screen impact, risk, merge strategy, copy/paste Claude Code instructions
- Same robust JSON extractor as Projects — no more raw-JSON-blob fallbacks when the model adds commentary after the code fence
- **Defensive commit metadata access (NEW 2026-05-15):** commits with missing committer/author/message no longer KeyError

### Stats
- Two views: **Commits**, **Code Size** (Lines Added removed — was broken on cold repos)
- Period selector: 1d / 3d / 1w / 2w / 1m / 2m (applies to Commits)
- **Group filter bar** mirrors Projects: All / named groups, server-rendered with `?group=<name>`
- **"By repo" / "By group" rollup toggle** (hidden when no groups or when a single group is active) — aggregates commits and code-size across each group; repos in no group roll up under "Ungrouped"
- No artificial cap on commit count (was 200; now effectively unlimited with a 5000-commit safety ceiling per repo)
- Empty-period repos drop below an alphabetical divider
- Per-scan in-memory cache; `?refresh=1` forces recompute

### Storage Persistence
- **Groups live in `~/.repodoctor/groups.json`** — survives wiping/re-cloning the project directory
- Legacy `config/groups.json` auto-migrated on first read

### What's Next
- Aggregated next-step bullets across every repo with an AI summary
- Per-repo cards sorted alphabetically, linking to repo detail

### Nav
- Sticky top nav, bracketed brand, pulsing green user pill + logout chip on the right
- Active link gets a glowing green underline
- Menu: My Repos · Projects · Stats · What's Next · Mac Setup · Settings

### Other
- Repo detail: Product Spec / Project Status / Session Notes panels, "What's Next" hero, conversation timeline
- Activity log with color-coded messages
- Netlify deployment (Node.js + Express + serverless-http) — older feature set
- **Tests:** 136 passing (97 from Session 13 + 39 new for the Codebase Tracker). 5 of the 39 tracker tests skip in remote envs lacking the `anthropic` SDK; they run locally on Mac.

---

## What's In Progress

Nothing actively in progress. All Session 14 work (Codebase Tracker + Firestore relocated to Settings + dashboard cellpadding fix) is committed on `claude/add-project-tracker-L4awW` and pushed; needs merge to `main`.

---

## What's Broken / Known Issues

- **Netlify version is behind Flask version.** All April/May features need porting to `netlify/functions/api/`.
- **Netlify free tier 10s function timeout.** Large GitHub accounts can still time out even with parallelized scans.
- **Truncated git trees.** Very large repos can have a truncated tree response; a warning is logged and some deep files may be missed.
- **Scan is slightly slower.** `/languages` call per repo doubles API calls vs. previous. Still sequential; parallelization on the backlog.
- **Stats first-load latency.** Commit stats page through up to 5000 commits per repo × N repos — first visit after a scan can take noticeably longer for heavy accounts. `?refresh=1` forces recompute; normal loads are cache hits.
- **Anthropic key not verified at login.** An invalid/empty Anthropic key only surfaces when the user runs `/projects/generate` or `/henry/generate`, where it shows up as a per-repo error. Flagged in audit; low-frequency since the key rarely changes after first setup.
- **`_save_json` has no disk-full / permission error handling.** Silent data loss possible on a full disk. Flagged in audit; revisit if it actually bites.
- **`/login/reset` has no CSRF protection.** Localhost-only app + same-origin requirement on form POSTs mitigate the risk; revisit if the app ever leaves localhost.

---

## Tech Debt — Remove on Next Rebuild

- **Default group seeding** (`models.seed_default_groups_if_missing`, `DEFAULT_USER_GROUPS`, and the call from `app._init_session`) was added April 2026 as a one-shot recovery. Groups now persist at `~/.repodoctor/groups.json` — delete the seeding path on the next rebuild. See `CLAUDE.md` → "Tech Debt" section for details.

---

## Next Steps (in priority order)

1. **Merge `claude/add-project-tracker-L4awW` into `main`** from Chris's local machine. (Supersedes the pending Session 13 merge — Session 14 branched off that work, so a single merge picks up both.)
2. **Try the tracker against a real repo.** Pull, restart Flask, click Tracker in the nav, pick a repo (e.g. parentpoint, catholicevents), click GENERATE TRACKER. Verify the eight tabs populate and the next-actions prompts copy cleanly into Claude Code.
3. **Generate trackers across the portfolio** to seed the load-bearing IDs. Re-generations from this point on will preserve those IDs.
4. **Wire tracker `next_actions` into What's Next** so open P0/P1 actions show up in the simple aggregated inbox alongside the project-summary bullets (Roadmap item 1).
5. **Port April/May features to Netlify** — auth-error handling, Stats (with group filter + rollup), What's Next, groups persistence, recursive search, Size column, refreshed nav, recent-updated sort, business-spec aliasing, unassigned-projects list. (Tracker stays Flask-only by design — local JSON storage doesn't fit serverless.)
6. **Parallelize initial scan** using `ThreadPoolExecutor` (same pattern as `/stats`).
7. **Verify Anthropic key at login** the same way GitHub PAT is verified, so bad keys are caught up front.

---

## Tech Stack

| Layer | Local (Flask) | Deployed (Netlify) |
|---|---|---|
| Backend | Python 3 + Flask | Node.js + Express + serverless-http |
| AI | `anthropic` SDK (Claude Haiku for summaries) | `@anthropic-ai/sdk` |
| GitHub | REST API v3 + `requests` | REST API v3 + `node-fetch` |
| Security | Fernet + PBKDF2 | — (relies on environment) |
| Storage | Local JSON in `config/`, `data/`, and `~/.repodoctor/` | Ephemeral per-invocation |
| Frontend | Jinja2 templates + retro terminal CSS | EJS-style views + same CSS |
| Tests | `unittest` (97 passing) | — |

---

## File Paths / Workflow

- Start command: `cd ~/repodoctor2 && python3 app.py` (runs on `http://127.0.0.1:5001`)
- Netlify build artifacts: `netlify_functions/` (gitignored)
- **User data (survives codebase wipes):** `~/.repodoctor/groups.json`
- User data in repo (all gitignored, do not survive a fresh clone): `config/credentials.enc`, `config/groups.json` (legacy), `data/scan_history.json`, `data/analysis_cache.json`, `data/action_log.json`, `data/project_summaries.json`, `data/specs/`
- Session logs: `SESSION_NOTES.md` (append new entries at top)
