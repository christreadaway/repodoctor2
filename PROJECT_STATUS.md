# RepoDoctor2 — Project Status

**Last Updated:** 2026-04-24
**Current Branch:** `claude/add-project-grouping-sq2Hy`
**Overall Progress:** ~88%

---

## Snapshot

RepoDoctor2 is a Flask web app that visualizes all your GitHub repos: branch counts, required-spec-file presence, AI-generated project summaries, and now named groups for filtering the Projects view. It runs locally and is also deployed on Netlify as a Node.js serverless app (Netlify version lags Flask by the current session's work).

---

## What's Working

- **Auth & security:** Fernet + PBKDF2 credential encryption, 30-minute session auto-lock, GitHub PAT scope verification
- **Scan:** parallel repo scan (batches of 10) with branch counts and required-file detection
- **Required files (5):** CLAUDE.md, LICENSE, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md — case-insensitive, any extension, **searched recursively** (root preferred, vendor dirs skipped)
- **Dashboard:** sortable columns, sticky headers, per-file Y/- indicators, per-repo X/5 score, "Current?" freshness column
- **Repo detail:** renders Product Spec, Project Status, Session Notes panels (cleaned markdown), What's Next extraction, conversation timeline from Claude exports
- **Projects page:** AI-generated summaries via Claude Haiku (≈$0.001/project), **group filter tab bar + Manage Groups panel**
- **Activity log** with color-coded messages
- **Netlify deployment** (Node.js + Express + serverless-http) — older feature set
- **Tests:** 60 passing

---

## What's In Progress

Nothing actively in progress. The current session's work (groups + recursive spec search) is pushed on `claude/add-project-grouping-sq2Hy` and ready to merge.

---

## What's Broken / Known Issues

- **Netlify version is behind Flask version.** The recursive-search + groups features from this session have not been ported to `netlify/functions/api/`. Netlify still uses the old root-only file check and has no groups feature.
- **Netlify free tier 10s function timeout.** Large GitHub accounts can still time out even with parallelized scans.
- **Truncated git trees.** On very large repos, the recursive tree response is truncated; a warning is logged and some deep files may be missed.

---

## Next Steps (in priority order)

1. **Merge `claude/add-project-grouping-sq2Hy` to main** — feature-complete, tested, pushed.
2. **Re-scan your repos** and verify:
   - PROJECT_STATUS.md column populates
   - Repos with specs in subfolders (e.g. `docs/PRODUCT_SPEC.md`) are now detected
   - Group filter works as expected
3. **Port groups + recursive search to the Netlify version** (`netlify/functions/api/lib/github-client.js`, `api.js`, dashboard/repo_detail views).
4. **Consider follow-ups:**
   - Allow multiple active groups simultaneously (currently only one filter at a time)
   - Persist groups in Netlify Blobs for the deployed version
   - Add a "group membership" quick-toggle directly on each project card

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

---

## File Paths / Workflow

- Start command: `cd ~/repodoctor2 && python3 app.py` (runs on `http://127.0.0.1:5001`)
- Netlify build artifacts: `netlify_functions/` (gitignored)
- User data locations (all gitignored): `config/credentials.enc`, `config/groups.json`, `data/scan_history.json`, `data/analysis_cache.json`, `data/action_log.json`, `data/project_summaries.json`, `data/specs/`
- Session logs: `SESSION_NOTES.md` (append new entries at top)
