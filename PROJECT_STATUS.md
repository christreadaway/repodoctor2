# RepoDoctor2 - Project Status

> **Repository:** `github.com/christreadaway/repodoctor2`
> **Category:** Infrastructure
> **Local Path:** `~/repodoctor2/`

## Overall Progress: 85%

## What's Working
- Secure credential storage (Fernet + PBKDF2 encryption) — local dev
- GitHub PAT authentication with scope verification
- Full repo scanning — branch counts, required file checks
- Dashboard table with sortable columns, retro terminal UI
- Sticky table headers — column labels stay visible when scrolling
- Required files detection: CLAUDE.md, LICENSE, BUSINESS_SPEC.md, PRODUCT_SPEC.md, PROJECT_STATUS.md, SESSION_NOTES.md (case-insensitive, any extension)
- Clickable repo detail pages with spec file content display
- "Current?" column — detects if docs are fresh (within 7 days of last commit)
- 30-minute session auto-lock
- Activity log with color-coded messages
- Netlify deployment — Node.js Express app as serverless function
- Site password gate for deployed version (SITE_PASSWORD env var)
- AI project summaries via Claude Haiku
- Cold start credential restoration (env vars re-read on each authenticated request)
- Parallelized scan (batches of 10) and summary generation (batches of 5)

## What's Broken
- Nothing currently broken

## What's In Progress
- Branch `claude/deploy-netlify-F7VHx` — Netlify deployment fixes (cold start auth, timeout parallelization), awaiting live verification

## Tech Stack
- **Local dev:** Python + Flask backend
- **Deployed (Netlify):** Node.js + Express + serverless-http
- **Templates:** Nunjucks (Jinja2-compatible)
- **AI:** Anthropic Claude API (Haiku for summaries)
- **GitHub:** REST API v3 with Personal Access Token
- **Security:** Fernet + PBKDF2 (local) / Env vars (Netlify)
- **Frontend:** Single-page app with retro terminal CSS (IBM Plex Mono)

## Next Steps
1. Deploy and verify scan + summary generation on live Netlify site
2. Ensure env vars are set in Netlify dashboard (GITHUB_PAT, ANTHROPIC_API_KEY, SITE_PASSWORD, FLASK_SECRET_KEY)
3. Merge `claude/deploy-netlify-F7VHx` to main once verified
4. Consider Netlify Blobs for persistent data (scan results survive cold starts)
5. If timeouts persist with many repos, evaluate Netlify Background Functions or paid plan

## Blockers
- Free tier has 10s function timeout (26s on paid). Large GitHub accounts may still time out.

## Last Session
- **Date:** 2026-03-11
- **Branch:** `claude/deploy-netlify-F7VHx`
- **Summary:** Fixed two serverless deployment bugs: (1) cold start losing GitHub credentials (added env var restoration in auth middleware), (2) inactivity timeout during scan/generate (parallelized repo processing with Promise.all batching, set max 26s timeout).
