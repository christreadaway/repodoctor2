# Claude Code Instructions - RepoDoctor2

## About This Project
Enhanced version of RepoDoctor. Flask web app for Git branch management with GitHub integration, AI analysis via Anthropic, retro terminal UI, and credential encryption. Analyzes branches, provides AI summaries, shows merge risks, and helps clean up orphaned branches. Built from comprehensive PDF spec v4.0.

## About Me (Chris Treadaway)
Product builder, not a coder. I bring requirements and vision — you handle implementation.

**Working with me:**
- Bias toward action - just do it, don't argue
- Make terminal commands dummy-proof (always start with `cd ~/repodoctor2`)
- Minimize questions - make judgment calls and tell me what you chose
- I get interrupted frequently - always end sessions with a handoff note

## Tech Stack
- **Language:** Python 3.9+
- **Framework:** Flask
- **Frontend:** Jinja2 templates + vanilla JavaScript
- **Styling:** Retro terminal aesthetic (green-on-black CRT)
- **Git Integration:** GitHub REST API v3
- **AI:** Anthropic Claude API (default: Haiku 4.5 for cost efficiency)
- **Security:** Fernet + PBKDF2 credential encryption
- **Storage:** JSON files for local state

## File Paths
- **Always use:** `~/repodoctor2/path/to/file`
- **Never use:** `/Users/christreadaway/...`
- **Always start commands with:** `cd ~/repodoctor2`

## PII Rules (CRITICAL)
❌ NEVER include:
- Real repo names in examples → use [Repo Name]
- Commit messages with personal info → redact
- File paths with /Users/christreadaway → use ~/
- API keys in code (use environment variables)

✅ ALWAYS use placeholders

## Project Structure
```
repodoctor2/
├── app.py                    # Flask app with all routes
├── security.py               # Fernet + PBKDF2 encryption
├── github_client.py          # GitHub REST API v3 client
├── ai_analyzer.py            # Anthropic API integration
├── models.py                 # Local storage (JSON files)
├── static/
│   ├── css/style.css         # Retro terminal design
│   └── js/app.js             # Frontend JavaScript
├── templates/                # 8 Jinja2 templates
│   ├── dashboard.html
│   ├── settings.html
│   ├── lock.html
│   └── ...
├── tests/test_app.py         # 43 unit tests
└── requirements.txt
```

## Key Features
- **Branch Analysis:** Categorizes as clean/merge/conflict/stale
- **AI Summaries:** Claude API analyzes diffs, provides plain-English descriptions
- **Risk Assessment:** Calculates merge risk (file changes, age, conflicts)
- **One-Click Actions:** Merge clean branches, delete stale ones
- **Display Modes:** "Plain English" (default) or "Shorthand" mode
- **Model Selection:** User picks Haiku/Sonnet/Opus based on budget
- **Guided Onboarding:** 4-step walkthrough for first-time users
- **Credential Encryption:** All API keys encrypted at rest

## Differences from Original RepoDoctor
- **repodoctor:** Browser-based with isomorphic-git
- **repodoctor2:** Flask server with GitHub REST API
- **repodoctor2** has cleaner UI, better onboarding, model selection

## Session End Routine
Before ending EVERY session, Claude will automatically create/update SESSION_NOTES.md:

```markdown
## [Date] [Time] - [Brief Description]

### What We Built
- [Feature 1]: [files modified]
- [Feature 2]: [what was implemented]

### Technical Details
Files changed:
- path/to/file.ext (what changed)
- path/to/file2.ext (what changed)

Code patterns used:
- [Pattern or approach used]
- [Libraries or techniques applied]

### Current Status
✅ Working: [what's tested and works]
❌ Broken: [known issues]
🚧 In Progress: [incomplete features]

### Branch Info
Branch: [branch-name]
Commits: [X files changed, Y insertions, Z deletions]
Ready to merge: [Yes/No - why or why not]

### Decisions Made
- [Decision 1 and rationale]
- [Decision 2 and rationale]

### Next Steps
1. [Priority 1 with specific action]
2. [Priority 2 with specific action]
3. [Priority 3 with specific action]

### Questions/Blockers
- [Open question or blocker]
- [Uncertainty that needs resolution]
```

**To execute:** Say "Append session notes to SESSION_NOTES.md" and Claude will:
1. Create/update SESSION_NOTES.md in repo root
2. Add new session at the TOP (most recent first)
3. Commit the file to current branch
4. Confirm completion

SESSION_NOTES.md is committed to the repo and tracks all session progress over time.

## Git Branch Strategy
- Claude Code creates new branch per session
- Merge to main when all 43 tests pass
- Delete merged branches immediately

## Testing Approach
- Run full test suite: `python -m pytest tests/`
- All 43 tests must pass before merge
- Manual testing on real repos with multiple branches
- Test both display modes (plain English + shorthand)
- Verify credential encryption works

## Setup/Installation
```bash
cd ~/repodoctor2
pip install -r requirements.txt
python app.py
# Open http://localhost:5001
```

## First Run Setup
1. Enter GitHub Personal Access Token (with repo scope)
2. Enter Anthropic API Key
3. Set password for credential encryption
4. Select AI model (Haiku recommended for cost)

## API Model Recommendations
- **Haiku 4.5:** ~$0.80/M tokens - Best value, sufficient for branch summaries
- **Sonnet 4.5:** ~$3/M tokens - Better quality, use for complex repos
- **Opus 4.5:** ~$15/M tokens - Overkill for most use cases

## Common Issues
- **GitHub token needs `repo` scope** - not just read-only
- **Display mode toggle** - Stored in user preferences
- **Credential encryption** - Don't lose password, can't recover
- **Port 5001 conflicts** - Change in app.py if needed

## Current Status
Fully built from PDF spec v4.0. All 43 tests passing. Ready for production use.

## Product Vision
**Phase 1 (Current):** Desktop web app for local branch management
**Phase 2:** Scheduled reports, email notifications
**Phase 3:** GitHub integration for remote repo analysis

---
Last Updated: February 16, 2026
