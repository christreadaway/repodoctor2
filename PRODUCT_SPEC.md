# RepoDoctor2 — Product Specification

**Version:** 9.0 | **Date:** 2026-05-19 | **Repo:** github.com/christreadaway/repodoctor2

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

- Paginated fetch of every repo the user can reach via GitHub REST API v3, sorted by last update — `affiliation=owner,collaborator,organization_member` so personal, collaborator, AND organization repos are all included (the `organization_member` value is required; without it org repos are silently dropped)
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

**Parallel scan (Session 24):** `/scan` reads repos 8 at a time (`_scan_repos_parallel`), returns them in the original order so output is identical to the old one-at-a-time loop, cancels the rest on a GitHub 401 (global handler shows the PAT remedy), keeps the per-repo error row for any other failure, and logs "Scan finished: N repos in X.Xs, E errors" plus elapsed time and error count in the action log. Each row now also carries GitHub's `archived` and `fork` flags (from the repo list; no extra calls). The scan's branch-list and file-tree reads are strict: a rate limit or 5xx makes that repo's error row (shown ungraded, retried next scan) instead of reading as '0 branches' / 'no docs'; In Flight always reads error rows rather than treating them as tidy.

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

Named groups of repos, free-form and many-to-many. A tab bar (`All | Group1 | Group2 …`) scopes a screen to the selected group; a collapsible "Manage Groups" panel handles create / rename / delete and per-group repo assignment via checkboxes.

- Storage: `~/.repodoctor/groups.json` (per-machine, survives re-clone), shape `{group_name: [repo_name, ...]}`
- Preference: `active_group` in `config/preferences.local.json` persists the current filter across navigation
- The four category groups (Church / School / Combined / Personal) can't be renamed or deleted. CREATE refuses a name that already exists (it used to overwrite that group's members). Saving a group keeps members that aren't in the current scan. Renaming a group carries its Program notes and cached program brief over.
- Guards: rename refuses to overwrite an existing target group; empty-name submissions are rejected with a flash; delete prefers the hidden `original_name` field so an in-flight edit can't redirect the target
- Active group lives in prefs and is updated atomically on rename/delete
- **Flows through the whole app (Session 19c):** the group filter tab bar now appears on My Repos, Selector, Next Up, and Docs as well as Projects / Stats / Briefing, all reading the same `active_group` — so picking a group on any screen scopes them all. Shared by `_filter_by_group()`.

### 4.7a′ Project Categories (Church / School / Combined / Personal)

**Files:** `models.py` (`CATEGORY_GROUPS`, `seed_category_groups`, `get_repo_categories`, `set_repo_category`), `app.py` (`/api/categorize`, `inject_preferences`), `templates/projects.html` (Categorize panel), `templates/_macros.html` (`category_badge`)

A single-select category on top of groups: the four categories **are** groups (so they flow through every group filter for free), but assigned one-per-project — "tag a project once." "Combined" is its own value (serves both church and school), so the four are mutually exclusive by design.

- **Seeded** at startup (`seed_category_groups`, idempotent, never clobbers members) so the four show as filters from first launch.
- **Tagged** on the Projects "Categorize" panel: one `<select>` per repo, saved instantly via `POST /api/categorize` (no reload; a brief "saved" flash per row). Assigning a category drops the repo from the other three category groups; non-category groups are untouched.
- **Badged everywhere:** `repo_categories` (name→category) is injected into every template by the context processor; the `category_badge` macro renders a colored badge (four distinct hues) on My Repos, Projects, Selector, and Next Up cards, and the category is written into the Selector's copy/paste output (`Church · Private`).

### 4.7b Stats View — what changed across every repo, any date range

**Files:** `activity.py` (range resolution, fetch, report, Markdown), `app.py` (`/stats`, `/stats/export.md`, `_activity_data` cache), `templates/stats.html`, `github_client.py` (`get_commits_range`, `get_merged_pulls_since`, `_get_paginated_strict`), `models.py` (`log_activity_event`)

**Job:** "Across ALL my repos, what did we update in the last week / month / quarter / YTD / any range I pick?" (Session 23).

**Date range (one picker drives the whole page):**

| Preset | Meaning (whole local days, America/Chicago, inclusive) |
|---|---|
| Week | Last 7 days including today |
| Month | Last 30 days including today |
| Quarter | Last 90 days including today |
| YTD | Jan 1 through today |
| Custom | Any From / To pair (`?range=custom&from=YYYY-MM-DD&to=YYYY-MM-DD`) |

Bad custom input never errors: reversed dates swap, a future end stops at today, a pre-2008 start clamps to Jan 1 2008 (GitHub's launch), and an unreadable date falls back to Week. Each adjustment is said in a notice on the page and logged (`range_adjusted`). The caption shows the exact dates, day count, and when the data was read.

**What it counts:** commits that landed on each repo's default branch, plus pull requests merged in the range (any base). Merge commits are kept out of commit totals (their branch commits are usually counted already), but a merge that lands in range still marks the repo updated, since its branch work may predate the range or have no PR. Unmerged branch work belongs to Next Up → In flight.

**Page layout:** range chips + Custom → group filter (keeps the range) → KPI tiles (repos updated of total, commits, PRs merged, active days of days in range) → timeline (commits per day ≤31 days, per week ≤182, per month ≤2 years, per quarter ≤6 years, per year beyond, so it never passes ~31 columns on a phone; hover shows a bucket's commits + PRs, and the plot is one tab stop with arrow / Home / End keys between buckets) → three views:

| View | What it shows |
|---|---|
| **What changed** (default) | One card per updated repo, busiest first: counts, last update, active days, merged PR titles (6 shown, rest behind a disclosure), commits on the default branch (40 shown, then "+N more on GitHub" to GitHub's own filtered history). Repos with nothing in range collapse into "Not updated in this range (N)". |
| **Commits** | The bar chart, now for the selected range; By repo / By group rollup (repos in no group roll up as "Ungrouped"). |
| **Code Size** | Bytes from `/languages` (not range-dependent). |

The chosen view is remembered per browser (localStorage, wrapped so a blocked store falls back to What changed). COPY FOR CLAUDE and DOWNLOAD .MD produce the same Markdown (`what-changed-<from>-to-<to>.md`): totals, per repo merged PRs and up to 50 commits (then a link), the idle list, and an "Incomplete" line naming repos GitHub couldn't read.

**Fetching + cache:** one parallel read (8 workers) of every scanned repo back 90 days (`activity.default_floor`) serves Week, Month and Quarter (and YTD from January to March) as local filters. YTD later in the year, or a custom range that runs through today, widens that read once. A custom range that ENDS in the past and starts before it gets its own bounded read (`since` → `until`, kept in a 6-slot LRU), so a busy repo's newer history can't crowd the old range out of a capped read. Caches are in memory, keyed by the scan snapshot the request started with, expire hourly, and are dropped on every scan; REFRESH re-reads and redirects back carrying only `range`/`from`/`to`/`group`, so a browser reload doesn't refetch. Fetches are serialized by a lock so two tabs don't double the API calls.

**Failures are never shown as "no activity":** pagination is strict, so a rate limit, 5xx, 403 or 404 on any page marks that repo as unreadable (the message names the real cause: rate limit vs. SAML/access block vs. not found vs. GitHub error), the page names it in a warning, totals leave it out, KPI denominators read "of N read", and the empty state says "No activity found in the repos GitHub could read" instead of "Nothing landed". A failed repo is retried on a later view after a 5-minute backoff (a widening read carries the failure instead of hitting the repo again; REFRESH always re-reads). An empty repo (409) is simply idle. Caps: 5,000 commits and 1,000 closed PRs per repo per read; when a read hits a cap it records how far back it IS complete, and only ranges that start before that point are marked partial (banner + "Only partly read" list, never "Not updated"). A repo in the list with no read at all is shown as "Not read yet", never idle. A merge commit that landed in range without a PR still counts as an update (shown as "Merges (no PR)"), though not toward the commit total. If the app restarted and lost its GitHub connection, a cached read is still shown, marked offline.

**What we shipped (AI, Session 24):** an optional plain-English paragraph per updated repo for the selected range, plus one overview for the portfolio or active group, for handing a quarter to a board or staff meeting. A "What we shipped" panel above the cards has SUMMARIZE (N) (repos lacking a fresh paragraph, then the overview) and REDO ALL, with a model-aware cost hint (~$0.005 each on Haiku); it runs the shared generation queue and reloads. Prompts use ONLY the repo's in-range merged PR titles, commit subjects and merges, with dates (capped at 150 items, and the prompt says when it was capped); output is structured JSON, 2–4 sentences, outcomes first. Paragraphs are cached in `data/activity_summaries.json` (newest 500 per section) keyed by a CONTENT fingerprint (model + repo + in-range commit SHAs + merged PR numbers, no dates), so Week / Month / Quarter reuse a paragraph when the work is the same; when the work changed, the last paragraph for that repo and range is shown marked STALE until SUMMARIZE rewrites it. Endpoints: `POST /api/stats/summarize/<owner>/<name>`, `POST /api/stats/summarize-portfolio` (JSON body: range/from/to, group, force; 401/404/502/503 JSON errors; nothing cached on failure; cost added to the session total). Both paragraphs flow into COPY FOR CLAUDE / DOWNLOAD .MD and into the Weekly review. Logged as `summarize`, `summarize_portfolio`, `summarize_error`, `summarize_skip` in `activity.log`; the API key is never logged.

**Logging:** every fetch (repos, since, ok/errors, truncated repos, commit/PR totals, elapsed ms), per-repo failure, page view (range, group, cache hit, totals) and export goes to `data/logs/activity.log` (one JSON object per line) and through the root logger to `data/logs/app.log`.

### 4.7c Planned Next Steps (was "What's Next", now part of Next Up)

**Files:** `app.py` (`_next_up_items`, rendered on `/yesterday`), `templates/yesterday.html`

Aggregated next-step bullets across every repo whose project summary has been generated on the Projects page. Each card links to the repo detail view, shows the generation timestamp, and lists up to 5 bullet items, sorted alphabetically by repo name. **Session 19b:** this stopped being its own tab. It and the Yesterday screen were two answers to the same question — "where do I pick back up?" — so the bullets now render as a **Planned next steps** section on the **Next Up** screen (the renamed Yesterday page, `/yesterday`), below the live session reconstruction. The old `/whats-next` route 302-redirects to `/yesterday`.

### 4.7d Code Size Metric

**Files:** `github_client.py` (`get_language_bytes`, `scan_repo_lite`)

During the lightweight scan, each repo also receives a single call to `/repos/{o}/{r}/languages`. The returned `{language: bytes}` map is stored on the repo as `code_size_bytes` (sum) and `languages` (breakdown). This byte total is displayed on the dashboard, in the Stats view, and can back future per-language charts. It is a proxy for "how much code" — not literal line count — and is labeled accordingly.

### 4.7e Codebase Tracker

**Files:** `tracker_data.py`, `tracker_generator.py`, `templates/tracker.html`, `static/js/tracker.js`, `app.py` (tracker routes), `models.py` (tracker storage + event log)

Per-repo deep view that turns each project's docs + code into a structured, navigable map of where the project stands and what to build next. Implemented per `CODEBASE_TRACKER_PRD.md` and adapted from the PRD's repo-agnostic React/TS reference to RepoDoctor's Flask + Jinja2 stack.

**Nine tabs:**

| Tab | What it shows |
|---|---|
| **Overview** | Open P0 next actions, last 3 changes, recommended build & rollout sequences |
| **Recent** | Newest-first changelog with kind chip (shipped / unblocked / doc / fix / blocked) |
| **Next Actions** | Copy-paste-ready Claude Code prompts, filterable by priority + status. Each card carries a status form (dropdown + note + UPDATE) so a user can mark items blocked / dismissed / in-progress / shipped without leaving the page. Open filter excludes shipped + dismissed by default; new filter chips for Blocked-only and Dismissed. |
| **Shipped** | Completed next-actions, kept as a record. Items flow here automatically when a user flips an action to "shipped." |
| **Modules** | Grouped by category, status chip (functional / prototype / visual / missing), with route paths. Each module carries a COPY PROMPT button generating a context-aware Claude Code prompt to advance that module. |
| **Infra Gaps** | Pieces of infrastructure that block ≥2 modules. Each gap carries a COPY PROMPT button. |
| **Features** | Proposed/shipped feature specs with separate build vs. rollout priorities. Each feature carries a COPY PROMPT button. |
| **External Systems** | Third-party deps with mode (Core / Integrate / Replace / Optional). Each system carries a COPY PROMPT button. |
| **Questions** | Open questions the code can't answer |

**Clickable headline stats.** The page header shows Functional / Prototype / Visual / Missing module counts plus P0-open and Infra-gap counts. Each is a button — click it and the tracker jumps to the relevant tab with the matching filter pre-applied (e.g. "9 P0 open" → Next Actions filtered to P0 priority + Open status).

**User-driven next-action statuses (PRD §6 extension).** Six values: `todo`, `in_progress`, `awaiting_deploy`, `shipped`, `blocked`, `dismissed`. The two new ones are user-set only — the AI is instructed to preserve them across regeneration along with the user's `status_note`. Visual treatment: blocked cards get a red left-rule + tinted background; dismissed cards ghost-fade; shipped cards live in their own tab.

**Stable ID system** (PRD §5.1): every row carries a permanent two-character prefix + integer (`M*` module, `I*` infra gap, `F*` feature, `E*` external system, `Q*` question, `N*` next action). IDs never get renumbered — once `M7` exists it stays `M7` across every regeneration. Each prefix has its own monotonic counter; new rows pick `max + 1` even when older rows are deleted.

**AI generation:**

- Per-repo, on-demand. Click GENERATE TRACKER on the toolbar; first generation takes 20-90 seconds depending on repo size.
- **Per-generation model override:** a dropdown next to the GENERATE button lets you pick Haiku / Sonnet / Opus for that one generation without changing the global Settings default. Useful when a dense repo (e.g. parentpoint) needs Sonnet's larger output budget while everything else stays on Haiku for cost.
- Default model comes from the configured `ai_model` preference (Haiku 4.5 default — ~$0.02-$0.05 per generation; Sonnet ~$0.15-$0.30; Opus ~$0.50-$1.00). Surfaced in the toolbar before clicking Generate.
- **Streaming output** (required by the Anthropic SDK for `max_tokens` ≥ ~16K): generation accumulates chunks from `client.messages.stream()` and pulls final usage + stop_reason off `get_final_message()`. Same cost as non-streaming — purely a transport change to keep the HTTP connection alive while large responses generate.
- Inputs: PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md, CLAUDE.md, README (each capped at 6000 chars), file tree (vendor dirs excluded, capped at 200 paths), last 30 days of commit titles (capped at 40), prior tracker if one exists (compact view, capped at 8000 chars), Firestore detection results when the repo uses Firebase.
- **Hard scope caps in the prompt** force the model to be selective on rich repos: max 25 modules, 8 infra_gaps, 12 features, 12 external_systems, 15 questions, 15 next_actions, 20 recent_changes, 8 each in build/rollout sequences. Explicit priority order for when it can't fit everything (P0/P1 actions over P2/P3, non-functional modules over functional, infra gaps blocking more modules over fewer). Prose fields are 1-3 sentences max.
- Regeneration preserves IDs: the prior tracker is passed in with a "LOAD-BEARING IDS that MUST appear in output" directive, plus a compact view of every existing row's id + name + status.
- Output is validated against the §5.5 invariants. Invalid AI output triggers one retry with the validation errors fed back to the model; second failure errors cleanly instead of saving a corrupt tracker.
- **Truncation handling:** if the model hits the 32K output cap (`stop_reason == "max_tokens"`), generation aborts immediately (no retry, since the next call truncates the same way) with a clear error message suggesting Sonnet or a retry. Token counts surface in the error so the user can see how close they got.

**Integrity validation (PRD §5.5 with practical leniency)** — enforced both at save time AND in unit tests:

- Every row ID matches its prefix regex (`/^M\d+$/`, etc.), is unique within its array.
- Every enum value (status, priority, effort, feature status, change kind, external mode, next-action status) is one of the known constants.
- `infra_gap.blocks` and `feature.modules` must point at real module IDs (strict — these have a specific architectural meaning).
- `next_action.related_ids` and `recent_change.related_ids` may point at any in-tracker ID (M/I/F/E/Q/N) — the PRD's M/F/I-only read was too narrow for the model's natural output (Q for "answers question," E for "uses external system" are both reasonable).
- `next_action.depends_on` may reference any row type (e.g., "can't ship N5 until I3 is fixed"), is acyclic on the N→N subgraph, and never self-references.
- `next_action.prompt` is ≥ 50 chars.
- `recent_change.date` matches `YYYY-MM-DD`. Sort order is auto-fixed (newest-first) by `sort_recent_changes()` before save instead of being a hard validation failure — the model occasionally interleaves dates when grouping commits and that's cosmetic, not corruption.
- **Type-guarded AI output:** a non-list value for a list-typed section (rare but possible if the model malforms output) falls back to the empty default and logs a warning rather than crashing the validator. A non-dict top-level response fails the attempt cleanly with a parse error.

**Color-coded chips** (departing from the all-green retro palette):

- Status: green = functional · amber = prototype · sky = visual · red = missing
- Priority: red = P0 · orange = P1 · amber = P2 · slate = P3 · ghost = —
- Tabs use cyan as the active accent so they read as a distinct surface; code-IDs (`M1`, `I3`, etc.) get cyan pill-rectangles for legibility against any chip.

**Storage:** one JSON file per repo at `data/trackers/<owner>__<repo>.json`. Path sanitisation rejects `..` segments and `/` characters. Corrupted JSON falls under the existing `_load_json` recovery path (renames to `.corrupt` and starts fresh).

**Routes:**

- `GET /tracker` — landing; auto-redirects to the most recently generated tracker when one exists, otherwise renders the onboarding screen with a repo dropdown.
- `GET /tracker/<owner>/<name>` — full 8-tab view for one repo.
- `POST /tracker/<owner>/<name>/generate` — run AI generation, save, redirect back to view.
- `GET /tracker/<owner>/<name>/debug` — live integrity check + tracker meta + tail-100 of the event log + Copy-for-Claude-Code formatter.
- `POST /api/tracker/<owner>/<name>/copy-event` — fire-and-forget client beacon logging copy-prompt events.
- `POST /tracker/<owner>/<name>/action/<action_id>/status` — update a single next-action's status + status_note (used by the per-card status form).

**Logging** (per CLAUDE.md mandate): every generation start / done / error, validation pass / fail, render warning, and copy-prompt event lands in `data/logs/tracker.log` as one JSON object per line. The debug surface's "Copy for Claude Code" button formats the tail-100 buffer as a markdown block for paste-back debugging.

**Firestore auto-detection:** when generation runs against a Firebase-using repo, `firestore_detector` is invoked and its output (status / project ID / indicators / missing config) is fed to the prompt with explicit instructions to emit a Firestore row in `external_systems`, an `infra_gaps` row for each missing config item, and a `next_actions` entry with a copy/paste fix prompt. The standalone Firestore fleet view moved from main nav to **Settings → Tools** (still works at `/firestore`).

### 4.7f Repo Selector

**Files:** `app.py` (`/selector`, `/api/selector/blurb/<owner>/<name>`, `_generate_blurb_for_repo`), `templates/selector.html`, `models.py` (selector-blurb storage + event log), `static/css/style.css`

One screen to pick from **every** repo the scan reaches — personal, private, public, and organization repos — and copy a clean summary sheet into another document (a deck, a status update, a board doc).

- **Every repo, tagged.** Each repo is listed with a `PUBLIC` / `PRIVATE` badge and its owner (so an org repo is distinguishable from a personal repo of the same name), sorted newest-push-first. A visibility filter (All / Public / Private) and a name/owner search narrow the list; SELECT ALL / CLEAR act on the visible rows.
- **Three-sentence blurbs.** Tick the repos you want, click **GENERATE SUMMARIES**, and the configured AI model writes a three-sentence plain-English blurb (what it is, who it's for, how finished) for each selected repo that doesn't already have one. Generation reads the repo's spec docs and falls back to the README, so org/public repos without RepoDoctor's spec files still get a real summary. One repo per request via `POST /api/selector/blurb/<owner>/<name>`, driven by the shared client-side queue (live progress, CANCEL, per-repo errors that never overwrite a good blurb). A repo that already has a blurb is skipped unless `force` is sent — no double-billing.
- **Copy / paste out.** An output preview rebuilds live from the current selection with a **Markdown / Plain text** toggle; COPY puts it on the clipboard, DOWNLOAD .MD saves it (client-side Blob, newlines preserved exactly). Selection persists per-device in `localStorage`, so it survives the page reload after generation.
- **Storage:** blurbs cached in `data/selector_blurbs.json`, keyed by `owner/name` (NOT bare name) so an org repo can't collide with a personal one. Every generation start / done / skip / error logs to `data/logs/selector.log` (one JSON object per line, per the CLAUDE.md logging mandate).

### 4.7g In Flight (Next Up → "In flight")

**Files:** `inflight.py`, `app.py` (`/inflight`, `/inflight/refresh`, `/inflight/export.md`), `templates/inflight.html`, `models.py` (`data/inflight.json` cache + `data/logs/inflight.log`)

The window-independent half of "where do I pick back up?": **every unmerged non-default branch and open PR across every repo, no matter how old.** Each Claude Code web session lands in GitHub as a `claude/<slug>` branch, so this list IS the set of coding work that is started but not finished. A session that hasn't pushed yet cannot appear, and the page says so.

- **Deterministic status per branch** (pure function of git facts, no AI): READY TO MERGE (open PR; notes if behind base), FINISH IT (draft PR or WIP-looking last commit), NEEDS PR (fresh commits, no PR), STALE (no PR, no activity for 14+ days), CHECK ON GITHUB (GitHub couldn't compare the branch, so nothing is claimed about it), MERGED (0 ahead of base, OR its PR was squash/rebase-merged and the branch tip is still that PR's head — delete the branch). Sorted by what needs a decision first, then newest activity.
- **Cheap to gather.** When the scan is under a day old, repos whose cached scan shows only the default branch cost zero API calls (an older scan can't vouch for that, so every repo is read). Others cost one branches call + one open-PRs call + one closed-PRs page (squash-merge detection) + one compare per branch. Branches with an open PR are always listed; the compare cap (40) applies only to branches without one, and a banner names any repo where branches were left off. One repo's failure is logged and reported in a banner, never blanks the page. Branch names are URL-encoded in every GitHub call (`fix#12` used to be compared as `fix`).
- **Cached, not live.** Results persist in `data/inflight.json` so the page opens instantly; REFRESH regathers the whole portfolio and the view scopes by group and status filter. COPY FOR CLAUDE / DOWNLOAD .MD produce a grouped "Work in flight" document. Each card links to that repo's Resume packet.
- **Delete merged branches.** MERGED cards (no open PR) get a DELETE BRANCH button, and a DELETE ALL N MERGED bar appears when more than one is in view. Both confirm first. This is the app's only destructive GitHub write, so the server trusts nothing from the cache: each target is re-checked live right before deletion (0 commits ahead of the default branch, not the default branch itself, no open PR for it, repo is in the scan), capped at 50 per click, guarded by the per-boot form token. Refusals are listed with the reason (has commits, open PR, protected, token lacks Contents: write). Deleted branches drop out of the cache; every attempt logs a `delete_merged` event to `data/logs/inflight.log`.

### 4.7h Resume (Next Up → "Resume a project")

**Files:** `resume.py`, `app.py` (`/resume`, `/resume/<owner>/<name>`, `/resume/<owner>/<name>/export.md`), `templates/resume.html`, `models.py` (`data/resume_history.json`, `data/logs/resume.log`)

A **context-switch packet** for one project, built on demand with zero AI cost, so switching campaigns doesn't depend on remembering where each one was. The picker lists every repo (group-scoped, searchable, newest-push-first) with its open-branch count from the In Flight cache and a "recently resumed" row. One project's packet composes:

1. What it is (cached AI brief → project summary → selector blurb, whichever exists), stage, last push, groups, languages, docs present/missing.
2. **Work in flight** for this repo (live).
3. **Recent sessions** (last 14 days, live, via `sessions.gather_sessions`).
4. The **top of PROJECT_STATUS.md** (cut at a section boundary) and the **latest SESSION_NOTES.md entry** (first `## ` section), read from the default branch; README fallback when neither exists.
5. Open tracker actions (P0 first) and planned next steps from the project summary.
6. A **suggested first prompt for Claude Code**, chosen deterministically from the most pressing fact (unfinished branch → PR ready to merge → P0/P1 tracker action → first planned step → "summarize and propose").

COPY FOR CLAUDE CODE / DOWNLOAD .MD hand over the whole packet as Markdown. Each open is recorded in `data/resume_history.json` so the picker can say when you last opened a project.

### 4.7i Components Inventory

**Files:** `components.py`, `app.py` (`/components`, `/components/scan`, `/components/export.md`), `templates/components.html`, `static/css/components.css`, `models.py` (`data/components.json`, `data/logs/components.log`), `COMPONENTIZATION_PLAN.md`

Step one of componentizing shared infrastructure: **which repo has which reusable capability, and which repo is the best source to extract it from.** Twenty-five capabilities (auth, Firebase/Firestore, database, email, SMS, payments, API clients, AI integration, logging, testing, security, deployment, Claude docs, data viz, marketing/GTM, design system, notifications/webhooks, scheduling, PDF export, scripts/launchers, plus video production, audio & transcription, courseware, CRM & contacts, privacy & PII redaction added in Session 22) grouped into eleven buckets matching the owner's component list. Detection is **path-based only** (one git-tree call per repo, vendor dirs skipped; no file contents are read) and the page says so. Each capability card names the best source (most matching files, most recent push on ties) with evidence paths, every other repo that has it, and a GAP marker when none does; a "Suggested extraction order" ranks by repo count × spread. Cached in `data/components.json`; COPY FOR CLAUDE / DOWNLOAD .MD export the inventory. The architecture and sequencing for the actual extraction live in `COMPONENTIZATION_PLAN.md`.

### 4.7j Portfolio (top-level tab: By theme · Infrastructure · Components scan)

**Files:** `portfolio.py`, `app.py` (`/portfolio`, `/api/portfolio/tag`, `/portfolio/tags`, `/api/portfolio/suggest`, `/portfolio/suggestions`, `/portfolio/infra-home`, `/portfolio/export.md`), `templates/portfolio.html`, `models.py` (`tag_repo`, `create_tag`, `data/portfolio_suggestions.json`, `~/.repodoctor/infra_homes.json`, `data/logs/portfolio.log`)

Every project at a glance, grouped by theme, built for context switching.

- **Tags ARE groups.** One data model: a tag picked here scopes every other screen through the shared group filter. Each theme section links straight to that theme's In flight, Briefing, Resume and Recent sessions views. A project can carry several tags; Church / School / Combined / Personal stay single-select categories (adding one replaces the other).
- **Default themes** (Investing / Personal Finance, Marketing / GTM, School, Church, Infrastructure) are created once on first open; a deleted default never comes back (`portfolio_seeded` preference).
- **Per project:** name (links to its Resume packet), public/private, open-branch count from the In Flight cache, a one-line summary (cheapest cached source first: Selector blurb → Projects summary → GitHub description), last push, how finished it is, and its tag chips (× removes, + TAG adds an existing or new tag; saved instantly via `/api/portfolio/tag`). SUMMARIZE N MISSING writes Selector blurbs for projects with no one-liner (same per-repo queue as the Selector).
- **Tag manager:** create, rename, delete (categories are fixed). Rename carries Program notes/briefs over.
- **SUGGEST TAGS:** one AI call over the untagged projects (or RE-SUGGEST ALL) proposes 1-2 tags each, preferring existing tags and creating a new one only when 3+ projects share a theme. Suggestions are saved for review, never applied automatically: tick/untick, APPLY TICKED or DISMISS ALL. Only suggested repo::tag pairs can be applied (forged pairs are ignored). A failed AI call saves nothing.
- **Infrastructure tab:** one card per functional area (marketing, video production, security, messaging, BI/data viz, courseware, plus audio/transcription, AI integration, sign-in & data, CRM & contacts, privacy & PII redaction, payments, logging, testing, Claude docs, deploy/launchers/scheduling, documents & PDF). Each card holds a **home repo** you assign (stored in `~/.repodoctor/infra_homes.json`) and the top 3 source repos from the last Components scan, or a GAP marker.
- **Components** moved under this tab's sub-nav (nav stays at ten tabs); it gained Video Production, Audio & Transcription, Courseware, CRM & Contacts and Privacy & PII Redaction capabilities (25 total).
- COPY FOR CLAUDE / DOWNLOAD .MD export the grouped portfolio plus the infrastructure map. Every tag change, suggestion run and home assignment logs to `data/logs/portfolio.log`.

### 4.7k Do next (Next Up → "Do next", the Next Up landing page)

**Files:** `nextup.py` (`build_do_next`, `compose_do_next_markdown`), `app.py` (`/next`, `/next/export.md`), `templates/next.html`, `_macros.html` (`do_next_item`)

**Job:** one ranked answer to "what should I work on?" across every repo (Session 24). Before, it was split across In Flight, Tracker, Projects and Recent sessions.

**Sources (no GitHub or AI call on page view):** the In Flight cache (`data/inflight.json`), saved trackers (P0/P1 next actions), and Projects summaries (planned steps). The page shows when branches were last read and has REFRESH FROM GITHUB, which re-reads In Flight and returns to this page.

**Ranking (deterministic):**

| # | Tier | Source | Order within tier |
|---|---|---|---|
| 1 | MERGE | In Flight: PR open and ready | Oldest waiting first (PRs rot) |
| 2 | FINISH | In Flight: draft PR or WIP commits | Most recent first (context is warm) |
| 3 | CONFIRM LIVE | Tracker action `awaiting_deploy` | By repo |
| 4 | P0 | Tracker P0 (todo / in progress) | By repo |
| 5 | OPEN A PR | In Flight: commits, no PR | Most recent first |
| 6 | P1 | Tracker P1 (todo / in progress) | By repo |
| 7 | DECIDE | In Flight: stale or couldn't compare | Most recent first |
| 8 | PLANNED | First planned step, only for repos with nothing above (a BLOCKED item doesn't count); max 12; never for forks | Most recently pushed repo first |
| 9 | CLEAN UP | Merged branches not deleted: ONE row for all repos → In Flight | — |
| 10 | BLOCKED | Tracker P0/P1 `blocked`, with its note | By repo |

P2/P3 tracker actions are counted, not listed (except `awaiting_deploy`, which is always CONFIRM LIVE). Archived repos are left out entirely. When the last In Flight read couldn't reach some repos, a warning names them and the empty state never claims everything is merged. Each row: tier chip, repo (links to its Resume packet), title, one-line reason, links (PR / branch, OPEN SESSION when the Claude Code session is known, Tracker), and COPY PROMPT (the tracker's own prompt when it has one, otherwise a generated opening line for a fresh Claude Code session). Group filter, tier chips that jump to each section, a Monday banner pointing to the Weekly review, COPY FOR CLAUDE / DOWNLOAD .MD. Views and exports log to `data/logs/nextup.log`.

### 4.7l Weekly review (Next Up → "Weekly review")

**Files:** `nextup.py` (`build_review`, `gone_quiet`, `compose_review_markdown`), `app.py` (`/review`, `/review/export.md`), `templates/review.html`

**Job:** the Monday look back + look ahead, one screen, group-scoped.

| Section | What it shows | Source |
|---|---|---|
| Shipped this week | KPI tiles + per repo: commits / PRs merged and up to 3 titles | Stats activity report, last 7 days (same 90-day cached read as Stats) |
| Waiting on you | MERGE, FINISH, CONFIRM LIVE rows | Do next |
| Gone quiet | Repos active in the last 90 days with nothing for 21+ days; stale branches | Stats activity report (quarter) + In Flight cache |
| Next up | Top 6 of P0 / OPEN A PR / P1 / PLANNED, with prompts | Do next |

Works offline: if the app lost its GitHub connection and there's no cached read, Shipped and Gone quiet say so and the rest still renders. COPY FOR CLAUDE / DOWNLOAD .MD (`weekly-review-<date>.md`). Logged to `data/logs/nextup.log`.

### 4.7m Health grade (My Repos column + Portfolio chip)

**Files:** `health.py`, `app.py` (`_health_report`, `dashboard()`, Portfolio data), `portfolio.py`, `templates/dashboard.html`, `templates/portfolio.html`, `_macros.html` (`health_breakdown`), `models.py` (`log_health_event`)

One letter per repo so neglected projects surface themselves, computed only from data the app already has (no API calls on page view):

| Check | Points | Rule |
|---|---|---|
| Required docs (the 5 dashboard files) | 30 | 6 each; all 5 OK, 3–4 WARN, 0–2 FIX |
| Docs current | 15 | The scan's `docs_updated` (same as the "Current?" column) |
| Branches tidy | 25 | With In Flight data that has seen every branch the scan lists (and didn't cap the repo): STALE + merged-not-deleted branches (0 → 25, 1 → 15, 2–3 → 8, 4+ → 0); otherwise the extra-branch count from the scan (0–2 → 25, 3–5 → 15, 6+ → 5) with a 'refresh In flight' note. Branches in progress cost nothing |
| Recently active | 30 | Last push: ≤30 days 30, ≤90 20, ≤180 10, older 0 |

A 90+, B 80+, C 70+, D 60+, F below. Checks the scan can't answer are left out and the score is rescaled (under 50 scoreable points → not graded). Not graded ("—", with the reason): scan error, archived or forked on GitHub (flags carried by the scan since Session 24), tagged "Archived", empty repo. The breakdown opens by click, tap or keyboard, lists every check with points and a plain-English fix, and names the single fix that lifts the grade most. My Repos gets a sortable Health column (lowest first, ungraded last) and a "Health D or F" tile. Each My Repos render logs the grade distribution to `data/logs/health.log`; grading can never take the page down.

### 4.7n OPEN SESSION — back into the Claude Code session

**Files:** `sessions.py` (`claude_session_url`, `safe_session_url`), `inflight.py`, `resume.py`, `nextup.py`, `_macros.html` (`open_session`), In Flight / Recent sessions / Resume / Do next / Weekly review templates

Commits made by Claude Code carry a `Claude-Session: https://claude.ai/code/session_…` trailer. Every In Flight item and Recent-sessions card gets `session_url` (newest commit with a link wins, across all commits before truncation; falls back to the PR body) and `session_count`; the Resume packet picks the most relevant one and shows it at the top, and finish / needs-PR first prompts mention it. A cyan OPEN SESSION ↗ chip (new tab, `rel=noopener`) appears wherever a link is known, and Markdown exports include it. The parser only ever returns `^https://claude\.ai/code/session_[A-Za-z0-9_-]{1,128}$`; values read back from caches are re-checked with `safe_session_url` before reaching an href. Link counts are logged in the existing In Flight / sessions / resume events.

### 4.7o Phone snapshot (Settings → Phone snapshot; Netlify)

**Files:** `snapshot.py` (render, encrypt, passphrase rules, shell, site files, zip), `templates/snapshot/` (body, parts, styles), `netlify_publish.py` (Netlify API client), `qr.py` (dependency-free QR code as inline SVG), `app.py` (`# --- Phone snapshot (Netlify)`: `_snapshot_data`, `/snapshot/setup`, `/snapshot/publish`, `/snapshot/recreate`, `/snapshot/check`, `/snapshot/unlock`, `/snapshot/preview`, `/snapshot/takedown`), `security.py` (`encrypt_snapshot_secrets` / `decrypt_snapshot_secrets`), `models.py` (snapshot state + log), `templates/settings.html` + `templates/_snapshot_unlock.html`, `templates/review.html` (PUBLISH TO PHONE), `templates/base.html` (snapshot messages)

**Job:** read the dashboard on a phone without putting the app, its tokens, or readable data online (Session 25; option 1 of the Netlify choices).

**What's in it (read-only):** Do next (first 40 items, COPY PROMPT, OPEN SESSION and PR links), Weekly review, What changed for Week / Month / Quarter / YTD (KPIs, overview paragraph, per repo counts + AI paragraph or top titles, 25 repos per range), Portfolio by theme with health grades. It always covers all repos (the group filter on the page you publish from doesn't apply). Links back into the app (Tracker, In Flight) are dropped; they can't work from a phone. Built from the same caches as the pages, with one Stats read widened to cover YTD. The build time (to the second, Central) is the only plaintext.

**Privacy model:** the Mac renders the HTML, pads it to a 32 KB boundary (so the file size doesn't reveal how much changed), then encrypts it with AES-256-GCM using a key from PBKDF2-HMAC-SHA256 (600,000 iterations, 16-byte random salt, 12-byte random nonce, fresh every publish) and the owner's snapshot passphrase. The published files are `index.html` (a lock screen + the ciphertext envelope + one inline script that decrypts with WebCrypto), `_headers` and `robots.txt`; none contains readable data. A hash-pinned Content-Security-Policy (in `_headers` and a meta tag) lets only that one script run; the decrypted HTML is injected without scripts, images or inline handlers, outbound links use `rel="noopener noreferrer"`, and pages send `noindex`, `no-referrer`, `no-store`. The phone trims spaces at the ends of what you type and normalizes it the same way Python does.

**Passphrase rules (`snapshot.check_passphrase`):** the encrypted file is public, so it can be guessed at offline. 12+ characters, no spaces at the ends, no invisible characters, enough variety, not built on a common password / keyboard row / sequence / repeat (they're stripped in a loop and only what's left is scored, 60-bit floor), and never the app password. Four random words or Settings' SUGGEST (4 groups of 4 from an unambiguous alphabet, about 79 bits) pass.

**Remember on this device (phone, opt-in):** stores a non-extractable PBKDF2 key in that browser's IndexedDB (never the passphrase itself; an older version's localStorage copy is migrated and removed). FORGET deletes it and locks the page. PREVIEW on the Mac never offers Remember.

**Netlify flow:** a personal access token is verified, a site with a random 24-character name (no label that says what it holds) is created, and each publish uploads the zip (`POST /sites/{id}/deploys`, `application/zip`) and waits until the deploy is ready (90 s cap). Errors become plain-English messages (bad token, no access, rate limit, Netlify down, deploy failed); the token is scrubbed from every message and log line. Site creation never retries after a gateway error (it may already exist).

**Setup and changes (Settings card):**
- First time: four steps (where to make the token, which expiration to pick), then token + passphrase (or SUGGEST) + app password → SET UP & PUBLISH.
- Afterwards the form says "leave blank to keep": a blank token keeps the saved one (Netlify shows a token only once), a blank passphrase keeps the current one.
- **The phone link stays the same** for a re-run or a new token on the same account (`get_site` with the new token). **A new passphrase gets a new site**, because Netlify keeps every old deploy at its own address. So does a site the new token can't reach, or setup after a credential reset (the old passphrase is unknown).
- **Replacing is create-first:** the new site is created and published, and only then is the old one deleted (with the previous token first, then the new one). If the create fails, nothing changed. If the publish fails, the old site stays queued in `retire` and the card says "Your previous phone link (…) is still online"; the next successful publish deletes it. A delete that fails temporarily (network, 429, 5xx) stays queued; one the token can't do (401/403) names the site and where to delete it in Netlify.

**Status and recovery (Settings card):**
- **Messages show inside the card.** Every snapshot message is flashed with a `snapshot-` category; Settings renders them at the top of the card (where the `#snapshot` redirect lands, with `scroll-margin-top` clearing the sticky nav), and `base.html` skips them there. On other pages (PUBLISH TO PHONE on Weekly review) they show at the top as usual. A setup problem reopens the form (`?snapshot_form=1`).
- **Getting it onto the phone:** the Site line has COPY LINK, and "Open it on your phone" shows a QR code with the three steps. It opens by itself until the first publish and right after a new link (`?snapshot_new=1`).
- **Site deleted on Netlify:** a publish that gets `site_gone` clears the site, keeps `gone_url` and explains it in the card. **CREATE NEW SITE & PUBLISH** makes a new one with the saved token and passphrase, without re-typing anything.
- **Slow deploy:** a publish still processing after 90 s is stored as `pending_deploy` (not an error). The card offers **CHECK**, which asks Netlify how that deploy ended (ready → published, error → reason, still working → try again in a minute).
- **After an app restart:** the card has an app-password **UNLOCK** field (`/snapshot/unlock`), so there's no need to log out and back in. Publish, PREVIEW and TAKE DOWN say which case applies: locked after a restart, cleared by a credential reset, or not set up / taken down.
- **TAKE DOWN** needs the per-boot form token (a stale tab is told nothing was deleted). It deletes the site and any queued old sites, then forgets the token and passphrase.

**Secrets:** the Netlify token and the passphrase are stored only on the Mac, encrypted with the app password in `config/snapshot.enc` (gitignored, atomic writes), unlocked into memory at login (or with UNLOCK in the card) and cleared at logout. A credential reset deletes them (they were encrypted with the old password) and marks the snapshot as needing setup. Non-secret state is in `~/.repodoctor/snapshot.json`: site id, URL, last publish, last error, `pending_deploy`, `retire`, `gone_url`. Everything is logged to `data/logs/snapshot.log` and never a secret or any data text: setup, publish, recreate, check, unlock, preview, takedown, site_retired and Netlify calls.

**Request guard:** every snapshot POST is refused when the Origin is `null` or foreign, or `Sec-Fetch-Site` is anything but same-origin / none. PREVIEW refuses cross-site loads.

**PREVIEW** serves the exact encrypted page from the Mac under the same CSP (one `Content-Type`), to try unlocking before publishing.

### 4.8 Settings & Preferences

**Files:** `app.py` (settings route), `models.py`, `templates/settings.html`

- **Local root path** — where repos are cloned locally (default: `~/claudesync2`)
- **AI model selection** — Haiku/Sonnet/Opus with pricing shown
- **Display mode** — Plain English (default) or Shorthand
- **Excluded repos** — comma-separated list of repos to skip during scan
- **Product spec management** — upload/view specs per repo for AI context
- **Credential reset** — delete encrypted credentials and force re-setup

**Anthropic key check (Session 24):** `ai_analyzer.verify_anthropic_key` makes one models-list call (no tokens), no retries, 2s connect / 2.5s read, hard 5s cap, and returns ok / invalid (empty key or 401) / unverified (timeout, network, 5xx, 429, 403). At login GitHub stays the gate: an invalid key still logs you in but flashes that summaries, briefs and recaps will fail and how to fix it; unverified only logs. Settings → Credentials shows the last result and has **Update Anthropic key** (new key + app password, re-encrypts credentials; text that isn't an `sk-ant-` key is refused, a key Anthropic rejects is not saved, and a key that can't be checked right now is only saved when 'Save even if it can't be checked' is ticked) and **CHECK KEY**. The key is never logged or echoed. "ok" proves the key works, not that the account has credit.

### 4.9 Data Persistence

**Files:** `models.py` (187 lines)

All data stored as local JSON files — no database dependency.

| Data | File | Retention |
|---|---|---|
| User preferences (incl. `active_group`) | `config/preferences.local.json` (gitignored; seeded once from the tracked `config/preferences.json`, which the app no longer writes, so `git pull` in the launchers can't be blocked by it) | Permanent |
| Encrypted credentials | `config/credentials.enc` | Until reset |
| Project groups | `config/groups.json` (gitignored) | Permanent (per-machine) |
| Scan history | `data/scan_history.json` | Last 50 scans |
| Analysis cache | `data/analysis_cache.json` | Permanent (keyed by commit SHA) |
| Action log | `data/action_log.json` | Permanent |
| Product specs | `data/specs/[repo].md` | Permanent |
| Project summaries (powers What's Next) | `data/project_summaries.json` | Regenerated on demand |
| Codebase trackers (per-repo) | `data/trackers/<owner>__<repo>.json` | Permanent; regenerated on demand with ID preservation |
| Tracker event log | `data/logs/tracker.log` | Append-only; one JSON event per line |
| Conversation mappings | `config.json` | Permanent |
| Parsed conversations | `projects/conversations.json` | Until re-import |
| Stats activity cache (commits + merged PRs per repo) | In-memory only (keyed by scan identity) | 1 hour, next scan, or REFRESH |
| Stats activity log | `data/logs/activity.log` | Append-only; one JSON event per line |
| What we shipped paragraphs | `data/activity_summaries.json` | Newest 500 per section; keyed by content fingerprint |
| Phone snapshot secrets (Netlify token + passphrase) | `config/snapshot.enc` (encrypted with the app password) | Until TAKE DOWN or a new setup |
| Phone snapshot state | `~/.repodoctor/snapshot.json` | Site id / URL / last publish / last error / pending deploy / sites waiting to be deleted |
| Phone snapshot log | `data/logs/snapshot.log` | Append-only; one JSON event per line |
| Health log / Do next + Review log | `data/logs/health.log`, `data/logs/nextup.log` | Append-only; one JSON event per line |

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
- **Top nav (9 tabs):** bracketed brand on the left, menu items (My Repos, Projects, Selector, Stats, Next Up, Briefing, Docs, Tracker, Settings) in the center, pulsing user-pill + Logout chip on the right; active link gets a glowing underline. Consolidated from 12 in Session 19b — three tabs were folded into logical parents (see below), which also keeps the nav on one row.
- **Folded destinations** (reached from a parent, not the top nav): **What's Next → Next Up** (the planned next-steps now render as a section on the Yesterday/Next Up screen; `/whats-next` 302-redirects there); **Program → Briefing** (group-scoped rollup reached via a "Program rollup →" link in Briefing's group bar; `/program` still serves the page with a back-to-Briefing breadcrumb); **Mac Setup → Settings** (linked from a "Setup & Running" card in Settings; `/mac-setup` still serves the guide with a back-to-Settings breadcrumb).
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
    +-- tracker_data.py      Codebase Tracker schema + validation
    |       +-- Status / Priority / Effort / Kind enum tables
    |       +-- validate_tracker() (§5.5 integrity invariants)
    |       +-- next_id() (monotonic ID minting per prefix)
    |       +-- *_META visual chip metadata
    |
    +-- tracker_generator.py Codebase Tracker AI pipeline
    |       +-- gather_repo_inputs() (docs + file tree + commits + firestore)
    |       +-- build_user_prompt() (with prior-tracker ID-preservation directive)
    |       +-- generate_tracker() (validation + retry loop)
    |
    +-- firestore_detector.py Firestore / Firebase setup status detector
    |       +-- detect_firestore_status() (deps + config files → status + missing list)
    |
    +-- models.py            Local JSON storage
    |       +-- Preferences, scan history, analysis cache, action log, specs, cost tracking
    |       +-- Tracker storage (data/trackers/<owner>__<repo>.json) + event log (data/logs/tracker.log)
    |
    +-- templates/ (15)      Jinja2 HTML templates (includes templates/tracker.html)
    +-- static/css/          Retro terminal stylesheet (extended with color-coded tracker chips)
    +-- static/js/           Vanilla JS (no frameworks); tracker.js handles tabs / filters / copy-prompt
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
| POST | `/api/projects/summary/<name>` | Active | Generate the AI summary for one repo (client-side queue) |
| GET | `/selector` | Active | Repo Selector — pick any repos (org repos included), tagged public/private |
| POST | `/api/selector/blurb/<owner>/<name>` | Active | Generate one repo's three-sentence blurb (`{force:true}` to regenerate) |
| GET | `/inflight` | Active | In Flight — every unmerged branch / open PR across repos (`?kind=`, `?group=`) |
| GET | `/next` | Active | Do next — one ranked list across repos (`?group=`); the Next Up landing page |
| GET | `/next/export.md` | Active | Do next as Markdown |
| GET | `/review` | Active | Weekly review — shipped / waiting / gone quiet / next up (`?group=`) |
| GET | `/review/export.md` | Active | Weekly review as Markdown |
| POST | `/snapshot/setup` | Active | Phone snapshot: verify the Netlify token, keep or create the site (blank fields keep the saved token / passphrase), save encrypted, publish, then delete a replaced site |
| POST | `/snapshot/publish` | Active | Re-render, re-encrypt and upload the phone snapshot (`next` returns to the page it was pressed on) |
| POST | `/snapshot/recreate` | Active | Site deleted on Netlify: make a new one with the saved token + passphrase and publish |
| POST | `/snapshot/check` | Active | Ask Netlify how a publish that outlasted the 90 s wait ended |
| POST | `/snapshot/unlock` | Active | Unlock the snapshot secrets with the app password after a restart |
| GET | `/snapshot/preview` | Active | The exact encrypted phone page, served locally under its CSP |
| POST | `/snapshot/takedown` | Active | Delete the Netlify site (and any queued old site) and forget the snapshot secrets (needs the per-boot form token) |
| POST | `/api/stats/summarize/<owner>/<name>` | Active | Write / refresh one repo's "What we shipped" paragraph for a range (JSON) |
| POST | `/api/stats/summarize-portfolio` | Active | Write / refresh the range's overview paragraph (JSON) |
| POST | `/inflight/refresh` | Active | Regather In Flight from GitHub and cache it |
| GET | `/inflight/export.md` | Active | In Flight as a Markdown download |
| GET | `/portfolio` | Active | Portfolio — projects by theme tag (`?tab=infra` for the infrastructure map) |
| POST | `/api/portfolio/tag` | Active | Add/remove one tag on one repo (`{repo, tag, add}`) |
| POST | `/portfolio/tags` | Active | Create / rename / delete a tag |
| POST | `/api/portfolio/suggest` | Active | AI tag suggestions for untagged (or all) repos, saved for review |
| POST | `/portfolio/suggestions` | Active | Apply ticked suggestions or dismiss them |
| POST | `/portfolio/infra-home` | Active | Set the home repo for an infrastructure area |
| GET | `/portfolio/export.md` | Active | Portfolio + infrastructure map as Markdown |
| POST | `/inflight/delete-merged` | Active | Delete fully merged branches (`target=owner/repo:branch`, repeatable; each re-verified live) |
| GET | `/resume` | Active | Resume picker — every repo with open-branch counts and last-resumed stamps |
| GET | `/resume/<owner>/<name>` | Active | One project's context-switch packet (live, zero AI) |
| GET | `/resume/<owner>/<name>/export.md` | Active | The packet as a Markdown download |
| GET | `/components` | Active | Components inventory (capability × repo matrix, best source, gaps) |
| POST | `/components/scan` | Active | Detect capabilities in every repo from its file tree and cache |
| GET | `/components/export.md` | Active | Inventory as a Markdown download |
| POST | `/projects/groups/save` | Active | Create or rename a group + set its repo membership |
| POST | `/projects/groups/delete` | Active | Delete a group (prefers hidden `original_name` field) |
| POST | `/api/categorize` | Active | Set one project's category (Church/School/Combined/Personal, or empty to clear) |
| GET | `/stats` | Active | What changed across every repo for Week / Month / Quarter / YTD / Custom (`?range=`, `from`, `to`, `group`, `refresh=1`), plus Commits / Code Size charts |
| GET | `/stats/export.md` | Active | The What changed document as a Markdown download (same args as `/stats`) |
| GET | `/whats-next` | Redirect | → `/yesterday` (merged into Next Up; kept so old links don't 404) |
| GET | `/tracker` | Active | Tracker landing — redirects to most recent tracker or shows onboarding + dropdown |
| GET | `/tracker/<owner>/<name>` | Active | Full 8-tab per-repo tracker view |
| POST | `/tracker/<owner>/<name>/generate` | Active | Run AI generation, validate, save |
| GET | `/tracker/<owner>/<name>/debug` | Active | Live integrity check + log tail + Copy-for-Claude-Code |
| POST | `/api/tracker/<owner>/<name>/copy-event` | Active | Client beacon for copy-prompt events |
| POST | `/tracker/<owner>/<name>/action/<id>/status` | Active | Update one next-action's status + status_note |
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
- **Local-only guard (Session 21):** every request whose Host isn't `127.0.0.1` / `localhost` / `[::1]` gets a 403 (blocks DNS rebinding), and every POST whose `Origin` is a different site gets a 403 (blocks cross-site form posts, including from other localhost ports). Credential reset and branch delete also require the per-boot form token. `?next=` after login accepts same-site paths only (backslash, tab and protocol-relative tricks rejected).
- **Rendered Markdown is safe HTML:** `md_render` escapes every line before markup, allow-lists link schemes (http, https, mailto, relative), rejects control characters in URLs, and never restores a code span inside an attribute.
- **Tests never touch real data:** `conftest.py` sets `REPODOCTOR_HOME` to a temp dir, which re-roots `data/`, `config/` and `~/.repodoctor` for the run.

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

## 11. Current Status (May 2026)

| Area | Status |
|---|---|
| Credential encryption | Working |
| Dashboard (repo table + file checks + Size column) | Working |
| Required files (5, recursive: CLAUDE.md, LICENSE, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md) | Working |
| "Current?" doc freshness column | Working |
| Repo detail (spec viewer + branch list) | Working |
| Project Groups (filter + manage) | Working |
| Stats view (What changed for any date range + Commits / Code Size) | Working |
| What's Next aggregated view | Working |
| Codebase Tracker (per-repo, 8 tabs, ID preservation, validation, debug surface) | Working |
| Firestore auto-detection inside tracker generation | Working |
| Refreshed top nav with pulsing user pill | Working |
| Settings | Working |
| Cost tracking | Working |
| Tests (223 total via `pytest`, incl. Session 17 regression suite) | Passing |
| Netlify deployment (Node.js) | Hosted site decommissioned 2026-07-23; code retained in `netlify/` for reference only (not used by the phone snapshot). Local Flask is the supported way to run. |
| Phone snapshot (encrypted, read-only, on Netlify) | Working (Session 25) |
| AI project summaries via Claude Haiku | Working |
| AI branch analysis | Built, inactive |
| Archive management | Built, inactive |
| Action log | Built, inactive |
| Setup guide | Built, inactive |
| Conversation import + mapping | Code complete, not yet wired into routes/UI |

---

## 12. Roadmap

### Near Term
1. Surface tracker `next_actions` (open P0/P1) into the What's Next aggregated view so the single-page inbox includes them alongside the project-summary bullets
2. (Deprioritized — hosted Netlify site decommissioned 2026-07-23) If ever redeployed: port Project Groups + recursive spec search + Stats + What's Next to the Netlify Node.js version (Tracker is Flask-only by design — relies on local JSON storage)
3. Parallelize the initial scan (languages call doubled API traffic; `ThreadPoolExecutor` on `scan_repo_lite` like `/stats` does via `activity.gather`)
4. Wire `project_mapper.py` into Flask routes and add conversation import UI (drag-and-drop ZIP + mapping review screen)
5. Re-enable commented-out features: AI analysis, archive, action log, setup guide
6. Build branch detail cards with AI summary display, risk badges, and action buttons

### Medium Term
7. Tracker: commit option that mirrors the tracker JSON back to the host repo as `docs/tracker.json` so trackers survive a RepoDoctor wipe
8. Tracker: optional cost-preview confirm step before generation (token estimate via existing `ai_analyzer.estimate_cost`)
9. Per-language bar-chart breakdown on the Stats page (we already fetch `languages` during scan)
10. Quick "group assign" toggle directly on each project card (instead of only in the Manage Groups panel)
11. Timeline view merging GitHub events + Claude conversations per repo
12. Batch branch operations (select multiple, merge/delete/archive)
13. Docker deployment option

### Long Term
14. GitHub webhook integration for real-time branch event tracking
15. Team/org support with shared dashboards
16. Scheduled branch cleanup automation
17. GitHub App authentication (replace PAT)
