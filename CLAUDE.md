# RepoDoctor2 — CLAUDE.md

> **Repository:** `github.com/christreadaway/repodoctor2`
> **Category:** Infrastructure
> **Stack:** Python, GitHub API, reportlab
> **Localhost Port:** 3011

## What This Project Is
Next-gen repository health analysis with PDF reports

## Session Start Protocol (MANDATORY — DO THIS FIRST)
**Before writing ANY code or making ANY changes, execute these steps in order:**

### Step 1: Sync with remote
```bash
git fetch origin
```

### Step 2: Check for critical files
```bash
ls PROJECT_STATUS.md SESSION_NOTES.md 2>/dev/null
```

### Step 3: If EITHER file is missing, recover from main
```bash
git checkout origin/main -- PROJECT_STATUS.md 2>/dev/null; git checkout origin/main -- SESSION_NOTES.md 2>/dev/null
```

### Step 4: If creating a new branch, ALWAYS branch from latest origin/main
```bash
git fetch origin
git checkout -b <branch-name> origin/main
```
This ensures the new branch starts with all files from main, including PROJECT_STATUS.md and SESSION_NOTES.md.

### Step 5: Read SESSION_NOTES.md for prior context
Check what was built last session, what's broken, and what the next steps were. This is your continuity lifeline.

### Step 6: Confirm branch state
Run `git status` and `git branch` to confirm where you are before making changes.

**DO NOT SKIP THESE STEPS.** Missing them causes context loss and wasted time.

## Session End Routine (MANDATORY — DO THIS LAST)
**Before ending EVERY session, create/update SESSION_NOTES.md with this format:**

```markdown
## [Date] [Time] - [Brief Description]

### What We Built
- [Feature 1]: [files modified]
- [Feature 2]: [what was implemented]

### Technical Details
Files changed:
- path/to/file.ext (what changed)

Code patterns used:
- [Pattern or approach used]

### Current Status
- Working: [what's tested and works]
- Broken: [known issues]
- In Progress: [incomplete features]

### Branch Info
Branch: [branch-name]
Ready to merge: [Yes/No - why or why not]

### Decisions Made
- [Decision 1 and rationale]

### Next Steps
1. [Priority 1 with specific action]
2. [Priority 2 with specific action]
3. [Priority 3 with specific action]

### Questions/Blockers
- [Open question or blocker]
```

**How to execute:**
1. Create or update SESSION_NOTES.md in the repo root
2. Add new session entry at the TOP (most recent first)
3. Run: `git add SESSION_NOTES.md && git commit -m "Update session notes"`
4. Confirm completion to the user

**DO NOT END A SESSION WITHOUT DOING THIS.**

## Slash Commands
- `/start` — Run the Session Start Protocol (branch sync + file recovery + context load)
- `/end` — Run the Session End Routine (save session notes + commit)

## Project-Specific Notes
- PDF report generation for repo health
- Evolution of RepoDoctor
- GitHub API integration

## Security Requirements
- Proactively self-evaluate for SQL injection, XSS, CSRF, auth bypasses, and other common vulnerabilities
- Flag security issues before completing builds — do not wait to be asked
- NEVER expose API keys, tokens, or credentials in code or committed files
- Use .env files (with .gitignore protection) for local secrets
- Use secrets managers or environment variables for production

## PII Rules
- No real institution names, people, addresses, phones, or emails in code — use [Parish Name], [Staff Name], etc.
- No local file paths in committed code — use ~/ or environment variables
- No API keys, tokens, or credentials in any committed files
- These rules apply to ALL code, artifacts, files, or snippets generated

## User Context
- Chris is a product builder, NOT a developer — provide detailed, dummy-proof instructions
- When giving terminal commands, ALWAYS start with `cd` to the correct directory
- Default to Windows paths (C:\Users\chris-treadaway\) — Chris works primarily on Windows
- Minimize questions — make reasonable judgment calls and explain what you chose
- Auth preference: Google Sign-In via Firebase (never username/password)
- Chris gets interrupted frequently — SESSION_NOTES.md is the continuity lifeline

