# Repo Doctor 2 - Product Specification

**Repository:** `repodoctor2`  
**Filename:** `repodoctor2-PRODUCT_SPEC.md`  
**Last Updated:** 2026-02-16 at 16:30 UTC

---

## What This Is

**Repo Doctor 2** - Enhanced Git branch management tool with AI-powered analysis, recommendations for branch cleanup, and retro terminal UI. Flask web application with GitHub integration and intelligent branch lifecycle management.

## Who It's For

**Primary Users:** Developers managing multiple repositories, team leads overseeing branch hygiene, development teams

## Tech Stack

Python Flask web app, GitHub REST API v3, Anthropic Claude API (Haiku 4.5 default), Fernet + PBKDF2 credential encryption, retro terminal UI aesthetic, local storage with models.py

---

## Core Features

The following features have been implemented based on development sessions:

1. Flask web application with all routes and views
2. GitHub Personal Access Token authentication for entire account
3. Repository scanning across all user repos
4. Branch analysis with AI-powered recommendations
5. Display modes: Plain English (default) and Shorthand
6. Quick-toggle button in nav bar for display mode switching
7. Settings dropdown for permanent display mode preference
8. AI model selection (Haiku 4.5, Sonnet 4.5, Opus 4.5) with pricing display
9. Default model: Claude Haiku 4.5 (~$0.80/M tokens)
10. Four-step guided onboarding walkthrough
11. Plain English descriptions: "4 unique changes not in main" instead of technical jargon
12. Color-coded branch recommendations
13. Branch lifecycle recommendations: "Safe to archive and delete", "Merge to keep changes"
14. Sequential branch analysis assuming next branch builds on prior
15. Time/date stamps for branch activity analysis
16. 30-day deprecation period before recommending deletion
17. Credential encryption with security.py
18. GitHub REST API client (github_client.py)
19. AI analyzer with Anthropic integration (ai_analyzer.py)
20. Retro terminal aesthetic (CSS)
21. Frontend JavaScript for interactions
22. Eight Jinja2 templates
23. Comprehensive test suite (43 tests, all passing)
24. Dashboard with descriptive status badges
25. Repository detail pages with branch analysis

---

## Technical Implementation

Key technical details from implementation:

- app.py - Flask application with all routes
- security.py - Fernet + PBKDF2 credential encryption for GitHub PAT and API keys
- github_client.py - GitHub REST API v3 client for repo/branch operations
- ai_analyzer.py - Anthropic API integration for intelligent branch analysis
- models.py - Local storage for credentials and preferences
- static/css/style.css - Retro terminal UI aesthetic
- static/js/app.js - Frontend JavaScript for interactivity
- templates/ directory with 8 Jinja2 templates
- tests/test_app.py - 43 comprehensive tests (100% passing)
- Requirements: Flask, Anthropic, cryptography libraries
- Runs on localhost:5001
- Built from PDF specification (repodoctor2-spec-v4.pdf)
- ~5,100 lines of code across 20 files

---

## Architecture & Design Decisions

Key decisions made during development:

- Default to Claude Haiku 4.5 for cost efficiency (~$0.80/M tokens vs Sonnet/Opus)
- Allow model selection in settings with transparent pricing
- Plain English mode as default to reduce developer cognitive load
- Shorthand mode available for experienced users who prefer compact labels
- Assume sequential branch development (next builds on prior)
- 30-day archival period before deletion recommendations
- Guided onboarding to eliminate "empty dashboard" confusion
- Color-coded recommendations for quick visual scanning
- Encrypted credential storage using Fernet + PBKDF2
- Session-based authentication (not per-repo tokens)
- GitHub PAT with repo scope for full account access
- Local storage model for simplicity (no database dependency)

---

## Development History

Full session-by-session development history is maintained in `SESSION_NOTES.md`.

This specification is automatically updated alongside session notes to reflect:
- New features implemented
- Technical decisions made
- Architecture changes
- Integration updates

---

## Updating This Spec

At the end of each Claude Code session, this spec is updated automatically when you say:
> "Append session notes to SESSION_NOTES.md"

Claude will:
1. Update `SESSION_NOTES.md` with detailed session history
2. Update `repodoctor2-PRODUCT_SPEC.md` with new features/decisions
3. Commit both files together

**Never manually edit this file** - it's maintained automatically from session notes.
