# RepoDoctor2 — Project Status

**Last Updated:** 2026-05-15 (Session 13)
**Current Branch:** `claude/fix-flask-display-issue-1yxlq` (pushed) — needs merge to `main`
**Overall Progress:** ~95%

---

## Snapshot

RepoDoctor2 is a Flask web app that visualizes all your GitHub repos: branch counts, required-spec-file presence, code size, AI-generated project summaries, named groups for filtering, a Stats view with Commits / Code Size bar charts across time periods (aggregatable by group), and an aggregated What's Next view. Runs locally; also deployed on Netlify as a Node.js serverless app (Netlify lags Flask).

---

## What's Working

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
- **Tests:** 97 passing (83 inherited + 9 new for `GitHubAuthError` handling + 5 new for henry-branch exclusion)

---

## What's In Progress

Nothing actively in progress. All 2026-05-15 work (Sessions 12 and 13) is committed on `claude/fix-flask-display-issue-1yxlq` and pushed.

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

1. **Chris rotates his GitHub PAT.** The 2026-05-15 session was triggered by his old PAT being rejected. New PAT needed at github.com/settings/tokens (must have `repo` scope), then RESET CREDENTIALS on the login page (after merging) or `rm config/credentials.enc` and re-launch.
2. **Merge `claude/fix-flask-display-issue-1yxlq` into `main`** from Chris's local machine.
3. **Rescan + regenerate summaries** once the new PAT is in place.
4. **Port April/May features to Netlify** — auth-error handling, Stats (with group filter + rollup), What's Next, groups persistence, recursive search, Size column, refreshed nav, recent-updated sort, business-spec aliasing, unassigned-projects list.
5. **Parallelize initial scan** using `ThreadPoolExecutor` (same pattern as `/stats`).
6. **Verify Anthropic key at login** the same way GitHub PAT is verified, so bad keys are caught up front.

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
