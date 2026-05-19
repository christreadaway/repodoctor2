# Codebase Tracker — Portable PRD

> Drop this file into any repository, point Claude Code at it, and you
> end up with a `/tracker` route that gives you a single, scrollable,
> filterable view of your codebase: every module flagged
> functional / prototype / visual / missing, every infra gap with the
> modules it blocks, every feature spec with build + rollout priority,
> every external system, every open question, and a hardcoded list of
> "next actions" with copy-paste Claude Code prompts.
>
> The PRD is intentionally repo-agnostic. Replace the example rows
> with your own. Everything else (data model, IDs, tabs, filters,
> tests) is portable.

---

## 1. What this is

A single-page "tracker" view at `/tracker` (or any internal route)
that renders a structured inventory of a codebase. It is a
**strawman of the state of the repo** that a builder can keep in front
of them while planning, prioritising, and instructing Claude Code.

Three traits make it useful:

1. **Every row has a stable ID** (`M1`, `F2`, `I1`, `E3`, `Q4`, `N5`),
   so the builder, Claude Code, and any collaborator can reference items
   precisely in conversation.
2. **It is hardcoded TypeScript, not parsed at runtime.** A markdown
   source of truth (optional) sits next to it, but the rendered view
   reads a single typed module so the page boots instantly with zero
   network calls and the type system catches drift.
3. **Every "next action" carries a ready-to-paste Claude Code prompt.**
   Click "Copy prompt", paste into Claude Code, ship the unit of work.

It is **not** a project management tool, a ticket queue, or a CMS.
There is no auth, no write path, no multi-user state. It is a
read-only orientation map maintained by editing a single TS file.

### The codes — what each prefix means

Every row in the tracker carries a stable two-character prefix plus
an integer. The prefix tells you what kind of thing the row is; the
integer is a permanent handle you can reference forever. Once `M7`
exists, it stays `M7` even after the underlying module is renamed,
re-categorised, or split into pieces.

The whole point of the codes is **shared vocabulary**. Instead of
saying "the calendar tagging feature, the one with the admin toggle,
you know, the one we talked about Tuesday," you say "F1." Claude
Code understands the same shorthand because every Next Action prompt
references the rows it touches by ID. Three people in three time
zones can run the same conversation.

**`M*` — Module.** A first-class surface in your app. Usually one
route (`/admin/settings`), one domain (Lunch Ordering, Faculty
Directory, Announcements), or one cohesive area of functionality. If
you'd describe it to a new hire as "here's where the X stuff lives,"
it's a module. Every module has a `status` (functional / prototype /
visual / missing) so you can see at a glance how complete it is.

**`I*` — Infra gap.** A piece of *infrastructure* that's missing or
not wired, where the absence blocks more than one module. Examples:
"Cloud Functions aren't deployed yet," "Stripe key isn't in env,"
"webhook endpoint not configured," "Firestore rules not redeployed."
If exactly one module is blocked, the gap belongs in that module's
`notes` rather than as its own row. Infra gaps are usually the
**highest-leverage** things to fix — flipping one `I*` to resolved
can flip 5-10 modules from `prototype` to `functional` overnight.

**`F*` — Feature spec.** A new feature with a written-up spec doc
under `docs/specs/` (or wherever your specs live). Different from a
Module: a Module is *what exists today*, an `F*` is *what you're
considering building or have recently shipped*. Each feature carries
two priorities — `buildPriority` (how urgent to ship?) and
`rollPriority` (how urgent to *lead with* in launch / sales?) — and
the two can disagree. A foundational feature might be P0 to build
but P3 to market.

**`E*` — External system.** A third-party service the app depends
on. Auth providers (Google, Microsoft), email senders (Postmark,
SendGrid), payment processors (Stripe), AI APIs (Anthropic, OpenAI),
calendar feeds (Google Calendar ICS), analytics, monitoring. Each
`E*` has a mode — `Core` (not replaceable), `Integrate` (third party
owns the surface, you configure it), `Optional` (off by default),
`Replace` (planned migration off this).

**`Q*` — Open question.** Something the codebase *can't* answer —
needs a human decision. "Which Stripe Connect model do we use, per-
school or platform-account?" "When does the beta allowlist flip back
to a Firestore-backed list?" "What does 'done' look like for the
next 30 days?" These live in the tracker so they don't get lost in
a Slack thread or scribbled on a notebook margin.

**`N*` — Next action.** A concrete unit of work, scoped tight to a
single `M*` / `F*` / `I*`, with a **copy-paste Claude Code prompt**
attached. Think "one Claude Code session can ship this." Each Next
Action references the rows it touches via `relatedIds`, and can
declare an order via `dependsOn` (`N5` can't start until `N3`
ships). This is where the tracker stops being descriptive and
becomes a launchpad.

#### Reading a code in context

When you see "F1 (M7, M8) — P0 build, P0 roll, shipped 2026-05-16"
you should be able to parse it instantly:

- `F1` — feature spec #1
- `(M7, M8)` — touches the two modules listed
- `P0 build` — top-priority to ship
- `P0 roll` — and top-priority to lead with at launch
- `shipped 2026-05-16` — went live on that date

That density is the point. The codes pay for themselves the third
time you reference one in a conversation.

#### How to add a new row

1. Find the highest existing number in the relevant array (`M58`).
2. Add `M59` for the new module. **Never reuse a deleted number.**
3. If the new row replaces or supersedes an older one, keep the old
   row in place but flip its status (and note the successor by ID in
   the notes field) so historical references don't break.

---

## 2. Who it's for

The repo owner / lead builder who wants:

- A "what's the state of every module right now" at-a-glance view.
- A way to hand Claude Code a self-contained next-action prompt
  without re-explaining context.
- A reference-able vocabulary (IDs + status chips) shared between
  builder, Claude Code, and any collaborators.

Secondary audience: a new contributor or AI session that needs to
get oriented in 90 seconds.

---

## 3. User stories / jobs to be done

- **Get oriented.** "I haven't looked at this repo in a week — what's
  shipped, what's stuck, what's next?"
- **Plan the next move.** "Show me every P0 that's still open."
- **Hand work to Claude Code.** "Copy the prompt for `N5`, paste,
  go."
- **Reference rows in conversation.** "Reorder `F2` ahead of `F3` and
  bump `I1` to P0."
- **See what changed.** "What landed in the last week, with links
  back to the rows it touched."
- **Catch silent drift.** "If a feature references a module ID I
  later renamed, fail CI."

---

## 4. Core features

The page is a single React component split into eight tabs. Tab
order matters — Overview first so a cold reader lands on context.

### 4.1 Tabs

| Tab | Purpose |
|---|---|
| **Overview** | "What this is" + the 3 most recent changes + P0 right now + recommended build sequence + rollout sequence. Two CTAs jump to Next Actions and Recent. |
| **Recent** | Living changelog feed, newest first, grouped by date. Each entry has a kind chip (`shipped` / `unblocked` / `doc` / `fix` / `blocked`) and links to the IDs it touched. |
| **Next Actions** | Hardcoded units of work with copy-paste Claude Code prompts. Filter by priority and status. Default view hides shipped items. |
| **Modules** | Grouped, filterable inventory of every module in the codebase. Search + status filter + priority filter. |
| **Infra Gaps** | The 1-N pieces of infrastructure blocking multiple modules. Each gap shows the modules it blocks. |
| **Features** | Feature specs with build priority + rollout priority. Status chip (Proposed / In Discussion / In Progress / Shipped / Abandoned). |
| **External Systems** | Third-party dependencies (auth provider, payment processor, AI API, etc.) with mode (`Core`, `Integrate`, `Replace`) and migration notes. |
| **Questions** | Open questions the codebase can't answer, grouped by topic. Stable Q-IDs so they can be referenced. |

### 4.2 Visual conventions

- **Status chips** — `Functional` (✅ green), `Prototype` (🚧 amber),
  `Visual-only` (🎨 sky), `Missing` (❌ rose).
- **Priority chips** — `P0` (rose bg, white text), `P1` (orange), `P2`
  (amber), `P3` (slate), `—` (slate / no action needed).
- **Feature status chips** — `Shipped` (✅), `In Progress` (🛠),
  `In Discussion` (💬), `Proposed` (📝), `Abandoned` (🚫).
- **Effort chips** — `XS` (< 1 hr), `S` (½ day), `M` (1-2 days),
  `L` (~1 week), `XL` (multi-week).
- **Next-action status chips** — `Todo`, `In progress`,
  `Awaiting deploy`, `Shipped`. Shipped cards render with strike-
  through title + 75 % opacity so the list stays scannable.

### 4.3 Interactions

- **Header** — counts of modules per status (`Functional 23`, etc.).
- **Tab nav** — sticky, click to switch. Each tab label shows a count
  badge.
- **Filters** — pill chips on Modules, Next Actions; search input on
  Modules.
- **Copy prompt** button on every Next Action card — copies the full
  prompt to the clipboard, surfaces "Copied!" for 1.5 s.
- **Show / Hide prompt** toggle — expands a `<pre>` block with the
  full prompt body so the builder can review before copying.
- **Cross-tab jumps** — Overview's "Show me what to build next" CTA
  switches to the Next Actions tab; "What changed recently" switches
  to Recent.

### 4.4 Footer

Single small line: data source (e.g.
`docs/CODEBASE_TRACKER.md`), author, last-verified date, branch
at verification. Reinforces "this is a strawman — disagreements are
signal" and reminds the reader to reference rows by ID.

---

## 5. Business rules and logic

### 5.1 ID conventions

See **§1 "The codes — what each prefix means"** for the full
definition of every prefix (`M*`, `F*`, `I*`, `E*`, `Q*`, `N*`).
Quick reference:

| Prefix | Meaning | Example |
|---|---|---|
| `M*` | **Module** — a first-class surface that already exists in the app | `M1`, `M2`, … |
| `F*` | **Feature spec** — something proposed, in progress, or recently shipped, with a spec doc | `F1`, `F2`, … |
| `I*` | **Infra gap** — a missing piece of infrastructure that blocks multiple modules | `I1`, `I2`, … |
| `E*` | **External system** — a third-party service the app depends on | `E1`, `E2`, … |
| `Q*` | **Open question** — a human decision the codebase can't answer | `Q1`, `Q2`, … |
| `N*` | **Next action** — a concrete unit of work with a copy-paste Claude Code prompt | `N1`, `N2`, … |

The hard rules:

- IDs are **monotonically increasing integers**, scoped per prefix
  (so `M1` and `F1` and `I1` coexist).
- **Never renumber.** Once `M7` ships and is referenced in a Recent
  entry, a spec doc, or a PR title, it keeps that ID forever.
- Deleting a row → leave the ID gap. The next new row gets the next
  unused number, not the gap.
- If a row is superseded, **flip its status** and note the successor
  ID in the `notes` field rather than deleting and rewriting history.

### 5.2 Status definitions

- **Functional** — Real data flows end-to-end. Modulo any gaps in
  the row's Notes field.
- **Prototype** — Works against the real backend, but at least one
  link of the chain is gapped (often a Cloud Function or env var).
- **Visual-only** — UI exists, backend not wired.
- **Missing** — Scaffolded / referenced but not implemented.

### 5.3 Priority definitions

- **P0** — Critical. Blocks launch or has a hard external deadline.
- **P1** — Needed before the next major milestone.
- **P2** — Strong value, not blocking.
- **P3** — Strategic or nice-to-have.
- **—** — Already done; no action needed.

`build priority` and `rollout priority` on Features can diverge — a
feature can be P2 to ship but P0 to lead with in sales / launch.

### 5.4 Source of truth

Two valid configurations:

1. **TS-only** (simpler). `trackerData.ts` is the only source of
   truth. The footer points to the file.
2. **Markdown-mirrored** (richer). A markdown file under `docs/`
   carries narrative context; the TS file is a structured mirror. A
   "tracker hygiene" item in `NEXT_ACTIONS` reminds the builder to
   sync after each ship.

Either way, **the rendered page never parses markdown at runtime**.
It reads the typed TS module so first paint is instant.

### 5.5 Data integrity (enforced by tests, not docs)

The following invariants are enforced by unit tests (Vitest or
equivalent). Adding a row that violates one fails CI.

- Every module ID matches `/^M\d+$/`, is unique.
- Every external system ID matches `/^E\d+$/`, is unique.
- Every question ID matches `/^Q\d+$/`, is unique.
- Every module's `status` is one of the 4 known statuses.
- Every module's `priority` is one of the 5 known priorities.
- Every infra gap `blocks: string[]` points at real module IDs.
- Every feature `modules: string[]` points at real module IDs.
- Every feature `spec` path matches `/^docs\/specs\/.+\.md$/`
  (or whichever spec directory you use).
- Every `NEXT_ACTIONS[].relatedIds` entry points at a real
  `M*` / `F*` / `I*`.
- Every `NEXT_ACTIONS[].dependsOn` entry points at a real action ID,
  with no self-references and no cycles.
- Every `NEXT_ACTIONS[].effort` is one of the 5 known efforts.
- Every `NEXT_ACTIONS[].prompt` is non-empty and at least 50 chars.
- Every `RECENT_CHANGES[].date` matches `/^\d{4}-\d{2}-\d{2}$/` and
  entries are sorted most-recent-first.
- Every `RECENT_CHANGES[].kind` is one of the known kinds.
- Every `RECENT_CHANGES[].relatedIds` points at a real
  `M*` / `F*` / `I*` / `E*` / `N*`.

Add custom invariants for any "load-bearing" claim — e.g. "the
biggest infra gap should block ≥ N modules" or "the F1 prerequisite
must always be P0 build + P0 roll."

### 5.6 Next action lifecycle

- New actions enter with `status: undefined` → renders as `Todo`.
- Flip to `in_progress` when work starts; add a `statusNote`
  explaining where it stands.
- Flip to `awaiting_deploy` when code is merged but not yet live.
- Flip to `shipped` and append the ship date + branch name to
  `statusNote`. **Do not delete shipped actions** — they're a
  changelog and a reference for downstream work.

### 5.7 Recent changes feed

- Prepend new entries at the top.
- One entry per discrete delivery — a Phase 1 + a doc + a fix landing
  in the same session = three entries, not one.
- `relatedIds` should always include every M / F / I / E / N that
  changed status because of this ship. The reader uses these as
  jump-links.

---

## 6. Data requirements

A single TS file (recommended path: `src/tracker/trackerData.ts`).
Below is the canonical shape — types first, then required exports.

```ts
// ─── Enums ────────────────────────────────────────────────
export type Status = 'functional' | 'prototype' | 'visual' | 'missing';
export type Priority = 'P0' | 'P1' | 'P2' | 'P3' | '—';
export type FeatureStatus =
  | 'Proposed' | 'In Discussion' | 'In Progress'
  | 'Shipped' | 'Abandoned';
export type NextActionEffort = 'XS' | 'S' | 'M' | 'L' | 'XL';
export type NextActionStatus =
  | 'todo' | 'in_progress' | 'awaiting_deploy' | 'shipped';
export type ChangeKind =
  | 'shipped' | 'unblocked' | 'doc' | 'fix' | 'blocked';

// ─── Visual meta ──────────────────────────────────────────
// Each enum has a *_META map: { label, icon?, chip, description? }.
// chip = Tailwind class string for the pill (bg + text + border).

// ─── Row types ────────────────────────────────────────────
export type Module = {
  id: string;            // 'M1', 'M2', …
  name: string;
  category: string;      // for grouping; freeform but stable
  routes: string[];      // app paths or file paths this row covers
  status: Status;
  priority: Priority;
  notes: string;         // one paragraph; include dates of recent changes
};

export type InfraGap = {
  id: string;            // 'I1'
  name: string;
  blocks: string[];      // M-IDs blocked by this gap
  priority: Priority;
  description: string;   // multi-sentence; current state + what unblocks it
};

export type FeatureIdea = {
  id: string;            // 'F1'
  name: string;
  modules: string[];     // related M-IDs
  buildPriority: Priority;
  rollPriority: Priority;
  take: string;          // 2-4 sentences: value, ease, recommendation
  spec: string;          // 'docs/specs/feature-foo.md'
  status: FeatureStatus;
  builtNote?: string;    // shown when status implies built === true
};

export type ExternalSystem = {
  id: string;            // 'E1'
  name: string;
  what: string;          // what this system does for the app
  mode: string;          // 'Core', 'Integrate', 'Replace', 'Optional'
  migration: string;     // setup / migration notes
};

export type Question = {
  id: string;            // 'Q1'
  group: string;         // 'Roadmap & priorities', etc.
  text: string;
};

export type NextAction = {
  id: string;            // 'N1'
  title: string;
  relatedIds: string[];  // M/F/I IDs this action touches
  why: string;
  effort: NextActionEffort;
  priority: Priority;
  prompt: string;        // full copy-paste Claude Code prompt
  dependsOn?: string[];  // N-IDs that must ship first
  status?: NextActionStatus;
  statusNote?: string;
};

export type RecentChange = {
  date: string;          // 'YYYY-MM-DD'
  title: string;
  kind: ChangeKind;
  relatedIds: string[];  // M/F/I/E/N IDs touched
  description: string;
};

// ─── Required exports ────────────────────────────────────
export const MODULES: Module[] = [...];
export const INFRA_GAPS: InfraGap[] = [...];
export const FEATURES: FeatureIdea[] = [...];
export const EXTERNAL_SYSTEMS: ExternalSystem[] = [...];
export const QUESTIONS: Question[] = [...];
export const NEXT_ACTIONS: NextAction[] = [...];
export const RECENT_CHANGES: RecentChange[] = [...];
export const BUILD_SEQUENCE: string[] = [...];   // ordered narrative
export const ROLLOUT_SEQUENCE: string[] = [...]; // ordered narrative
export const TRACKER_META = {
  author: string,
  lastVerified: string,           // 'YYYY-MM-DD'
  branchAtVerification: string,
};
```

---

## 7. Integrations and dependencies

The tracker has **no runtime external dependencies**. It runs against:

- React + TypeScript (or any equivalent typed component framework).
- A utility-CSS layer for the chips (Tailwind by default; CSS Modules
  or vanilla CSS work — keep the same visual hierarchy).
- A test runner that can run TS unit tests (Vitest, Jest).
- A lazy-import-friendly router so `/tracker` doesn't load on first
  paint of the main app.

It optionally integrates with:

- A markdown source file under `docs/` for narrative-rich tracker
  content. The TS file remains the rendering source.
- A spec template at `docs/specs/_TEMPLATE.md` so every
  `FeatureIdea.spec` path resolves to a real, structured doc.

---

## 8. Out of scope (v1)

- **Write path / editing in the UI.** Edits happen in the TS file,
  committed and pushed.
- **Auth.** The page is internal but unauthenticated; treat the
  contents as if any signed-in employee or contributor could see them.
  Do not put secrets, PII, or anything sensitive in the rows.
- **Multi-user state, comments, threading, voting.** Not this page.
- **Auto-generated rows.** Don't try to parse the codebase at build
  time to populate Modules; the value comes from a human + Claude Code
  curating the list.
- **Cross-repo aggregation.** One tracker per repo. If you want a
  fleet view, build it separately.

---

## 9. Open questions

Replace these with the equivalents for your repo. The point is to
have a Questions tab populated from day one — empty Questions reads
as "everything's clear," which it never is.

- **Q1** — Which environment is "live" vs. "preview"? What's the
  verification path after each push?
- **Q2** — Where do feature specs live, and what's the template?
- **Q3** — Who owns the tracker — one person, or the team?
- **Q4** — How often does the `lastVerified` date get bumped, and by
  whom?

---

## 10. Success criteria

The tracker succeeds when **all six** are true:

1. **Boots instantly.** First paint at `/tracker` is under 500 ms,
   zero network requests. Lazy-loaded so the main app isn't slower.
2. **Survives drift.** The integrity test suite (§5.5) is green in
   CI. A typo in a `relatedIds` entry fails the build, not silent
   404s in the UI.
3. **Hands work to Claude Code in one click.** Copy prompt → paste
   into Claude Code → unit of work proceeds with no re-explaining.
4. **Renders shipped status without losing history.** Old Next
   Actions stay visible (toggled out of the default view) so the page
   doubles as a record.
5. **Updates on a regular cadence.** The `lastVerified` date is
   never older than 14 days during active development.
6. **Reads cold.** A new contributor or AI session lands on the page
   and can describe the state of the repo back to you in two minutes.

---

## 11. Built-in logging and debug surface (required for Claude Code-built specs)

Per the working agreement, any product spec given to Claude Code
must include logging infrastructure that surfaces errors in a
copy-paste-back-to-Claude-Code format. For the tracker:

### 11.1 What to log

- **Tab navigation events** — `tracker:tab_view { tab, ts }`.
- **Filter changes** — `tracker:filter_change { tab, filter, value }`.
- **Copy prompt events** — `tracker:copy_prompt { actionId, ok }`.
- **Clipboard failures** — `tracker:copy_prompt_failed { actionId, err }`.
- **Render errors** — caught by an ErrorBoundary that wraps the
  tracker route and persists to whatever logging sink the host repo
  uses (Firestore, Sentry, or `console.warn` as a no-op default).
- **Data-integrity test failures** — surfaced in CI; the failing
  test name plus the offending row ID goes into the PR check output
  so the builder sees it without rerunning tests locally.

### 11.2 Debug surface

A **`/tracker/debug`** sub-route (or a `?debug=1` query toggle) that
renders:

- `TRACKER_META` (author, lastVerified, branch at verification).
- A live re-run of every data-integrity check from §5.5 — pass / fail
  rows with the offending IDs called out.
- The full session log buffered in `window.__trackerLog` (last
  100 events) with a **"Copy for Claude Code"** button that formats
  the buffer as a markdown block:

  ````
  ```text
  [tracker session 2026-05-19T14:22:01Z]
  - tab_view: overview
  - tab_view: next
  - copy_prompt: N5 ok
  - integrity_fail: NEXT_ACTIONS N12 references unknown M-ID M99
  ```
  ````

- Paste-ready output is the bar — the builder shouldn't have to
  open DevTools to share state with Claude Code.

### 11.3 Conventions

- Log lines are single-line JSON-ish, prefixed with the module name
  (`tracker:`), keyed by event verb. Match whatever logging convention
  the host repo already follows; if there isn't one, this is a fine
  starting point and worth promoting to a `docs/LOGGING.md` doc.

---

## 12. Implementation plan for Claude Code

When ingesting this PRD in a new repo, run these steps in order:

### Phase 1 — Scaffolding (½ day)

1. Create `src/tracker/trackerData.ts` with the type definitions from
   §6 and **empty** exports (`MODULES: Module[] = []`, etc.).
2. Create `src/tracker/TrackerPage.tsx` rendering all 8 tabs against
   the empty arrays. The page should render without crashing on
   empty data (show "No modules yet" empty states).
3. Lazy-mount `/tracker` from the app's router. Match whatever
   `lazy(...)` / `Suspense` pattern the host repo already uses.
4. Add the `*_META` maps from §4.2 for status / priority / effort /
   feature-status / change-kind / next-action-status. These are the
   only place that knows about chip styling.
5. Smoke-test that `/tracker` loads with all 8 tabs visible and
   empty.

### Phase 2 — Codebase recon (1 day)

Claude Code walks the host repo and proposes the initial population:

1. **Modules.** For each top-level feature surface (every parent /
   admin / teacher route, every domain module under `src/modules/`
   or equivalent), propose a Module row with:
   - `name` from the most descriptive file or route segment.
   - `category` grouped sensibly (Communication, Calendar, Auth, etc.).
   - `routes` listing the actual paths.
   - `status` — best-guess from the code: full Firestore writes →
     `functional`, prototype data only → `prototype`, UI but no data
     wiring → `visual`, referenced but missing → `missing`.
   - `priority` — start everything at `—`; surface the open ones
     during review.
   - `notes` — one sentence per module with dates of recent commits
     touching the file, gleaned from `git log`.
2. **Infra gaps.** Look for: missing Cloud Functions, missing env
   vars, missing API keys, missing webhook endpoints, missing rules
   files. Each becomes an `I*` row with `blocks: [M-IDs]`.
3. **External systems.** Walk imports in `package.json` and `.env`
   references; every third-party SDK becomes an `E*` row.
4. **Feature specs.** If `docs/specs/` (or equivalent) exists,
   one `F*` row per spec doc. Otherwise leave empty until specs are
   written.
5. **Open questions.** Generate from "what the code can't answer":
   ambiguous flags, multiple paths with no winning convention,
   half-implemented features.
6. **Next actions.** For each `I*` and each `prototype` / `missing`
   module with priority ≥ P1, propose a Next Action with a copy-paste
   Claude Code prompt that includes:
   - The goal (1 sentence).
   - Steps (numbered).
   - Acceptance criteria.
   - Pointer to the spec doc if one exists.

### Phase 3 — Integrity tests (½ day)

Implement the test file at `src/tracker/trackerData.test.ts` covering
every invariant in §5.5. Run in CI as part of the standard test
suite. **Do not** ship the tracker without these — silent drift is
the failure mode that makes the tracker untrustworthy.

### Phase 4 — Recent changes seeding (½ day)

Walk the last 30 days of `git log` and seed `RECENT_CHANGES` with
the most significant ships. From here on, every PR that touches a
tracked row should add an entry. Append-only, newest-first.

### Phase 5 — Debug surface (¼ day)

Implement `/tracker/debug` (or `?debug=1`) per §11.2. The
"Copy for Claude Code" button is the deliverable.

### Phase 6 — Cadence

Add a `tracker hygiene` Next Action that reminds the builder (or
Claude Code) to:

1. Bump `TRACKER_META.lastVerified` to today.
2. Move shipped Next Actions from `todo` → `shipped` with a
   `statusNote`.
3. Prepend new entries to `RECENT_CHANGES`.
4. Re-run the integrity tests.

Run this every Friday or at the end of any session that lands a
shippable change.

---

## 13. Anti-goals (what makes the tracker fail)

- **Letting it rot.** A 60-day-old `lastVerified` reads as "no one
  is paying attention." Bump the date or delete the tracker.
- **Renumbering IDs.** Breaks every reference in old PRs, Slack
  threads, and Claude Code sessions.
- **Putting secrets in `notes`.** This page is internal but
  unauthenticated. Treat the contents like a README.
- **Auto-populating from a script.** The whole point is that a human
  + Claude Code curates the strawman. Generated lists read as noise.
- **Skipping the integrity tests.** Silent drift kills the tracker's
  credibility the first time a `relatedIds` entry points at a row
  that no longer exists.
- **Letting the markdown source and TS mirror drift apart.** Pick
  one as canonical, or run a sync check in CI.

---

## 14. Quickstart prompt for Claude Code

Paste this into Claude Code at the root of any repo to bootstrap:

```text
Implement the Codebase Tracker from docs/CODEBASE_TRACKER_PRD.md
end-to-end. Start with Phase 1 scaffolding: create
src/tracker/trackerData.ts with the types and empty exports,
src/tracker/TrackerPage.tsx rendering all 8 tabs against empty data,
and a lazy /tracker route in the app's router. After Phase 1 is
green, pause and ask me which categories to use for module grouping
and which routes / modules you noticed during the scaffolding so we
can populate Phase 2 together.

Throughout: match the host repo's existing conventions (CSS approach,
test runner, router pattern). The PRD's Tailwind classes are
defaults — adapt if the host uses a different styling layer.
```

---

**Source:** abstracted from a working tracker built for a multi-
tenant SaaS app. The reference implementation lives at
`src/tracker/TrackerPage.tsx` + `src/tracker/trackerData.ts` in the
originating repo; the integrity test suite at
`src/tracker/trackerData.test.ts` is the canonical example of §5.5
in action.
