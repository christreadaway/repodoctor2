# RepoDoctor2 — Project Status

**Last Updated:** 2026-10-26 (Session 22 — Portfolio: projects by theme tag, editable tags with AI suggestions, infrastructure map)
**Current Branch:** `main` (Sessions 20-22 pushed to `main`; also on `claude/confident-wright-gfnscy`)
**Overall Progress:** ~97%

---

## Snapshot

RepoDoctor2 is a Flask web app that visualizes all your GitHub repos: branch counts, required-spec-file presence, code size, AI-generated project summaries, named groups for filtering, a Stats view with Commits / Code Size bar charts across time periods (aggregatable by group), an aggregated What's Next view, a per-repo Codebase Tracker, a Chat Briefing screen that composes a single portfolio document for pasting into a Claude chat session, a **Yesterday** screen that reconstructs what you were doing across every repo (broken out by session, with a where-you-left-off call), a **Docs Explorer** for reading each repo's Markdown files in the browser, a **Repo Selector** for ticking any repos (personal, private, public, and organization repos) and copying a clean three-sentence summary sheet into another document, an **In Flight** view of every unmerged branch and open PR across all repos, a **Resume** packet per project for context switching, and a **Components** inventory of reusable infrastructure across repos. **Runs locally as a Flask app — this is the supported way to use it.** A Node.js/Netlify serverless port still lives in `netlify/` for reference, but the hosted deployment was decommissioned on 2026-07-23.

---

## Latest — Session 22 (2026-10-26)

- **Portfolio (new tab).** Every project grouped by theme tag, with a one-line summary, open branches and last push, for fast context switching. Each theme links to its In flight, Briefing, Resume and Recent sessions views. Tags are the app's groups, so a theme picked here filters every screen.
- **Editable tags.** Add/remove tags per project with chips, create / rename / delete tags, and SUGGEST TAGS (one AI call) proposes themes that you review and tick before anything changes. Default themes: Investing / Personal Finance, Marketing / GTM, School, Church, Infrastructure.
- **Infrastructure map.** One card per functional area (your list: marketing, video, security, messaging, BI, courseware; plus audio, AI, sign-in & data, CRM, privacy/PII, payments, logging, testing, Claude docs, deploy, documents), each with a home repo you pick and the best sources from the Components scan. `COMPONENTIZATION_PLAN.md` has the area table with likely source repos.
- **Components** moved under Portfolio's sub-nav (nav stays at ten tabs) and gained 5 capabilities (25 total).
- **Netlify:** asked and deferred ("not now"). The local app stays the supported way to run it.
- **Tests: 485 passing** (+15). Portfolio and Infrastructure verified in Chromium at 390px and 1280px, including a real tag add / new tag / remove round trip.

---

## Session 21 (2026-10-25)

Built the one item Session 20 held for a yes (delete merged branches), then ran a four-way bug sweep across the whole app and fixed every finding that reproduced. See `SESSION_NOTES.md` for the full list.

- **Delete merged branches from In Flight.** DELETE BRANCH on MERGED cards and a DELETE ALL N MERGED bar. Each target is re-checked live right before deletion (nothing left to merge, no open PR, not the default branch), capped at 50 per click, token-guarded, logged.
- **Critical:** stored XSS in the Docs Markdown renderer (a crafted `.md` in any readable repo could run script on page load). Fixed with a scheme allow-list and placeholder hardening; 20 regression tests.
- **High:** open redirect after login (`/\evil.com`, tab tricks); unencoded branch names (`fix#12` compared as `fix` — dangerous with branch delete); Mac launcher killing the browser on relaunch (`lsof` matched client sockets); launcher never reinstalling new requirements; tracked `config/preferences.json` rewritten by the app, which made `git pull` in the launchers fail; tests writing fake errors into the real `data/logs/app.log`; console logging silent; Yesterday dropping repos when the scan was older than the window; a refused/truncated AI response overwriting a good brief and counting as fresh; DNS-rebinding / cross-site POST exposure (now a Host + Origin guard).
- **Medium/low:** In Flight cap hiding PR branches, squash merges never MERGED, failed compares reported as "0 commits", tidy-repo skip never firing; Components/Firestore/Docs treating a dead token or rate limit as "no files" and overwriting good caches; `public_repo` passing the `repo` scope check; CREATE GROUP overwriting a group; group saves dropping unscanned members; transient read errors renaming good JSON to `.corrupt`; tracker regeneration undoing status clicks; Selector/Resume search hiding nothing (CSS `[hidden]`); copy buttons stuck on COPIED!; tracker pages overflowing on phones; plus ~25 more (see notes).
- **Tests: 470 passing** (was 395). Every screen re-crawled in Chromium at 390px and 1200px with XSS fixture data: no console errors, no horizontal scroll.

### Known limitations (not fixed this session)
- Briefs, summaries, groups and categories are keyed by bare repo name, so a personal repo and an org repo with the same name share one entry. In Flight, Resume and Docs now key by owner/name; the rest is a storage migration for a later session.
- Logout is still a GET and other POSTs rely on the new Origin check rather than a per-form token.

---

## Session 20 (2026-10-25)

Came back after two months. Verified the state (327 tests green, branch equal to `main`), then built the three things the coming weeks need. Nav stays at ten tabs: the three "pick back up" screens share the **Next Up** tab via a sub-nav. See `SESSION_NOTES.md` for the full log.

- **In Flight (`/inflight`, Next Up → In flight).** Every unmerged branch and open PR across every repo, regardless of age — the live list of coding you've kicked off that isn't finished. Deterministic status per branch: READY TO MERGE / FINISH IT / NEEDS PR / STALE / MERGED (delete). Cached in `data/inflight.json` (REFRESH regathers; tidy repos cost zero API calls), group + status filters, COPY FOR CLAUDE / DOWNLOAD .MD, each card links to the repo's Resume packet. Logged to `data/logs/inflight.log`.
- **Resume (`/resume`, Next Up → Resume a project).** Pick a project, get one page + one paste with: what it is, stage, open branches, last 14 days of sessions, top of PROJECT_STATUS.md, latest SESSION_NOTES.md entry, open tracker actions, planned next steps, and a suggested first prompt for Claude Code. Live from GitHub, zero AI cost. Picker shows open-branch counts and "recently resumed". Logged to `data/logs/resume.log`.
- **Components (`/components`, new tab).** Path-based detection of 20 reusable capabilities across 9 buckets (your list: marketing/GTM, design, BI/data viz, testing, security, Claude docs, communications, APIs, platform/ops) in every repo — best extraction source with evidence, who else has it, gaps, suggested extraction order. Companion doc: `COMPONENTIZATION_PLAN.md` (recommended architecture: Claude Code plugin for knowledge/process components, packages for runnable code, a thin template repo; first three to extract: Claude docs management, logging, testing).
- **App-wide log.** Everything Python logs now also goes to `data/logs/app.log` (rotating, 2 MB × 3). Settings → Debug log shows the tail with COPY FOR CLAUDE CODE.
- **Usability fixes.** Reconnect banner now links to the login page with a return-to-this-page `?next=` (no logout, scan data kept); stale-scan banner with RE-SCAN NOW once the scan is 2+ days old; mobile nav no longer stacks the tabs in a column beside the logo; `btn-outline` finally has a style (app.js emitted it with no CSS rule).
- **Tests: 395 passing** (was 327: +28 components, +40 inflight/resume/logging/routes). Every screen rendered in Chromium at desktop and phone widths with fixture data — no console errors, no horizontal scroll.

---

## Latest — Session 19 (2026-08-21)

One new screen plus the scan fix that makes it complete. See `SESSION_NOTES.md` for the full log.

- **Repo Selector (`/selector`, nav: Selector).** One place to pick from **every** repo you can reach and copy a clean summary sheet into another document. Each repo is tagged `PUBLIC`/`PRIVATE`, sorted newest-push-first, with a visibility filter (All / Public / Private) and a name/owner search. Tick the projects you want; **GENERATE SUMMARIES** writes a **three-sentence** plain-English blurb (what it is, who it's for, how finished) for any selected repo that doesn't have one — one repo per request via `POST /api/selector/blurb/<owner>/<name>`, driven by the shared client-side queue (live progress, CANCEL, per-repo errors that never overwrite a good blurb). The output preview rebuilds live from the current selection with a **Markdown / Plain text** toggle; **COPY** and **DOWNLOAD .MD** hand you the sheet. Selection is remembered per-device in `localStorage`, so it survives the reload after generation. Blurbs cached in `data/selector_blurbs.json` (keyed by `owner/name`, not bare name, so an org repo can't collide with a personal one); every generation logs to `data/logs/selector.log`.
- **Org repos now included in the scan.** `github_client.get_repos()` was requesting `affiliation=owner,collaborator`, which silently dropped every repo living under an organization. Changed to `owner,collaborator,organization_member` so organization repos show up everywhere — dashboard, Projects, Stats, Briefing, and the new Selector.
- **Tests:** 320 passing (was 313 → +7: Selector route/model/API paths + org-affiliation coverage in `tests/test_app.py`).

**Session 19b — top-nav consolidation (12 → 9 tabs).** Trimmed the nav to nine by folding three tabs into logical parents, each still reachable and old URLs preserved:
- **What's Next → Next Up.** What's Next (planned next-steps from Projects summaries) and Yesterday were two answers to "where do I pick back up?" — merged. The Yesterday screen is renamed **Next Up** and gains a **Planned next steps** section below the live session reconstruction. `/whats-next` 302-redirects to `/yesterday`; `whats_next.html` removed.
- **Program → Briefing.** Program is Briefing scoped to one group; reached now via a "Program rollup →" link in Briefing's group bar. `/program` still serves its page (with a back-to-Briefing breadcrumb); data untouched.
- **Mac Setup → Settings.** Linked from a new "Setup & Running" card in Settings (same pattern as the Firestore fleet view). `/mac-setup` still serves the guide with a back-to-Settings breadcrumb.
- Nav now: My Repos · Projects · Selector · Stats · Next Up · Briefing · Docs · Tracker · Settings. **Tests: 318 passing** (replaced 6 What's-Next group-filter tests with 4 Next-Up merge tests).

**Session 19c — categories + groups flow through the whole app.**
- **Reconnect banner.** After an app restart the session cookie survives but the in-memory GitHub client doesn't, so pages rendered but actions 401'd with a confusing "not connected" error. Now a global amber banner (from a `github_connected` context flag) warns on every page to log out and back in. (+2 tests.)
- **Project categories (Church / School / Combined / Personal).** A single-select tag per project, implemented as four seeded groups so they reuse the group machinery. Tagged one-dropdown-per-repo on the Projects "Categorize" panel (`POST /api/categorize`, instant save), single-select enforced. A colored `category_badge` (four hues) flows onto My Repos, Projects, Selector, and Next Up via a context-injected `repo_categories` map, and into the Selector copy output.
- **Groups flow through everywhere.** The group filter tab bar now also appears on My Repos, Selector, Next Up, and Docs (was Projects / Stats / Briefing only), all sharing `active_group` via `_filter_by_group()` — pick a group on any screen and every screen scopes to it.
- **Tests: 327 passing** (+7 category model/API/badge/filter, +2 reconnect banner).

---

## Latest — Session 18 (2026-07-29)

Two new screens, both driven by the same insight: every Claude Code web session shows up in GitHub as a `claude/<slug>` branch, so branches + PR state are enough to reconstruct "what was I doing." See `SESSION_NOTES.md` for the full log.

- **Yesterday (`/yesterday`, nav: Yesterday).** "What was I doing yesterday, and where did I leave off?" Reconstructs the day's work across every repo from GitHub commits + PR state, **broken out by session** (one card per branch). Each card shows the topic, the commits (with local times), PR status, and a deterministic **what's-next** call — MERGE TO MAIN / CONTINUE / FINISH IT / DONE / DECIDE / CONFIRM LIVE. Range presets (Yesterday / Today / Last 3 / Last 7) + a date picker. On demand, a per-session plain-English AI recap (what you did, how you directed Claude — inferred from the commit arc, honestly hedged since no transcript is read, and what's next), cached in `data/session_recaps.json` and regenerated only when the branch tip moves. **COPY FOR CLAUDE** / **DOWNLOAD .MD** produce a paste-ready "where I left off" document. Every gather + generation logs to `data/logs/sessions.log`.
- **Docs Explorer (`/docs`, nav: Docs).** Quick access to the Markdown files in each repo, rendered in the browser. Repo picker + folder-grouped sidebar (root docs first: README, PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, CLAUDE), a RAW toggle, and an ON GITHUB link. Markdown is rendered by a **dependency-free, escape-first renderer** (`md_render.py`) — every line is HTML-escaped before any markup is applied and link schemes are checked, so no raw HTML/`<script>` or `javascript:` link from a repo's docs can reach the browser. No new runtime dependency was added (the Mac launcher installs deps only once, so a new import would break existing installs).
- **Tests:** 308 passing (was 223 → +37 Yesterday, +21 md_render, +16 Docs, plus the pre-existing suite). New: `tests/test_sessions.py`, `tests/test_md_render.py`, `tests/test_docs.py`.

---

## Latest — Session 17 (2026-07-23)

Focused on making local setup foolproof and hardening the app. See `SESSION_NOTES.md` for the full log.

- **Local run, one command.** A brand-new computer and an old one now use the exact same paste (README.md, RUN_LOCAL.md, SETUP_PC/MAC.md, GET_LATEST_PC.md all unified). The old split between "first time" and "every time" was the trap that failed on a fresh PC. Added `README.md` as the front door. `start.ps1` now checks for Git and stops cleanly if `git pull` fails.
- **Security hardening.** Login brute-force throttle + constant-time compare on the Netlify port; Secure/HttpOnly session cookie; SESSION_COOKIE_SAMESITE=Lax on the local app (Safari doesn't default to it); Werkzeug debug mode OFF by default (opt in with `REPODOCTOR_DEBUG=1`); `/logout` global-state wipe gated behind an authenticated session; escaped remaining unescaped template data. Patched 4 Netlify dependency CVEs (Express 4.21 → 4.22.2).
- **Comprehensive bug audit.** A 5-agent line-by-line audit fixed 2 HIGH crash bugs (tracker-validator type crashes; null-project 500 on repo-detail) plus a batch of medium/low robustness issues (network-error handling, lost-update locks, log rotation, null guards, exact per-model pricing). Added `tests/test_bug_fixes.py`.
- **Tests:** 223 passing (was 209 + 14 new regression tests).

---

## What's Working

### Chat Briefing (NEW 2026-06-12 Session 15)
- `/briefing` (nav: Briefing) — one screen summarizing every project comprehensively, with one **COPY FOR CLAUDE CHAT** button producing a single Markdown document (purpose preamble + At-a-Glance table + one section per project, newest-push-first) so a chat session knows where every project stands in one paste. **DOWNLOAD .MD** serves the same document at `/briefing/export.md`; every card also has COPY SECTION.
- Per-repo AI **chat briefs** (modeled on parentpoint's CHAT_BRIEFING.md): what it is (business problem first), stack, stage (Idea / Requirements / Building / Testing / Live / Paused + evidence sentence), where we are, what's built (by audience), what's left (sequenced), open decisions (owner), and constraints a chat session must respect. Generated from each repo's docs (recursive lookup) + README fallback + compact tracker facts; output normalized (stage enum enforced, bullet caps) before save.
- Merged with hard data per project: last push, branches, docs X/5 (+ missing list), size, languages, groups, tracker open actions (P0-first, with status notes) and open questions. Repos without briefs still export via Projects-summary/description fallback, marked "No AI brief yet."
- **Smart regeneration:** default generation skips repos not pushed-to since their brief (`STALE` badge when out of date); REGENERATE ALL forces the group in view. Briefs cached in `data/briefs.json`; every generation logged to `data/logs/briefing.log` (same one-JSON-per-line format as `tracker.log`).
- **Incremental generation (15b):** one repo per request via `POST /api/briefing/generate/<name>` driven by a client-side queue — live "3 of 12" progress overlay, CANCEL, per-repo errors that never kill the batch or overwrite a good brief.
- Group tabs filter screen, generation, and export alike.

### Yesterday — session recap (NEW 2026-07-29 Session 18)
- `/yesterday` (nav: Yesterday) — cross-repo answer to "what was I doing, and where did I leave off?" A **session** = one Claude Code branch's activity in a time window, reconstructed from GitHub (there is no access to the Claude Code transcript, so "how you directed Claude" is inferred from the commit messages + PR title and honestly hedged).
- Gathering (`sessions.py`): prunes repos by cached `pushed_at`, then per candidate repo pulls branches + PRs (newest-updated first, stops paging once older than the window) and builds sessions three ways — **merged/closed/open PRs** (commits via the PR-commits endpoint, so a session that merged yesterday still shows its work), **bare branches** with in-window commits (via compare, capped at 40/repo with a logged warning), and **direct-to-default** commits (merge commits dropped, PR-covered shas deduped).
- Deterministic **what's-next** classifier (pure function of the git facts, so the page is useful with zero AI): MERGE TO MAIN (open PR) / CONTINUE / FINISH IT (WIP heuristic on the last commit) / DONE (merged or already in main) / DECIDE (closed-unmerged PR) / CONFIRM LIVE (direct-to-main).
- Per-session AI recap on demand (client-side queue, one POST per session via `POST /api/yesterday/summarize/<token>`), cached in `data/session_recaps.json` keyed `repo::branch::window`, stale when the branch tip sha changes. Range presets + date picker; **COPY FOR CLAUDE** / **DOWNLOAD .MD** at `/yesterday/export.md`. Every gather + generation logged to `data/logs/sessions.log`.
- 48 unit tests (`tests/test_sessions.py`): window resolution, topic/token parsing, the classifier, commit-window filtering, mocked cross-repo gather (all three session kinds + dedup + prune + failure isolation), recap staleness, Markdown, and the routes.

### Docs Explorer (NEW 2026-07-29 Session 18)
- `/docs` (nav: Docs) — read the Markdown files in any repo, rendered in the browser. Repo picker + folder-grouped sidebar (root docs first, in the order README / PRODUCT_SPEC / PROJECT_STATUS / SESSION_NOTES / CLAUDE / PROJECT_RULES), a RAW/RENDERED toggle, and an ON GITHUB link. One tree call lists the `.md` files (vendor dirs excluded); one content call fetches the selected file. A crafted `?path=` is rejected unless it's in the repo's own md listing (verified: never fetched).
- Rendering (`md_render.py`) is **dependency-free and XSS-safe by construction**: every line is HTML-escaped BEFORE any Markdown token is applied, and link targets are scheme-checked (http/https/mailto/relative only), so no raw HTML/`<script>` or `javascript:`/`data:` link from a repo's docs can render. Handles headings, fenced/inline code, bold/italic, links, nested lists, blockquotes, tables, and horizontal rules; anything unrecognized stays plain escaped text. No new runtime dependency (the Mac launcher stamps deps as installed once, so an added import would break existing installs).
- 37 unit tests (`tests/test_md_render.py` safety + blocks + inline + nesting + robustness; `tests/test_docs.py` listing/grouping + routes + path-validation).

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
- **Production-hardening on Session 14b (2026-05-19):** parentpoint's dense docs surfaced two real-world bugs after the initial merge — (1) Haiku 4.5 output was truncated at the 8K-then-16K cap, (2) the Anthropic SDK refuses non-streaming requests at high `max_tokens`. Fixed by bumping the output budget to 32K, switching to the streaming API (`client.messages.stream`), adding hard scope caps to the prompt (max 15 next_actions, max 25 modules, etc.), detecting `stop_reason == "max_tokens"` and failing fast with a clear message instead of silently truncating, and adding a per-generation model override dropdown on the toolbar so a dense repo can flip to Sonnet for that one generation without changing the global Settings default.
- **Validation leniency on Session 14c (2026-05-19):** the streaming fix exposed that the validator was too strict for what Claude actually emits. Now `next_action.related_ids` accepts any in-tracker ID (was M/F/I-only — Q for "answers question" and E for "uses external system" are now accepted). `next_action.depends_on` accepts any row type (was N-only — N can now depend on an infra gap being fixed or a module being built). Cycle detection still walks only N→N edges. `recent_changes` is auto-sorted newest-first by a new `sort_recent_changes()` helper before save instead of erroring on interleaved dates. Also type-guards AI output: a non-list value for a list-typed section falls back to the empty default and logs a warning rather than crashing the validator. 43 unit tests + 29-check audit pass.
- **Tracker UX work on Session 14d (2026-05-19):** First real-use feedback turned the tracker from read-only into something you can drive work from. (1) Headline stat chips are clickable — each jumps to the matching tab + filter. (2) Two new next-action statuses, `blocked` and `dismissed`, with per-card status form (dropdown + note + UPDATE button) hitting POST `/tracker/<owner>/<name>/action/<id>/status`. Open filter excludes shipped + dismissed; new BLOCKED / DISMISSED filter chips. AI prompt rule 2a tells the model to preserve user-set statuses + status_notes on regeneration. (3) Shipped tab — shipped next-actions move out of Next Actions into their own tab with their own count badge. (4) Per-row Claude Code prompts on Modules / Infra Gaps / Features / External Systems via Jinja macros — every card gets the same COPY PROMPT button Next Actions had. 47 tests pass.

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
- **Henry branches excluded from the count (2026-05-15 Session 13):** per-repo Branches column and cross-repo Total Branches summary use `non_henry_branch_count`. Henry branches still appear in the expandable list, marked faded + italic with a `(henry)` suffix and tooltip explaining the exclusion. (The dedicated Henry tab was removed 2026-08-04; this count-exclusion behavior stays.)

### Projects
- AI-generated summaries via Claude Haiku (~$0.001/project) — generated one repo per request (15b) with live progress, scoped to the group in view; failures report per-repo and never overwrite an existing good summary
- **Structured outputs enforce the JSON shape (NEW 2026-08-04):** every AI feature goes through `ai_analyzer.request_json`, which sends a JSON Schema with the request so the API constrains the model to emit valid JSON. Replaces "ask nicely for JSON, then parse whatever came back" — the model can no longer answer with prose, which is what produced the `No JSON object found in AI response` failures. Truncated responses auto-retry once at double the token budget; refusals and unparseable replies raise an error that quotes what the model actually said
- **Own event log at `data/logs/summaries.log`** — one JSON object per line, copy/paste-able into Claude Code (matches tracker/briefing/sessions logs)
- **Sorted by most recently updated (desc)** by default; missing dates sink to the bottom
- **Project Groups** — tab-bar filter (All / named groups) + collapsible Manage Groups panel for create/rename/delete/assign
- **Unassigned-projects section** at the bottom of Manage Groups — readonly chips listing every repo that isn't a member of any group, or an "Every project belongs to at least one group" empty-state
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
- Netlify serverless port (Node.js + Express + serverless-http) — older feature set; hosted deployment decommissioned 2026-07-23, code retained in `netlify/` for reference
- **Tests:** 223 passing (`pytest`), including `tests/test_bug_fixes.py` (Session 16 regression tests). A few tracker tests skip in envs lacking the `anthropic` SDK.

---

## What's In Progress

Nothing actively in progress. All Session 15 work (Chat Briefing screen + 15b refactors: atomic writes, incremental per-repo generation with progress, shared doc-fetch path, centralized model constants, group-seeding removal) is merged to `main` (2026-06-12).

---

## What's Broken / Known Issues

- **Netlify serverless port is behind Flask and its hosted deployment is decommissioned (2026-07-23).** The code stays in `netlify/` for reference; local Flask is the supported path. Redeploying would mean re-adding env vars in Netlify (and rotating the PAT) plus porting the April/May features listed under Next Steps.
- **Truncated git trees.** Very large repos can have a truncated tree response; a warning is logged and some deep files may be missed.
- **Scan is slightly slower.** `/languages` call per repo doubles API calls vs. previous. Still sequential; parallelization on the backlog.
- **Stats first-load latency.** Commit stats page through up to 5000 commits per repo × N repos — first visit after a scan can take noticeably longer for heavy accounts. `?refresh=1` forces recompute; normal loads are cache hits.
- **Anthropic key not verified at login.** An invalid/empty Anthropic key only surfaces when the user generates summaries or briefs, where it shows up as a per-repo error in the progress overlay. Flagged in audit; low-frequency since the key rarely changes after first setup.
- ~~`/login/reset` has no CSRF protection~~ — FIXED: both credential-reset endpoints require a per-boot anti-CSRF token, `/logout`'s global-state wipe is gated behind an authenticated session, and the local session cookie is now `SameSite=Lax` + `HttpOnly` (Session 16).
- **Known limitations left as-is (Session 16 audit).** Documented, low-severity, deferred by design: repo lookups keyed by name alone can collide if two collaborator repos share a name; the Netlify port's in-memory scan state isn't shared across serverless instances (moot while decommissioned); GitHub pagination can silently truncate on a mid-list rate-limit (rare for a personal account). See `SESSION_NOTES.md`.
- ~~`_save_json` has no disk-full / permission error handling~~ — FIXED Session 15b: all JSON/spec/credential writes are atomic (temp file + rename) with errors logged.

---

## Tech Debt — Remove on Next Rebuild

- Nothing currently flagged. (Default group seeding was removed in Session 15b per its own removal note — groups persist at `~/.repodoctor/groups.json`.)

---

## Next Steps (in priority order)

1. **Run locally on each machine — one command.** Paste the block from `README.md` / `RUN_LOCAL.md`: Windows uses `.\start.ps1`, Mac uses `./start.command`. It clones if the repo is missing, otherwise pulls `main` and launches — the same command works on a brand-new computer or an old one.
2. **Generate chat briefs across the portfolio.** Briefing tab → GENERATE BRIEFS, watch the per-repo progress, then COPY FOR CLAUDE CHAT and paste into a Claude chat session.
3. **Try the tracker against a real repo.** Click Tracker in the nav, pick a repo (e.g. parentpoint, catholicevents), click GENERATE TRACKER. Verify the eight tabs populate and the next-actions prompts copy cleanly into Claude Code.
4. **Generate trackers across the portfolio** to seed the load-bearing IDs. Re-generations from this point on will preserve those IDs.
5. **Wire tracker `next_actions` into What's Next** so open P0/P1 actions show up in the simple aggregated inbox alongside the project-summary bullets (Roadmap item 1).
6. **(Deprioritized) Netlify port.** The hosted deployment is decommissioned — only revisit if you decide to redeploy. Would need the April/May features ported (auth-error handling, Stats with group filter + rollup, What's Next, groups persistence, recursive search, Size column, refreshed nav, recent-updated sort, business-spec aliasing, unassigned-projects list) plus env vars re-added in Netlify. Tracker stays Flask-only by design.
7. **Parallelize initial scan** using `ThreadPoolExecutor` (same pattern as `/stats`).
8. **Verify Anthropic key at login** the same way GitHub PAT is verified, so bad keys are caught up front.

---

## Tech Stack

| Layer | Local (Flask) — supported | Netlify port (decommissioned 2026-07-23) |
|---|---|---|
| Backend | Python 3 + Flask | Node.js + Express + serverless-http |
| AI | `anthropic` SDK (Claude Haiku for summaries) | `@anthropic-ai/sdk` |
| GitHub | REST API v3 + `requests` | REST API v3 + `node-fetch` |
| Security | Fernet + PBKDF2; SameSite=Lax + HttpOnly cookie; debug off by default | — (relies on environment) |
| Storage | Local JSON in `config/`, `data/`, and `~/.repodoctor/` | Ephemeral per-invocation |
| Frontend | Jinja2 templates + retro terminal CSS | EJS-style views + same CSS |
| Tests | `pytest` (223 passing) | — |

---

## File Paths / Workflow

- Start: paste the one-command block from `README.md` (or run `./start.command` on Mac / `.\start.ps1` on Windows). Direct: `cd ~/repodoctor2 && python3 app.py` (runs on `http://127.0.0.1:5001`). Set `REPODOCTOR_DEBUG=1` to turn Flask debug mode on (off by default).
- Netlify build artifacts: `netlify_functions/` (gitignored)
- **User data (survives codebase wipes):** `~/.repodoctor/groups.json`
- User data in repo (all gitignored, do not survive a fresh clone): `config/credentials.enc`, `config/groups.json` (legacy), `data/scan_history.json`, `data/analysis_cache.json`, `data/action_log.json`, `data/project_summaries.json`, `data/specs/`
- Session logs: `SESSION_NOTES.md` (append new entries at top)
