# RepoDoctor2 - Project Status

> **Repository:** `github.com/christreadaway/repodoctor2`
> **Category:** Infrastructure
> **Local Path:** `~/repodoctor2/`

## Overall Progress: 82%

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

## What's Broken
- Nothing currently broken

## What's In Progress
- Branch `claude/deploy-netlify-F7VHx` — Netlify deployment with full Node.js refactor (fixed CSS/JS serving, awaiting live verification)

## Tech Stack
- **Local dev:** Python + Flask backend
- **Deployed (Netlify):** Node.js + Express + serverless-http
- **Templates:** Nunjucks (Jinja2-compatible)
- **AI:** Anthropic Claude API (Haiku for summaries)
- **GitHub:** REST API v3 with Personal Access Token
- **Security:** Fernet + PBKDF2 (local) / Env vars (Netlify)
- **Frontend:** Single-page app with retro terminal CSS (IBM Plex Mono)

## Next Steps
1. Verify Netlify deployment with retro terminal UI loading correctly
2. Set env vars in Netlify dashboard (GITHUB_PAT, ANTHROPIC_API_KEY, SITE_PASSWORD, FLASK_SECRET_KEY)
3. Test full scan + repo detail on live site
4. Merge `claude/deploy-netlify-F7VHx` to main
5. Consider Netlify Blobs for persistent data (scan results survive cold starts)

## Blockers
- Netlify Functions 10-second timeout may affect repos with 50+ repositories during scan

## Last Session
- **Date:** 2026-03-11
- **Branch:** `claude/deploy-netlify-F7VHx`
- **Summary:** Rewrote Flask backend to Express.js for Netlify deployment. Fixed static asset serving — CSS/JS weren't loading because publish directory structure didn't match template URLs. Build now copies static files into dist/static/ and removes force=true from catch-all redirect so CDN serves assets directly.
