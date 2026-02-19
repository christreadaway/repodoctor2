# Claude Code Instructions - RepoDoctor2

## About This Project
Repository visualization tool showing branch counts and required file status across all GitHub repos (public and private). Flask web app.

## About Me (Chris Treadaway)
Product builder, not a coder. I bring requirements and vision — you handle implementation.

**Working with me:**
- Bias toward action — just do it, don't argue
- Make terminal commands dummy-proof (always start with `cd ~/repodoctor22`)
- Minimize questions — make judgment calls and tell me what you chose
- I get interrupted frequently — always end sessions with clear handoff

## Tech Stack
- **Backend:** Python + Flask
- **AI:** Anthropic Claude API (Sonnet 3.5) for branch summaries
- **GitHub:** REST API v3 with Personal Access Token
- **Security:** Fernet + PBKDF2 credential encryption
- **Frontend:** Single-page app with retro terminal CSS
- **Session:** 30-minute auto-lock

## File Paths
- **Always use:** `~/RepoDoctor2/`
- **Always start commands with:** `cd ~/repodoctor22`

## PII Rules
❌ NEVER include: GitHub tokens, Anthropic API keys, real repo analysis data, credential files, file paths with /Users/christreadaway → use ~/
✅ Keys stored encrypted locally, never in prompts/logs/UI

## Key Features
- Secure Credential Storage (encrypted at rest)
- Cross-Repo Dashboard with health indicators
- AI-Powered Branch Summaries
- Smart Branch Categories with auto-delete/one-click merge
- Claude Conversation Integration (import ZIP exports + local sessions)
- Activity Log with color-coded messages
- Cost tracking (~$0.003 per conversation analyzed)

## Git Branch Strategy
- Claude Code creates new branch per session
- Merge to main when stable
- Delete merged branches immediately

## Session End Routine

At the end of EVERY session — or when I say "end session" — do ALL of the following:

### A. Update SESSION_NOTES.md
Append a detailed entry at the TOP of SESSION_NOTES.md (most recent first) with: What We Built, Technical Details, Current Status (✅/❌/🚧), Branch Info, Decisions Made, Next Steps, Questions/Blockers.

### B. Update PROJECT_STATUS.md
Overwrite PROJECT_STATUS.md with the CURRENT state of the project — progress %, what's working, what's broken, what's in progress, next steps, last session date/summary. This is a snapshot, not a log.

### C. Commit Both Files
```
git add SESSION_NOTES.md PROJECT_STATUS.md
git commit -m "Session end: [brief description of what was done]"
git push
```

### D. Tell the User
- What branch you're on
- Whether it's ready to merge to main (and if not, why)
- Top 3 next steps for the next session

---
Last Updated: February 16, 2026


## Branch Rules
Always work on the main branch. Do not create new branches unless explicitly asked. Commit and push all changes directly to main.

