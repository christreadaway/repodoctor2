# RepoDoctor2 — Project Status

**Last Updated:** 2026-04-24
**Current Branch:** `claude/add-project-grouping-sq2Hy`
**Overall Progress:** ~92%

---

## Snapshot

RepoDoctor2 is a Flask web app that visualizes all your GitHub repos: branch counts, required-spec-file presence, code size, AI-generated project summaries, named groups for filtering, a stats view with commit/size/LOC bar charts across time periods, and an aggregated What's Next view. Runs locally; also deployed on Netlify as a Node.js serverless app (Netlify lags Flask by the April 24 work).

---

## What's Working

### Core
- **Auth & security:** Fernet + PBKDF2 credential encryption, 30-minute session auto-lock, GitHub PAT scope verification
- **Scan:** repo list + branch counts + required-file detection + **code size (bytes per language)**
- **Required files (5):** CLAUDE.md, LICENSE, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md — case-insensitive, any extension, **searched recursively** (root preferred, vendor dirs skipped)

### Dashboard (My Repos)
- Sortable columns, sticky headers, per-file Y/- indicators, X/5 score
- **Size column** (B / KB / MB) — numeric sort honors unit suffixes
- "Current?" freshness column, expandable branch lists

### Projects
- AI-generated summaries via Claude Haiku (~$0.001/project)
- **Project Groups** — tab-bar filter (All / named groups) + collapsible Manage Groups panel for create/rename/delete/assign

### Stats (new)
- Three views: **Commits**, **Code Size**, **Lines Added**
- Period selector: 1d / 3d / 1w / 2w / 1m / 2m (applies to Commits and Lines Added)
- Empty-period repos drop below an alphabetical divider
- Per-scan in-memory cache; `?refresh=1` forces recompute

### What's Next (new)
- Aggregated next-step bullets across every repo with an AI summary
- Per-repo cards sorted alphabetically, linking to repo detail

### Nav (refreshed)
- Sticky top nav, bracketed brand, pulsing green user pill + logout chip on the right
- Active link gets a glowing green underline
- Menu: My Repos · Projects · Stats · What's Next · Mac Setup · Settings

### Other
- Repo detail: Product Spec / Project Status / Session Notes panels, "What's Next" hero, conversation timeline
- Activity log with color-coded messages
- Netlify deployment (Node.js + Express + serverless-http) — older feature set
- **Tests:** 60 passing

---

## What's In Progress

Nothing actively in progress. The 2026-04-24 work is committed on `claude/add-project-grouping-sq2Hy` and ready to merge.

---

## What's Broken / Known Issues

- **Netlify version is behind Flask version.** April 24 features (groups, recursive search, Stats, What's Next, Size column, refreshed nav) have not been ported to `netlify/functions/api/`.
- **Netlify free tier 10s function timeout.** Large GitHub accounts can still time out even with parallelized scans.
- **Truncated git trees.** Very large repos can have a truncated tree response; a warning is logged and some deep files may be missed.
- **Stats cold start.** First `/stats` visit on a cold repo may show 0 for Lines Added because GitHub computes `/stats/code_frequency` asynchronously (202 on first hit). Click **REFRESH** after ~10-20s and data will appear.
- **Scan is slightly slower.** Added `/languages` call per repo doubles API calls vs. previous. Still sequential; parallelization on the backlog.

---

## Next Steps (in priority order)

1. **Merge `claude/add-project-grouping-sq2Hy` to main** — feature-complete, tested (60/60), pushed.
2. **Pull main to both machines** — see `SESSION_NOTES.md` bottom for PowerShell and Terminal commands.
3. **Re-scan** and verify:
   - PROJECT_STATUS.md column populates
   - Repos with specs in subfolders (e.g. `docs/PRODUCT_SPEC.md`) are detected
   - Size column shows bytes across scales (B / KB / MB)
   - `/stats` chart bars render with correct periods
   - `/whats-next` aggregates next-step bullets
4. **Port April 24 features to the Netlify version** — Stats, What's Next, groups, recursive search, Size column, refreshed nav.
5. **Parallelize initial scan** using `ThreadPoolExecutor` (same pattern as `/stats`) — the added `/languages` call makes this more worthwhile.

---

## Tech Stack

| Layer | Local (Flask) | Deployed (Netlify) |
|---|---|---|
| Backend | Python 3 + Flask | Node.js + Express + serverless-http |
| AI | `anthropic` SDK (Claude Haiku for summaries) | `@anthropic-ai/sdk` |
| GitHub | REST API v3 + `requests` | REST API v3 + `node-fetch` |
| Security | Fernet + PBKDF2 | — (relies on environment) |
| Storage | Local JSON in `config/` and `data/` | Ephemeral per-invocation |
| Frontend | Jinja2 templates + retro terminal CSS | EJS-style views + same CSS |
| Tests | `unittest` (60 passing) | — |

---

## File Paths / Workflow

- Start command: `cd ~/repodoctor2 && python3 app.py` (runs on `http://127.0.0.1:5001`)
- Netlify build artifacts: `netlify_functions/` (gitignored)
- User data locations (all gitignored): `config/credentials.enc`, `config/groups.json`, `data/scan_history.json`, `data/analysis_cache.json`, `data/action_log.json`, `data/project_summaries.json`, `data/specs/`
- Session logs: `SESSION_NOTES.md` (append new entries at top)
- Pull instructions for PC + Mac: bottom of `SESSION_NOTES.md`
