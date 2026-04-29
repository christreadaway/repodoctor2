# RepoDoctor2 — Project Status

**Last Updated:** 2026-04-24
**Current Branch:** `claude/sort-projects-group-stats-yXxvE`
**Overall Progress:** ~93%

---

## Snapshot

RepoDoctor2 is a Flask web app that visualizes all your GitHub repos: branch counts, required-spec-file presence, code size, AI-generated project summaries, named groups for filtering, a Stats view with Commits / Code Size bar charts across time periods (aggregatable by group), and an aggregated What's Next view. Runs locally; also deployed on Netlify as a Node.js serverless app (Netlify lags Flask).

---

## What's Working

### Core
- **Auth & security:** Fernet + PBKDF2 credential encryption, 30-minute session auto-lock, GitHub PAT scope verification
- **Scan:** repo list + branch counts + required-file detection + code size (bytes per language)
- **Required files (5):** CLAUDE.md, LICENSE, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md — case-insensitive, any extension, searched recursively (root preferred, vendor dirs skipped)

### Dashboard (My Repos)
- Sortable columns, sticky headers, per-file Y/- indicators, X/5 score
- Size column (B / KB / MB) — numeric sort honors unit suffixes
- "Current?" freshness column, expandable branch lists

### Projects
- AI-generated summaries via Claude Haiku (~$0.001/project)
- **Sorted by most recently updated (desc)** by default; missing dates sink to the bottom
- **Project Groups** — tab-bar filter (All / named groups) + collapsible Manage Groups panel for create/rename/delete/assign
- **5 default groups seeded on login** (School, Church, Catholic Games, Infrastructure, Fun) — temporary one-shot recovery; removal flagged in `CLAUDE.md`

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
- **Tests:** 72 passing (60 existing + 12 new covering recent-updated sort, stats group filter, default group seeding, legacy groups migration)

---

## What's In Progress

Nothing actively in progress. The 2026-04-24 fixes are committed on `claude/sort-projects-group-stats-yXxvE` and ready for Chris's verification.

---

## What's Broken / Known Issues

- **Netlify version is behind Flask version.** April 24 features (groups, recursive search, Stats, What's Next, Size column, refreshed nav, recent-updated sort, group persistence, group rollup) have not been ported to `netlify/functions/api/`.
- **Netlify free tier 10s function timeout.** Large GitHub accounts can still time out even with parallelized scans.
- **Truncated git trees.** Very large repos can have a truncated tree response; a warning is logged and some deep files may be missed.
- **Scan is slightly slower.** `/languages` call per repo doubles API calls vs. previous. Still sequential; parallelization on the backlog.
- **Stats first-load latency.** Commit stats page through up to 5000 commits per repo × N repos — first visit after a scan can take noticeably longer for heavy accounts. `?refresh=1` forces recompute; normal loads are cache hits.

---

## Tech Debt — Remove on Next Rebuild

- **Default group seeding** (`models.seed_default_groups_if_missing`, `DEFAULT_USER_GROUPS`, and the call from `app._init_session`) was added April 2026 as a one-shot recovery. Groups now persist at `~/.repodoctor/groups.json` — delete the seeding path on the next rebuild. See `CLAUDE.md` → "Tech Debt" section for details.

---

## Next Steps (in priority order)

1. **Verify on local machines.** Chris pulls `claude/sort-projects-group-stats-yXxvE`, logs in, confirms all 5 reported fixes behave on real data.
2. **Merge to main** once verified.
3. **Port April 24 + April 24-bis features to Netlify** — Stats (with group filter + rollup), What's Next, groups persistence, recursive search, Size column, refreshed nav, recent-updated sort.
4. **Parallelize initial scan** using `ThreadPoolExecutor` (same pattern as `/stats`).
5. **Revisit stats caching for heavy accounts** — the 5000-commit page-through can be slow on very active repos; consider a streaming / progressive-render approach.

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
| Tests | `unittest` (72 passing) | — |

---

## File Paths / Workflow

- Start command: `cd ~/repodoctor2 && python3 app.py` (runs on `http://127.0.0.1:5001`)
- Netlify build artifacts: `netlify_functions/` (gitignored)
- **User data (survives codebase wipes):** `~/.repodoctor/groups.json`
- User data in repo (all gitignored, do not survive a fresh clone): `config/credentials.enc`, `config/groups.json` (legacy), `data/scan_history.json`, `data/analysis_cache.json`, `data/action_log.json`, `data/project_summaries.json`, `data/specs/`
- Session logs: `SESSION_NOTES.md` (append new entries at top)
- Pull instructions for PC + Mac: bottom of `SESSION_NOTES.md`
