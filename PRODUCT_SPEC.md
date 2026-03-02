# RepoDoctor2 — Product Specification

**Version:** 5.0 | **Date:** 2026-03-02 | **Repo:** github.com/christreadaway/repodoctor2

---

## 1. Product Overview

RepoDoctor2 is a Flask web application that gives developers a single-screen view of every GitHub repository they own, with branch counts, required file status, AI-powered branch analysis, and Claude conversation mapping. It's built for people who use Claude Code across multiple projects and need to keep their repos clean, documented, and organized.

The retro 1980s terminal aesthetic is intentional — it signals "developer tool" and keeps the UI distraction-free.

---

## 2. Problem Statement

Developers working across many repos lose track of branch hygiene and project documentation. Stale branches pile up, required files go missing, and there's no single view that answers "which of my repos need attention?" Meanwhile, Claude conversation history lives in a separate export with no connection to the repos those conversations were about.

RepoDoctor v1 proved the concept but was tightly coupled and hard to extend. RepoDoctor2 is a ground-up rebuild with modular architecture, better onboarding, model selection, and a conversation-to-repo mapping engine.

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
- Columns: repo name, visibility (public/private), created date, updated date, branch count, 6 required file indicators, file score
- Summary stats bar: total repos, total branches, repos missing required files
- Expandable branch name lists (click branch count to toggle)
- Color-coded rows: complete repos (all files present) vs. incomplete
- Click-through to repo detail page
- Column sorting (text and numeric) via inline JS

### 4.3 Required File Checks

**Files:** `github_client.py` (`check_required_files`, `get_root_files`)

Six files are checked per repo, with flexible matching (case-insensitive, any extension):

| Required File | Purpose |
|---|---|
| `CLAUDE.md` | Claude Code project instructions |
| `LICENSE` | Open source license |
| `BUSINESS_SPEC.md` | Business context and problem statement |
| `PRODUCT_SPEC.md` | Feature inventory and technical spec |
| `PROJECT_STATUS.md` | Current state snapshot |
| `SESSION_NOTES.md` | Session-by-session development log |

Matching is stem-based: `business_spec.pdf`, `BUSINESS_SPEC.md`, `Business_Spec.txt` all count. A single API call fetches the root directory listing, replacing 6 individual file checks (~83% fewer API requests).

### 4.4 Repository Detail View

**Files:** `app.py` (repo_detail route), `templates/repo_detail.html`

- Repo header: full name, visibility badge, default branch, branch count, description
- Required files status grid with Y/- indicators per file
- Spec file content panels: displays the actual contents of BUSINESS_SPEC, PRODUCT_SPEC, PROJECT_STATUS, and SESSION_NOTES pulled from the repo (truncated at 10,000 chars)
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
| User preferences | `config/preferences.json` | Permanent |
| Encrypted credentials | `config/credentials.enc` | Until reset |
| Scan history | `data/scan_history.json` | Last 50 scans |
| Analysis cache | `data/analysis_cache.json` | Permanent (keyed by commit SHA) |
| Action log | `data/action_log.json` | Permanent |
| Product specs | `data/specs/[repo].md` | Permanent |
| Conversation mappings | `config.json` | Permanent |
| Parsed conversations | `projects/conversations.json` | Until re-import |

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

**Files:** `static/css/style.css` (2,367 lines), `templates/base.html`

- **Aesthetic:** 1980s CRT phosphor-green terminal — dark backgrounds (#080a08), bright green text (#33ff33), monospace font (IBM Plex Mono)
- **Layout:** Top navigation bar + single-column content area + footer with cost display
- **Status colors:** Green (safe/present), amber (warning/pending), red (danger/missing), cyan (info)
- **Components:** Sortable tables, collapsible panels, badge system, file status indicators, flash messages
- **Interactions:** Copy-to-clipboard with feedback, expandable branch lists, keyboard shortcuts (Ctrl+K for search)
- **Responsive:** Fluid table widths with horizontal scroll on narrow screens

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
| GET | `/` | Active | Dashboard — repo table with branch counts and file status |
| POST | `/scan` | Active | Scan all GitHub repos (lightweight mode) |
| GET | `/repo/<owner>/<name>` | Active | Repo detail with spec contents and branch list |
| GET/POST | `/settings` | Active | Preferences, specs, credential management |
| GET | `/api/session-cost` | Active | JSON: token counts and cost for current session |
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
| Tests | pytest (43 tests) |
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

**Files:** `tests/test_app.py` (437 lines, 43 tests)

Coverage areas:
- Security: encryption/decryption roundtrips, wrong password rejection, credential file management
- Models: preferences CRUD, scan history, analysis caching, action logging, spec management, cost tracking
- GitHub: branch classification logic for all 5 categories
- AI: token estimation, cost estimation per model, prompt building with/without specs
- Flask: auth redirects, dashboard/settings access control, login/logout flows, API endpoints

---

## 11. Current Status (March 2026)

| Area | Status |
|---|---|
| Credential encryption | Working |
| Dashboard (repo table + file checks) | Working |
| Repo detail (spec viewer + branch list) | Working |
| Settings | Working |
| Cost tracking | Working |
| Tests (43/43) | Passing |
| AI branch analysis | Built, inactive |
| Archive management | Built, inactive |
| Action log | Built, inactive |
| Setup guide | Built, inactive |
| Conversation import + mapping | Code complete, not yet wired into routes/UI |

---

## 12. Roadmap

### Near Term
1. Wire `project_mapper.py` into Flask routes and add conversation import UI (drag-and-drop ZIP + mapping review screen)
2. Re-enable commented-out features: AI analysis, archive, action log, setup guide
3. Add 30-minute auto-lock session timeout
4. Build branch detail cards with AI summary display, risk badges, and action buttons

### Medium Term
5. Timeline view merging GitHub events + Claude conversations per repo
6. Next Steps tab with AI-generated prioritized recommendations
7. Batch branch operations (select multiple, merge/delete/archive)
8. Docker deployment option

### Long Term
9. GitHub webhook integration for real-time branch event tracking
10. Team/org support with shared dashboards
11. Scheduled branch cleanup automation
12. GitHub App authentication (replace PAT)
