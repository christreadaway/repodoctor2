# RepoDoctor2 — Product Specification

**Version:** 8.0 | **Date:** 2026-04-24 | **Repo:** github.com/christreadaway/repodoctor2

---

## 1. Product Overview

RepoDoctor2 is a Flask web application that gives developers a single-screen view of every GitHub repository they own, with branch counts, required file status, AI-powered branch analysis, and Claude conversation mapping. It's built for people who use Claude Code across multiple projects and need to keep their repos clean, documented, and organized.

The retro 1980s terminal aesthetic is intentional — it signals "developer tool" and keeps the UI distraction-free.

---

## 2. Problem Statement

Developers working across many repos lose track of branch hygiene and project documentation. Stale branches pile up, required files go missing, and there's no single view that answers "which of my repos need attention?" Meanwhile, Claude conversation history lives in a separate export with no connection to the repos those conversations were about.

RepoDoctor v1 proved the concept but was tightly coupled and hard to extend. RepoDoctor2 is a ground-up rebuild with modular architecture, better onboarding, model selection, and a conversation-to-repo mapping engine.

---

## 2.1 Business Context

- **Target Users:** Claude Code developers, vibe coders, solo developers, non-technical product builders who need guided onboarding
- **Business Model:** Free / Open Source developer tool
- **Cost-Conscious Design:** Haiku default keeps per-analysis cost at ~$0.003
- **Success Metrics:** Onboarding completion rate, model tier distribution, display mode preference split

---

## 3. Target Users

- **Primary:** Solo developers and vibe coders using Claude Code across multiple repos
- **Secondary:** Team leads who want a branch hygiene dashboard across their org's repos
- **Tertiary:** Non-technical product builders who need plain-English explanations of what branches contain

---

## 4. Core Features

### 4.1 Secure Credential Storage

**Files:** `security.py` (67 lines)

- Fernet symmetric encryption with PBKDF2-HMAC-SHA256 key derivation (480,000 iterations)
- User sets a password on first run; GitHub PAT and Anthropic API key are encrypted at rest
- Credentials decrypted into memory only after correct password entry
- Never written to logs, prompts, or UI
- First-run setup flow with separate screens for initial entry vs. returning-user unlock
- Credential reset available in settings (deletes encrypted file, forces re-setup)

### 4.2 Cross-Repo Dashboard

**Files:** `app.py` (dashboard route), `templates/dashboard.html`

- Sortable table of all user-owned and collaborator repos
- Columns: repo name, visibility (public/private), created, updated, "Current?", branch count, 5 required-file indicators (CLAUDE, LICENSE, SPEC, STATUS, NOTES), file score (X/5), and **Size** (code bytes formatted as B/KB/MB, sortable numerically)
- "Current?" column: checks if PRODUCT_SPEC.md / SESSION_NOTES.md were committed within 7 days of the most recent repo commit — YES (green), NO (red), or — (files don't exist)
- Summary stats bar: total repos, total branches, repos missing required files (dynamically computed from `files_present < files_total`)
- Expandable branch name lists (click branch count to toggle)
- Color-coded rows: complete repos (all files present) vs. incomplete
- Click-through to repo detail page
- Column sorting (text and numeric) via inline JS; numeric sort prefers `data-sort-value` so unit-suffixed values (KB/MB) order correctly

### 4.3 Required File Checks — Recursive

**Files:** `github_client.py` (`check_required_files`, `get_all_file_paths`)

Five files are checked per repo, with flexible matching (case-insensitive, any extension), searched **recursively across the entire repo tree**:

| Required File | Purpose |
|---|---|
| `CLAUDE.md` | Claude Code project instructions |
| `LICENSE` | Open source license |
| `PRODUCT_SPEC.md` | Product specification and business context |
| `PROJECT_STATUS.md` | Current-state snapshot (progress, blockers, next steps) |
| `SESSION_NOTES.md` | Session-by-session development log |

Matching is stem-based: `product_spec.pdf`, `PRODUCT_SPEC.md`, `Product_Spec.txt` all count. A single call to `/git/trees/{ref}?recursive=1` fetches the full tree; root-level matches are preferred, with fallback to the shallowest subfolder path. Vendor/build dirs (`node_modules`, `dist`, `.venv`, `venv`, `env`, `__pycache__`, `target`, `vendor`, `.next`, `.nuxt`, `.cache`, `coverage`, `.tox`, `bower_components`, `site-packages`) are excluded so dependency copies don't masquerade as real specs.

The matcher returns the **actual full path** found, so downstream features (freshness check, spec panels, AI summary generation) reference the real location even when specs live in a `docs/` subfolder.

### 4.4 Repository Detail View

**Files:** `app.py` (repo_detail route), `templates/repo_detail.html`

- Repo header: full name, visibility badge, default branch, branch count, description
- Required files status grid with Y/- indicators per file (5-file set)
- Spec file content panels: displays the actual contents of `PRODUCT_SPEC`, `PROJECT_STATUS`, and `SESSION_NOTES` pulled from the repo (truncated at 10,000 chars), resolving subfolder paths via the recursive matcher
- "What's Next" hero section (extracted from specs + mapped conversations)
- Claude conversations timeline for this repo
- Branch list with default branch marker
- Link to view on GitHub

### 4.5 GitHub Repository Scanner

**Files:** `github_client.py` (367 lines)

- Paginated fetch of all user-owned and collaborator repos via GitHub REST API v3, sorted by last update
- Branch comparison engine: compares every branch against default branch using GitHub's compare API
- Branch classification system:

| Classification | Meaning | Criteria |
|---|---|---|
| `SAFE TO DELETE` | Fully merged | 0 commits ahead of default |
| `AHEAD ONLY` | Has unmerged work, no conflicts | Ahead of default, not behind |
| `DIVERGED` | Has unmerged work AND is behind | Both ahead and behind default |
| `STALE` | Old unmerged work | >30 days since last commit, has unique commits |
| `ACTIVE PR` | Has open pull request | Open PR exists for this branch |

- Lightweight scan mode (`scan_repo_lite`): branch counts + required file checks only — fast dashboard population
- Full scan mode (`scan_repo`): complete branch analysis with commit history, file changes, classification — for deep repo inspection
- Archive tag creation: creates `archive/[branch]/[date]` tags before branch deletion

### 4.6 AI-Powered Branch Analysis

**Files:** `ai_analyzer.py` (157 lines)

- Uses Anthropic Claude API with structured system prompt
- Analyzes branch data and returns JSON with:
  - **Plain English summary** — 2-3 sentences, written for non-developers
  - **Feature assessment** — SHOULD_MERGE / OPTIONAL / OBSOLETE / UNCLEAR
  - **Risk level** — LOW / MEDIUM / HIGH
  - **Conflict prediction** — which files likely conflict and why
  - **Merge strategy** — fast-forward / merge / rebase
  - **Claude Code instructions** — complete, copy/paste-ready terminal commands including cd, branch verification, and rollback steps
  - **Spec alignment** — maps branch changes to product spec features (when spec is provided)
- Model selection with transparent pricing:

| Model | Input Cost | Output Cost | Use Case |
|---|---|---|---|
| Claude Haiku 4.5 (default) | $0.80/M tokens | $4.00/M tokens | Fast, cost-effective (~$0.003/analysis) |
| Claude Sonnet | $3.00/M tokens | $15.00/M tokens | Balanced detail |
| Claude Opus | $15.00/M tokens | $75.00/M tokens | Maximum depth |

- Graceful fallback: if AI response isn't valid JSON, returns structured placeholder with UNCLEAR assessment
- Token estimation for cost preview before running analysis
- Analysis results cached by repo/branch/commit SHA — no re-analysis of unchanged branches

### 4.7 Claude Conversation Import & Mapping

**Files:** `project_mapper.py` (339 lines)

Parses Claude's data export ZIP and maps conversations to GitHub repos. Conversation content is stored locally and never sent to any API.

**Export Parser:**
- Handles Claude's `.zip` data export format
- Supports multiple JSON structures: flat lists, nested conversations, single-conversation files, wrapper objects
- Multi-format date parsing: ISO 8601 with/without milliseconds, with/without timezone
- Extracts: conversation name, project name, date, message count, first user message as excerpt

**Three-Tier Matching Algorithm:**

| Tier | Method | Example |
|---|---|---|
| **Exact match** | Claude project name = GitHub repo name (case-insensitive) | Project "repodoctor2" matches repo "repodoctor2" |
| **Fuzzy match** | Project name contains repo name or vice versa | Project "repodoctor2 updates" matches repo "repodoctor2" |
| **Content match** | Scores conversation topic + excerpt against repo name parts | Topic mentions "repo doctor" → scores 5+ points → suggests match |

Content scoring weights:
- Repo name part found in topic: +3 points
- Repo name part found in excerpt: +2 points
- Full repo name (with hyphens as spaces) in topic: +5 points
- Full repo name in excerpt: +4 points
- Threshold for suggestion: score >= 4

**User Controls:**
- Manual assignment: override any match via dropdown
- Dismiss unmatched: hide irrelevant conversations
- Accept all suggestions: bulk-accept in one click
- Persistent config: mappings and dismissals saved to `config.json`

**Per-Repo Lookup:**
- `get_conversations_for_repo(repo_name)` returns all conversations mapped to a specific repo (manual + auto-matched), sorted by date — used for timeline view

### 4.7a Project Groups

**Files:** `models.py` (groups helpers), `app.py` (groups routes), `templates/projects.html`

Named groups of repos for filtering the Projects page. A tab bar at the top (`All | Group1 | Group2 …`) scopes the summary cards to the selected group; a collapsible "Manage Groups" panel handles create / rename / delete and per-group repo assignment via checkboxes.

- Storage: `config/groups.json` (gitignored — per-machine state), shape `{group_name: [repo_name, ...]}`
- Preference: `active_group` in `preferences.json` persists the current filter across navigation
- Guards: rename refuses to overwrite an existing target group; empty-name submissions are rejected with a flash; delete prefers the hidden `original_name` field so an in-flight edit can't redirect the target
- Active group lives in prefs and is updated atomically on rename/delete

### 4.7b Stats View

**Files:** `app.py` (`/stats`, `_collect_repo_activity`), `templates/stats.html`, `github_client.py` (`get_commits_since`, `get_code_frequency`, `get_language_bytes`)

Three CSS bar-chart views with a period selector (1d / 3d / 1w / 2w / 1m / 2m):

| View | Source | Notes |
|---|---|---|
| **Commits** | `/repos/{o}/{r}/commits?since=...` | Counted by commit age; `N+` suffix if truncated at 200 commits (2 pages × 100) |
| **Code Size** | `/repos/{o}/{r}/languages` byte sums | Sortable static view; no period needed |
| **Lines Added** | `/repos/{o}/{r}/stats/code_frequency` | Weekly buckets with explicit overlap pro-rating so sub-week periods (1d/3d) estimate cleanly |

Repos with zero activity in the selected period drop below a divider and sort alphabetically; active repos sort by count descending. Bars scale to the busiest repo in the period. Data is cached in memory keyed by scan identity; `?refresh=1` forces recompute. Per-repo fetches run in parallel via `ThreadPoolExecutor(max_workers=8)`.

### 4.7c What's Next View

**Files:** `app.py` (`/whats-next`), `templates/whats_next.html`

Aggregated next-step bullets across every repo whose project summary has been generated on the Projects page. Each card links to the repo detail view, shows the generation timestamp, and lists up to 5 bullet items. Cards sort alphabetically by repo name. When no summaries exist yet, the page prompts the user to click **GENERATE SUMMARIES** on the Projects page.

### 4.7d Code Size Metric

**Files:** `github_client.py` (`get_language_bytes`, `scan_repo_lite`)

During the lightweight scan, each repo also receives a single call to `/repos/{o}/{r}/languages`. The returned `{language: bytes}` map is stored on the repo as `code_size_bytes` (sum) and `languages` (breakdown). This byte total is displayed on the dashboard, in the Stats view, and can back future per-language charts. It is a proxy for "how much code" — not literal line count — and is labeled accordingly.

### 4.8 Settings & Preferences

**Files:** `app.py` (settings route), `models.py`, `templates/settings.html`

- **Local root path** — where repos are cloned locally (default: `~/claudesync2`)
- **AI model selection** — Haiku/Sonnet/Opus with pricing shown
- **Display mode** — Plain English (default) or Shorthand
- **Excluded repos** — comma-separated list of repos to skip during scan
- **Product spec management** — upload/view specs per repo for AI context
- **Credential reset** — delete encrypted credentials and force re-setup

### 4.9 Data Persistence

**Files:** `models.py` (187 lines)

All data stored as local JSON files — no database dependency.

| Data | File | Retention |
|---|---|---|
| User preferences (incl. `active_group`) | `config/preferences.json` | Permanent |
| Encrypted credentials | `config/credentials.enc` | Until reset |
| Project groups | `config/groups.json` (gitignored) | Permanent (per-machine) |
| Scan history | `data/scan_history.json` | Last 50 scans |
| Analysis cache | `data/analysis_cache.json` | Permanent (keyed by commit SHA) |
| Action log | `data/action_log.json` | Permanent |
| Product specs | `data/specs/[repo].md` | Permanent |
| Project summaries (powers What's Next) | `data/project_summaries.json` | Regenerated on demand |
| Conversation mappings | `config.json` | Permanent |
| Parsed conversations | `projects/conversations.json` | Until re-import |
| Stats cache | In-memory only (keyed by scan identity) | Until next scan or `?refresh=1` |

### 4.10 Session Cost Tracking

**Files:** `models.py` (SessionCost class), `app.py` (`/api/session-cost`)

- Tracks cumulative input tokens, output tokens, total cost, and analysis count per session
- Displayed in page footer with real-time polling (every 30 seconds)
- Pauses polling when browser tab is hidden (battery-friendly)

### 4.11 Action Logging

**Files:** `models.py` (log_action), `templates/action_log.html`

- Every scan, analysis, merge, delete, archive, and import is logged with timestamp, action type, repo, branch, and details
- Action log page shows time-series table with color-coded badges
- Serves as audit trail for all destructive operations

### 4.12 Archive Management

**Files:** `github_client.py` (create_archive_tag), `app.py` (archive routes), `templates/archive.html`

- Archive a branch by creating a Git tag (`archive/[branch]/[date]`) before deletion
- Tag message includes AI summary (if available) and optional user note
- Reinstate instructions: generates copy/paste terminal commands to restore from archive tag
- Archive browser with search/filter across all archived branches
- Searchable by repo name, branch name, tag name, or summary text

### 4.13 Setup Guide

**Files:** `templates/setup_guide.html`

- CLAUDE.md configuration template with branch management rules
- Claude Chat project instructions
- Claude Code best practices
- Personalized quick-start commands per repo
- CLAUDE.md status checklist across all repos

---

## 5. UI / Visual Design

**Files:** `static/css/style.css`, `templates/base.html`

- **Aesthetic:** 1980s CRT phosphor-green terminal — dark backgrounds (#080a08), bright green text (#33ff33), monospace font (IBM Plex Mono)
- **Layout:** Sticky top navigation bar + single-column content area + footer with cost display
- **Top nav:** bracketed brand on the left, menu items (My Repos, Projects, Stats, What's Next, Mac Setup, Settings) in the center, pulsing user-pill + Logout chip on the right; active link gets a glowing underline
- **Status colors:** Green (safe/present), amber (warning/pending), red (danger/missing), cyan (info)
- **Components:** Sortable tables, group-filter tab bars, CSS bar-chart rows with scaled fills, collapsible panels, badge system, file status indicators, flash messages
- **Interactions:** Copy-to-clipboard with feedback, expandable branch lists, tab-switching period pills, keyboard shortcuts (Ctrl+K for search)
- **Responsive:** Fluid table widths with horizontal scroll on narrow screens; nav collapses nav-links onto a second row below 900px

---

## 6. Architecture

```
Flask Application (app.py)
    |
    +-- security.py          Credential encryption (Fernet + PBKDF2)
    |
    +-- github_client.py     GitHub REST API v3 client
    |       +-- GitHubClient class (token verification, repo/branch/PR/tag operations)
    |       +-- scan_repo_lite() (dashboard: branch counts + file checks)
    |       +-- scan_repo() (detail: full branch analysis with classification)
    |
    +-- ai_analyzer.py       Anthropic Claude API integration
    |       +-- analyze_branch() (structured JSON analysis)
    |       +-- estimate_tokens() / estimate_cost()
    |
    +-- project_mapper.py    Claude conversation import + repo mapping
    |       +-- parse_claude_export() (ZIP parser)
    |       +-- map_conversations_to_repos() (3-tier matching)
    |       +-- assign/dismiss/get_conversations_for_repo()
    |
    +-- models.py            Local JSON storage
    |       +-- Preferences, scan history, analysis cache, action log, specs, cost tracking
    |
    +-- templates/ (8)       Jinja2 HTML templates
    +-- static/css/          Retro terminal stylesheet
    +-- static/js/           Vanilla JS (no frameworks)
```

---

## 7. API Endpoints

| Method | Endpoint | Status | Purpose |
|---|---|---|---|
| GET/POST | `/login` | Active | Authentication (setup + unlock) |
| GET | `/logout` | Active | Clear session and credentials from memory |
| GET | `/` | Active | Dashboard — repo table with branch counts, file status, Size column |
| POST | `/scan` | Active | Scan all GitHub repos (lightweight mode + language bytes) |
| GET | `/repo/<owner>/<name>` | Active | Repo detail with spec contents and branch list |
| GET | `/projects` | Active | Project summaries with group filter (`?group=Name`) |
| POST | `/projects/generate` | Active | Generate AI summaries via Claude Haiku |
| POST | `/projects/groups/save` | Active | Create or rename a group + set its repo membership |
| POST | `/projects/groups/delete` | Active | Delete a group (prefers hidden `original_name` field) |
| GET | `/stats` | Active | Commits / Size / Lines-Added bar charts with period selector |
| GET | `/whats-next` | Active | Aggregated next-steps across all repos |
| GET | `/mac-setup` | Active | Setup instructions page |
| GET/POST | `/settings` | Active | Preferences, specs, credential management |
| GET | `/api/session-cost` | Active | JSON: token counts and cost for current session |
| GET | `/api/debug-files/<owner>/<name>` | Active | JSON: recursive file detection output for debugging |
| POST | `/analyze` | Built (inactive) | AI analysis for a single branch |
| POST | `/estimate` | Built (inactive) | Token/cost estimate before analysis |
| GET | `/archive` | Built (inactive) | Browse archived branches |
| POST | `/archive/create` | Built (inactive) | Create archive tag for a branch |
| POST | `/archive/reinstate-instructions` | Built (inactive) | Generate restore commands |
| GET | `/setup-guide` | Built (inactive) | CLAUDE.md configuration walkthrough |
| GET | `/action-log` | Built (inactive) | Action history timeline |
| POST | `/api/mark-done` | Built (inactive) | Mark branch as handled |
| POST | `/api/toggle-display-mode` | Built (inactive) | Switch plain English / shorthand |

"Built (inactive)" = code complete, commented out in app.py, ready to re-enable.

---

## 8. Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+ / Flask 3.x |
| AI | Anthropic Claude API (Haiku 4.5 default) |
| GitHub | REST API v3 with Personal Access Token |
| Security | Fernet symmetric encryption + PBKDF2-HMAC-SHA256 |
| Frontend | Jinja2 templates + vanilla JS + retro terminal CSS |
| Storage | Local JSON files (no database) |
| Tests | unittest (60 tests) |
| Server | localhost:5001 |

**Dependencies:** flask, requests, anthropic, cryptography, python-dotenv

---

## 9. Data & Privacy

- Credentials encrypted at rest using password-derived key — never stored in plaintext
- GitHub PAT and Anthropic API key never appear in logs, prompts, UI, or git history
- Claude conversation data parsed and stored locally — never sent to any external API
- All storage is local JSON files — no cloud database, no telemetry
- Conversation content stays on disk in `projects/conversations.json`

---

## 10. Testing

**Files:** `tests/test_app.py` (60 tests)

Coverage areas:
- Security: encryption/decryption roundtrips, wrong password rejection, credential file management
- Models: preferences CRUD, scan history, analysis caching, action logging, spec management, cost tracking
- **Groups:** set/get, dedupe + sort, rename (with collision guard), delete, active-group sync on rename/delete
- **Required files (recursive):** 5-file count, names, business-spec exclusion, PROJECT_STATUS presence, all-present score, subfolder match, root-preferred, vendor-dir exclusion
- GitHub: branch classification logic for all 5 categories
- AI: token estimation, cost estimation per model, prompt building with/without specs
- Flask: auth redirects, dashboard/settings access control, login/logout flows, API endpoints

---

## 11. Current Status (April 2026)

| Area | Status |
|---|---|
| Credential encryption | Working |
| Dashboard (repo table + file checks + Size column) | Working |
| Required files (5, recursive: CLAUDE.md, LICENSE, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md) | Working |
| "Current?" doc freshness column | Working |
| Repo detail (spec viewer + branch list) | Working |
| Project Groups (filter + manage) | Working |
| Stats view (Commits / Code Size / Lines Added) | Working |
| What's Next aggregated view | Working |
| Refreshed top nav with pulsing user pill | Working |
| Settings | Working |
| Cost tracking | Working |
| Tests (60/60) | Passing |
| Netlify deployment (Node.js) | Working — behind Flask version (groups, recursive search, stats, whats-next not ported yet) |
| AI project summaries via Claude Haiku | Working |
| AI branch analysis | Built, inactive |
| Archive management | Built, inactive |
| Action log | Built, inactive |
| Setup guide | Built, inactive |
| Conversation import + mapping | Code complete, not yet wired into routes/UI |

---

## 12. Roadmap

### Near Term
1. Port Project Groups + recursive spec search + Stats + What's Next to the Netlify Node.js version
2. Parallelize the initial scan (languages call doubled API traffic; `ThreadPoolExecutor` on `scan_repo_lite` like `/stats` does)
3. Wire `project_mapper.py` into Flask routes and add conversation import UI (drag-and-drop ZIP + mapping review screen)
4. Re-enable commented-out features: AI analysis, archive, action log, setup guide
5. Build branch detail cards with AI summary display, risk badges, and action buttons

### Medium Term
6. Per-language bar-chart breakdown on the Stats page (we already fetch `languages` during scan)
7. Quick "group assign" toggle directly on each project card (instead of only in the Manage Groups panel)
8. Timeline view merging GitHub events + Claude conversations per repo
9. Batch branch operations (select multiple, merge/delete/archive)
10. Docker deployment option

### Long Term
11. GitHub webhook integration for real-time branch event tracking
12. Team/org support with shared dashboards
13. Scheduled branch cleanup automation
14. GitHub App authentication (replace PAT)
