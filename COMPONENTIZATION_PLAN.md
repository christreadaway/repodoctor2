# Componentization Plan

## What this is

A plan for pulling the reusable infrastructure out of ~50 repos and turning it into a small set of federated components that any new project can attach in minutes. RepoDoctor's new **Components** page is the first tool in that process: it inventories which repo has which capability and which repo is the best source to extract from.

## Why

In your words: the same pieces have been rebuilt across projects for years - auth, logging, email, testing setups, Claude docs, deployment config. The goal is to extract that infrastructure from the existing projects, federate it so each piece lives in one well-kept place, attach it when a new concept starts instead of rebuilding it, and automate the parts that can be automated (scans, checks, docs updates).

The payoff is speed on new ideas and fewer repeated mistakes. A new project should start with logging, tests, security basics, deploy config and Claude docs already in place, so the first session is about the product, not the plumbing.

## Recommended architecture

Three honest options for how a component gets attached to a project.

**(a) Component repos consumed as git submodules or packages.** A family of repos (one per capability, something like `cjt-components/logging`) pulled into each project as a git submodule, or published as a pip or npm package. Good for runnable code. Submodules are fiddly: every clone needs `--recurse-submodules`, updates are a manual bump, and a submodule can't easily be customized per project. Packages are cleaner but need a registry and versioning discipline.

**(b) Claude Code plugins and skills.** One marketplace repo with a `.claude-plugin/` folder that holds skills, slash commands and hooks. Any project adds the marketplace once and Claude Code loads the knowledge into every session. Perfect for process and knowledge: how to write a PRODUCT_SPEC, the security checklist, the testing playbook, the GTM launch checklist. Weak for runnable code: a skill can tell Claude how to wire up SendGrid, but it does not ship the client.

**(c) A template repo plus copy-in scripts.** A GitHub template repo you fork for every new concept, with scripts that copy a component's files in. Simplest to understand, zero tooling. Weakest long term: copies drift immediately and there is no way to push a fix back out to 50 projects.

| Component type | Mechanism | Why |
|---|---|---|
| Claude docs management (CLAUDE.md, PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, session start/end commands) | (b) plugin | It is process and templates, consumed by Claude, not by the app |
| Testing playbook, security checklist, design system guidance, GTM playbook | (b) plugin | Knowledge Claude applies, tuned to your standards |
| Logging, email, SMS, auth helpers, API client wrappers | (a) package (pip/npm), submodule only as a fallback | Runnable code that must be versioned and fixed in one place |
| Deployment config, launchers (start.command, start.ps1), Makefile | (c) template repo with (b) skill to adapt it | Boilerplate that is always lightly customized per project |
| Data viz, PDF export, scheduling | (a) package once stable, (b) skill until then | Starts as a recipe, graduates to code when two projects share the same shape |

**Recommendation:** build (b) first. It covers the three highest-frequency components (docs, testing, security) with the least tooling, and it works for pure-Python and Node projects alike. Add (a) packages as code components mature. Use (c) only as the new-project starting point, kept thin.

## Component catalog

Source candidates come from the RepoDoctor Components inventory. Run it, then fill the "best source" column from the page.

| Bucket | What goes in | Interface | Automation opportunity |
|---|---|---|---|
| Marketing / GTM | Landing page skeleton, SEO meta and sitemap generator, analytics tag snippet, pricing page pattern, launch checklist | Skill + HTML/CSS template | Generate sitemap and OG tags from the spec |
| Design / Usability | Design tokens, Tailwind or CSS base, component patterns, the UI/UX audit rubric from CLAUDE.md | Skill + CSS token file | Run the design audit as a slash command |
| BI / Data Viz | Chart defaults (palette, axes, tooltips), dashboard layout, stat tile pattern | Package (JS) + skill | Theme every chart from the token file |
| Testing | pytest/Jest scaffolds, route smoke tests, test naming rules, "test with ferocity" playbook | Skill + scaffold files | Pre-commit hook that runs the suite |
| Security | security.py pattern (CSRF, rate limiting, headers), secrets handling, dependency audit routine | Package (Python) + checklist skill | Scheduled pip-audit and npm audit |
| Claude Docs Management | CLAUDE.md template, PRODUCT_SPEC/PROJECT_STATUS/SESSION_NOTES structure, /start and /end commands, PII rules | Plugin | Session-end hook that updates status and commits |
| Communications | Email client (SendGrid/Resend/smtplib), SMS client (Twilio), webhook receiver, notification templates | Package | Retry and dead-letter logging built in |
| APIs & Integrations | Auth helper (Firebase/Google), Firestore access layer, API client base class with logging and backoff, AI SDK wrapper | Package | Rate-limit and cost logging by default |
| Platform & Ops | Logging setup with rotation, deploy configs (Netlify, Firebase, Docker), launchers, Makefile, scheduled jobs pattern | Template + package | Nightly health check across all deployed apps |

## Infrastructure areas (Portfolio → Infrastructure)

The Portfolio screen turns the catalog above into one card per **functional area**, each with a **home repo** you assign (where that component lives or will live) and the best source repos from the last Components scan. The areas, your list first, then the ones the repo list shows recurring:

| Area | What it holds | Likely sources today (by repo name; confirm with a Components scan) |
|---|---|---|
| Marketing / GTM | Landing pages, SEO, analytics, launch checklists | redmon, treadawaywebsite, st-theresasite |
| Video production | Remotion / ffmpeg pipelines, captions | none obvious yet (likely a gap) |
| Security | CSRF / headers / rate limits, secrets, credential encryption | repodoctor2 (`security.py`) |
| Messaging (email, SMS) | Email + SMS senders, templates, webhooks | parentpoint, campusops |
| BI / charts / data viz | Chart defaults, dashboards, stat tiles | personalfinance, thesharppoll, repodoctor2 (Stats) |
| Courseware | Lessons, curricula, standards alignment, quizzes | lessonalign, standardscollector, teacherAIde, mathtracker |
| Audio / transcription | Recording, Whisper transcription | audioscribe |
| AI integration | Claude client wrapper, structured outputs, cost logging | repodoctor2 (`ai_analyzer.py`), teacherAIde, craft-agents |
| Sign-in & data | Google sign-in, Firebase / Firestore layer | parentpoint, beacon |
| CRM & contacts | People records, leads, follow-ups | personalcrm, shepherdCRM, birddogCRM |
| Privacy & PII redaction | Anonymizing documents before AI or reports | docanonymizer |
| Payments | Stripe checkout, subscriptions | (scan will tell) |
| Logging & debugging | Rotating app log, copy-for-Claude error format | repodoctor2 |
| Testing | Test scaffolds, smoke tests, browser checks | repodoctor2 |
| Claude docs management | CLAUDE.md, spec/status/notes, /start and /end | claudesync2, claudecodearchiver, md_md, every repo |
| Deploy, launchers & scheduling | Netlify / Firebase / Docker, start scripts, scheduled jobs | ops, edge-ops, rundeck, StTdevops |
| Documents & PDF | PDF / DOCX generation, templates | worshipaidcreator, sacramentalrecords |

The CRM and privacy rows are the two the repo list surfaced on its own: three separate CRM repos means a shared contacts component would pay off immediately, and PII redaction is the guardrail every school and parish app needs before sending anything to an AI.

## How RepoDoctor supports the process

1. **Components inventory.** Scan once. The page shows each capability, the best-source repo with the evidence paths, and every other repo that has it. The "Suggested extraction order" ranks by how many repos share the capability.
2. **Pick the best source.** Open the repo from the card, read the evidence files in the Docs tab, confirm it really is the cleanest implementation (the scan ranks by file count, not quality, so this is a human call).
3. **Extraction checklist.** Copy the inventory with COPY FOR CLAUDE, start a Claude Code session in the source repo, and work the checklist below.
4. **Rescan after each extraction.** Coverage for that capability should drop as repos switch to the shared component. That is the progress metric.

## Extraction checklist (per component)

1. Name the component and its bucket. Write a one-paragraph spec: what it does, what it does not do.
2. Read the best-source implementation and one or two others. Note the differences. The union of features is usually wrong; pick the smallest version that covers the real uses.
3. Decide the mechanism (plugin, package, template) using the table above.
4. Create the component repo from the template. It gets its own CLAUDE.md, tests, and logging from day one.
5. Move the code or docs in. Strip project-specific names, paths and credentials. Apply the PII rules.
6. Write the attach instructions: one command or one copy step, verified on a fresh clone.
7. Attach it to the source repo first and delete the original implementation there. If the source repo still works, the component is real.
8. Attach to one more repo. Only then call it v1.
9. Record the component in the catalog table above and rescan in RepoDoctor.

## Sequencing for the coming weeks

**First three components, in order:**

1. **Claude docs management.** It is in nearly every repo (CLAUDE.md, spec, status, session notes, .claude/commands). It is pure text, so there is zero runtime risk, and it immediately improves every future session. Ship it as the first plugin.
2. **Logging.** Your standing rule is that every app logs everything for debugging. Most repos have some version of it and they all differ. A small Python package (file handler, rotation, a copy-paste-friendly error format) plus a JS equivalent removes a lot of duplicated setup with very low risk.
3. **Testing.** Scaffolds and the test playbook. Every repo has tests, few share conventions. Shipping the scaffold alongside the docs plugin means new projects start with a passing suite.

After those: security (checklist first, security.py package second), then deployment and launchers as the template repo, then communications and auth as packages once two projects need the same interface.

## Out of scope

- Reading file contents during the scan. The inventory is path-based by design; a content scan is a later feature.
- Automatic extraction or refactoring by RepoDoctor. It finds and ranks; a human and Claude Code do the extraction.
- A private package registry. Start with GitHub-hosted installs (`pip install git+...`, npm GitHub URLs).
- Migrating all 50 repos onto the components. Only the source repo and one more per component to prove it.

## Open questions

- One marketplace repo for all skills, or one plugin per bucket? (Leaning one marketplace, one plugin per bucket inside it.)
- Python-first or dual Python/JS for code packages? The inventory will show the real split.
- Should the components live under a GitHub org or as repos under your personal account with a `cjt-` prefix?
- How much customization does a component allow before it stops being one component? Needs a rule of thumb after the first two extractions.

## Success criteria

- The Components page shows every capability with a best source and evidence, and the Markdown export pastes cleanly into a Claude chat.
- Three components extracted and attached to at least two repos each within the first few weeks.
- A new concept repo goes from empty to "logging, tests, docs, security basics in place" in one session using only attach steps.
- Rescans show coverage shifting from many ad-hoc implementations to one shared component per capability.
- No regression in any source repo after its original implementation is replaced by the component.
