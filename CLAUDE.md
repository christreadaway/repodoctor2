# Claude Code Instructions - RepoDoctor2

## About This Project
v2 of RepoDoctor — rebuilt from spec v4.0. Same concept (GitHub branch analysis + AI summaries + cleanup) but rebuilt with improved architecture, better UI, model selection, and enhanced features. Flask web app with retro terminal aesthetic. 43 tests passing, ~5,100 lines of code.

## About Me (Chris Treadaway)
Product builder, not a coder. I bring requirements and vision — you handle implementation.

**Working with me:**
- Bias toward action — just do it, don't argue
- Make terminal commands dummy-proof (always start with `cd ~/repodoctor2`)
- Minimize questions — make judgment calls and tell me what you chose
- I get interrupted frequently — always end sessions with clear handoff

## Tech Stack
- **Backend:** Python + Flask
- **AI:** Anthropic Claude API (user-selectable model)
- **GitHub:** REST API v3 with Personal Access Token
- **Security:** Fernet + PBKDF2 credential encryption
- **Frontend:** Jinja2 templates + retro terminal CSS
- **Testing:** 43 tests across full test suite
- **Port:** localhost:5001

## File Paths
- **Always use:** `~/repodoctor2/`
- **Always start commands with:** `cd ~/repodoctor2`

## PII Rules
❌ NEVER include: GitHub tokens, Anthropic API keys, real repo data, credential files, file paths with /Users/christreadaway → use ~/
✅ Keys encrypted locally via Fernet + PBKDF2

## Running Locally
```
cd ~/repodoctor2
pip install -r requirements.txt
python app.py
```
Opens at http://localhost:5001

## Key Differences from RepoDoctor v1
- User can select which AI model to use
- Improved UI (was "all white and black" in v1)
- Better error handling and security review
- 43 automated tests
- Built from comprehensive PDF spec (v4.0)

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
