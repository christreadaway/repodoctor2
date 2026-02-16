# RepDoctor2 — Business Product Spec
**Version:** 4.0 | **Date:** 2026-02-16 | **Repo:** github.com/christreadaway/repodoctor2

---

## 1. Problem Statement
RepoDoctor (v1) proved the concept of AI-powered branch management, but the architecture was tightly coupled and the UI needed significant usability improvements. Users wanted model selection (not everyone needs the most expensive AI), guided onboarding (not a blank screen), and plain-English explanations that non-developers could understand. The v1 codebase also needed a cleaner separation of concerns for maintainability.

## 2. Solution
A ground-up rebuild of RepoDoctor with a modular Flask architecture, user-selectable AI models (Haiku/Sonnet/Opus with pricing), guided 4-step onboarding walkthrough, and dual display modes (Plain English default, Shorthand for power users). Same retro terminal aesthetic, but with a more polished and intuitive user experience.

## 3. Target Users
- Same as RepoDoctor: Claude Code developers, vibe coders, solo developers
- **Added focus:** Non-technical users who need guided onboarding

## 4. Core Features

### Guided Onboarding (4-Step Walkthrough)
1. **Welcome** — Explains what RepDoctor2 does in plain language
2. **Connect GitHub** — Paste Personal Access Token with clear instructions
3. **Connect AI** — Paste Anthropic API key with model selection
4. **Set Password** — Create encryption password for credential storage

### Model Selection
- **Claude Haiku** (default) — ~$0.80/M tokens, fast, cost-effective
- **Claude Sonnet** — ~$3/M tokens, balanced
- **Claude Opus** — ~$15/M tokens, most detailed analysis
- Pricing shown in settings dropdown
- Changeable anytime via settings

### Display Modes
- **Plain English** (default) — "4 unique changes not in main", "Safe to archive — no unique work"
- **Shorthand** — AHEAD, BEHIND, DIVERGED, STALE
- Quick-toggle button in nav bar
- Persistent preference saved in settings

### Colored Recommendations
- Each branch gets an actionable recommendation with color coding
- Green: Safe to delete/archive
- Yellow: Merge first to preserve changes
- Red: Needs manual review — diverged or complex state

### Secure Architecture (20 Files, ~5,100 Lines)
- **app.py** — Flask routes
- **security.py** — Fernet + PBKDF2 credential encryption
- **github_client.py** — GitHub REST API v3
- **ai_analyzer.py** — Anthropic API integration with model selection
- **models.py** — Local storage
- **templates/** — 8 Jinja2 templates
- **static/** — Retro terminal CSS + JS

### Session Management
- Password-protected lock screen
- Auto-lock timeout
- Encrypted credential file on disk

## 5. Tech Stack
- **Backend:** Python Flask
- **AI:** Anthropic Claude API (Haiku default, all 3 tiers available)
- **GitHub:** REST API v3
- **Security:** Fernet + PBKDF2
- **Frontend:** Jinja2 templates + vanilla CSS/JS, retro terminal aesthetic
- **Tests:** 43 unit tests
- **Port:** localhost:5001

## 6. Data & Privacy
- Same security model as RepoDoctor v1
- Credentials encrypted at rest
- API keys never in prompts, logs, or UI
- All data local

## 7. Current Status
- **Built:** Complete application from PDF spec (v4.0)
- **Tests:** 43/43 passing
- **Merged:** In main branch
- **Verified:** All pages load successfully
- **Improvements Applied:** Model selection, onboarding walkthrough, display modes, colored recommendations

## 8. Business Model
- **Free / Open Source** — Developer tool
- **Cost-Conscious Design** — Haiku default keeps per-analysis cost at ~$0.003

## 9. Success Metrics
- Same as RepoDoctor, plus:
- Onboarding completion rate (4-step walkthrough)
- Model tier distribution (how many users stay on Haiku vs. upgrade)
- Display mode preference split

## 10. Open Questions / Next Steps
- Consolidate RepoDoctor and RepDoctor2 into a single product
- Docker deployment option
- Claude conversation import (port from RepoDoctor v1)
- Branch cleanup automation with scheduling
- GitHub webhook integration
- Team/org support
