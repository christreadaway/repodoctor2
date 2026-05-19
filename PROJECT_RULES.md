# RepoDoctor2 — Project-Specific Rules

Project-local conventions that supplement the global instructions in
`CLAUDE.md`. Anything here applies **only to RepoDoctor2**; general
behavior lives in `CLAUDE.md`.

---

## Branch Rules

Always work on the `main` branch. Do not create new branches unless
explicitly asked. Commit and push all changes directly to `main`.

When the Claude Code harness auto-creates a session branch (e.g.
`claude/fix-...`), the session work lands there, but the canonical home
of finished work is still `main` — merge it in as soon as it's stable
and delete the branch.

---

## Session End Routine

At the end of EVERY session — or when Chris says "end session" — do ALL
of the following:

### A. Update SESSION_NOTES.md
Append a detailed entry at the TOP of `SESSION_NOTES.md` (most recent
first) with:
- What We Built
- Technical Details
- Current Status (✅/❌/🚧)
- Branch Info
- Decisions Made
- Next Steps
- Questions/Blockers

### B. Update PROJECT_STATUS.md
Overwrite `PROJECT_STATUS.md` with the CURRENT state of the project —
progress %, what's working, what's broken, what's in progress, next
steps, last session date/summary. This is a snapshot, not a log.

### C. Commit Both Files
```
git add SESSION_NOTES.md PROJECT_STATUS.md
git commit -m "Session end: [brief description of what was done]"
git push
```

### D. Tell the User
- What branch you're on
- Whether it's ready to merge to `main` (and if not, why)
- Top 3 next steps for the next session

---

## Tech Debt — Remove on Next Product Rebuild

**Default group seeding (added April 2026).** On login,
`_init_session` calls `models.seed_default_groups_if_missing()` which
hard-codes Chris's 5 groups (School, Church, Catholic Games,
Infrastructure, Fun) as a one-shot recovery after a codebase wipe.
This is temporary. When we next rebuild this product:

- Delete `DEFAULT_USER_GROUPS` and `seed_default_groups_if_missing()`
  from `models.py`.
- Remove the call + its `log_action` in `app._init_session`.
- Groups now persist at `~/.repodoctor/groups.json`, so nothing else
  is needed — the stored file is the source of truth.

---

## Project Path & Launch

- Always start commands with: `cd ~/repodoctor2`
- Mac launcher: `./start.command`
- Windows launcher: `.\start.ps1`
- Local URL: http://127.0.0.1:5001

For tech stack, feature list, and current status, see
`PROJECT_STATUS.md`.
