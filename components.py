"""
Components Inventory — which repo has which reusable capability, and which
repo is the best source to extract it from.

The owner wants to pull shared infrastructure (auth, logging, email/SMS,
testing, Claude docs management, deployment config, ...) out of ~50 repos
into federated component repos. Step one is knowing where each capability
already lives. This module answers that from the file tree of each repo.

Honest limitation: evidence is PATH-BASED ONLY. We fetch each repo's file
listing (one git-tree API call) and match filenames and folders against
regexes. We never read file contents, so a repo that uses Stripe only via a
`package.json` dependency with no stripe-named file will NOT register a hit.
A "hit" means "a file path in this repo looks like it belongs to this
capability." That is good enough to rank extraction candidates; it is not a
dependency audit. Pure module: no Flask, no network, no file I/O.
"""

from __future__ import annotations

import logging
import re

import github_client as gh

logger = logging.getLogger(__name__)

MAX_EVIDENCE = 8

# Folders that are never the owner's own code. A vendored node_modules tree
# would otherwise light up every capability at once.
SKIP_SEGMENTS = {
    "node_modules", ".git", "venv", ".venv", "env", ".env", "__pycache__",
    "dist", "build", "target", "vendor", "site-packages", ".next", ".nuxt",
    ".cache", "coverage", ".tox", "bower_components", ".pytest_cache",
    ".mypy_cache", ".parcel-cache", "out",
}

# Owner's buckets, in display order.
GROUPS = [
    "Marketing / GTM",
    "Design / Usability",
    "BI / Data Viz",
    "Testing",
    "Security",
    "Claude Docs Management",
    "Communications",
    "APIs & Integrations",
    "Media / Video",
    "Courseware / Learning",
    "Platform & Ops",
]

# Each capability: key, label, group, description, patterns (regexes applied
# to the lowercased, vendor-filtered file path). Patterns deliberately anchor
# on path segments or filenames so "auth" doesn't match "author.md".
CAPABILITIES: list[dict] = [
    # --- Marketing / GTM ---
    {
        "key": "marketing_gtm",
        "label": "Marketing / GTM",
        "group": "Marketing / GTM",
        "description": "Landing pages, SEO, sitemaps, analytics tags, pricing pages, newsletters.",
        "patterns": [
            r"(^|/)(landing|marketing|site|www|pricing|newsletter|newsletters|seo)(/|\.html?$|\.jsx?$|\.tsx?$|\.md$)",
            r"(^|/)(sitemap(\.xml)?|robots\.txt|og-image\.[a-z]+|opengraph[^/]*)$",
            r"(^|/)(pricing|landing|waitlist|launch)[^/]*\.(html?|jsx?|tsx?|md)$",
            r"(^|/)(analytics|gtag|gtm|plausible|posthog|segment)[^/]*\.(js|ts|py|html)$",
            r"(^|/)(email-templates|newsletter)[^/]*/",
        ],
    },
    # --- Design / Usability ---
    {
        "key": "design_system",
        "label": "Design System",
        "group": "Design / Usability",
        "description": "Tailwind config, design tokens, styles folders, shared component libraries.",
        "patterns": [
            r"(^|/)tailwind\.config\.[a-z]+$",
            r"(^|/)postcss\.config\.[a-z]+$",
            r"(^|/)(tokens|design-tokens|design_tokens|theme|themes)(/|\.(css|scss|json|js|ts)$)",
            r"(^|/)(styles?|scss|sass|css)/[^/]+\.(css|scss|sass|less)$",
            r"(^|/)(components|ui)/[^/]+\.(jsx|tsx|vue|svelte)$",
            r"(^|/)(design[-_]system|storybook|\.storybook)/",
            r"(^|/)[^/]*\.stories\.(jsx?|tsx?)$",
        ],
    },
    # --- BI / Data Viz ---
    {
        "key": "data_viz",
        "label": "Data Visualization",
        "group": "BI / Data Viz",
        "description": "Charts and dashboards: Chart.js, Recharts, Plotly, D3, matplotlib.",
        "patterns": [
            r"(^|/)[^/]*(chart|charts|chartjs|recharts|plotly|d3)[^/]*\.(js|ts|jsx|tsx|py|html)$",
            r"(^|/)(charts?|dashboards?|viz|visualizations?|reports?)/",
            r"(^|/)[^/]*(matplotlib|seaborn|dashboard)[^/]*\.(py|ipynb)$",
            r"(^|/)[^/]*\.ipynb$",
        ],
    },
    # --- Testing ---
    {
        "key": "testing",
        "label": "Testing",
        "group": "Testing",
        "description": "pytest, Jest, Vitest, Playwright, Cypress suites and their configs.",
        "patterns": [
            r"(^|/)(tests?|__tests__|spec|specs|e2e|cypress)/",
            r"(^|/)test_[^/]+\.py$",
            r"(^|/)[^/]+_test\.py$",
            r"(^|/)[^/]+\.(test|spec)\.(jsx?|tsx?|mjs)$",
            r"(^|/)(pytest\.ini|conftest\.py|tox\.ini|setup\.cfg)$",
            r"(^|/)(jest|vitest|playwright|cypress)\.config\.[a-z]+$",
        ],
    },
    # --- Security ---
    {
        "key": "security",
        "label": "Security",
        "group": "Security",
        "description": "security.py, encryption, CSRF protection, rate limiting, helmet.",
        "patterns": [
            r"(^|/)security[^/]*\.(py|js|ts)$",
            r"(^|/)[^/]*(encrypt|crypto|csrf|ratelimit|rate_limit|rate-limit|helmet|sanitize|permissions?)[^/]*\.(py|js|ts)$",
            r"(^|/)(security|SECURITY)\.md$",
            r"(^|/)(firestore|storage|database)\.rules$",
            r"(^|/)\.github/dependabot\.ya?ml$",
        ],
    },
    # --- Claude Docs Management ---
    {
        "key": "claude_docs",
        "label": "Claude Docs Management",
        "group": "Claude Docs Management",
        "description": "CLAUDE.md, PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, .claude/ commands and skills.",
        "patterns": [
            r"(^|/)claude\.md$",
            r"(^|/)(product_spec|project_status|session_notes|project_rules|prd|requirements)[^/]*\.md$",
            r"(^|/)\.claude/(commands|skills|agents|hooks)/",
            r"(^|/)\.claude/settings[^/]*\.json$",
            r"(^|/)\.claude-plugin/",
            r"(^|/)(skill|skills)/[^/]+/skill\.md$",
        ],
    },
    # --- Communications ---
    {
        "key": "email",
        "label": "Email",
        "group": "Communications",
        "description": "Transactional email: SendGrid, Resend, Nodemailer, smtplib, Mailgun, Postmark.",
        "patterns": [
            r"(^|/)[^/]*(sendgrid|resend|nodemailer|mailgun|postmark|smtp|mailer|send_email|sendemail|send-email)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)(email|emails|mail)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)(email|emails|mail)/",
        ],
    },
    {
        "key": "sms",
        "label": "SMS",
        "group": "Communications",
        "description": "Text messaging via Twilio or Vonage.",
        "patterns": [
            r"(^|/)[^/]*(twilio|vonage|nexmo|send_sms|sendsms|send-sms)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)sms[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)sms/",
        ],
    },
    {
        "key": "comms_notifications",
        "label": "Notifications & Webhooks",
        "group": "Communications",
        "description": "Push notifications, inbound/outbound webhooks, Slack or Discord bots.",
        "patterns": [
            r"(^|/)[^/]*(notification|notifications|notify|webhook|webhooks|push_notif|pushnotif|slack|discord|fcm|onesignal)[^/]*\.(py|js|ts|mjs|json)$",
            r"(^|/)(notifications?|webhooks?)/",
            r"(^|/)(firebase-messaging-sw|service-worker|sw)\.js$",
        ],
    },
    # --- APIs & Integrations ---
    {
        "key": "auth",
        "label": "Auth & Sign-in",
        "group": "APIs & Integrations",
        "description": "Firebase Auth, Google Auth, Passport, NextAuth, Flask-Login, session handling.",
        "patterns": [
            r"(^|/)auth[^/]*\.(py|js|ts|jsx|tsx|mjs)$",
            r"(^|/)(auth|authentication|login|oauth)/",
            r"(^|/)[^/]*(firebase[-_]?auth|google[-_]?auth|passport|next-auth|nextauth|flask_login|flask-login|login|signin|sign-in|session_auth)[^/]*\.(py|js|ts|jsx|tsx|mjs)$",
            r"(^|/)\[\.\.\.nextauth\]/",
            r"(^|/)(login|signin|sign-in|register|signup)\.html$",
        ],
    },
    {
        "key": "firebase_firestore",
        "label": "Firebase / Firestore",
        "group": "APIs & Integrations",
        "description": "Firebase project config, Firestore rules and indexes, Cloud Functions.",
        "patterns": [
            r"(^|/)(firebase|\.firebaserc)[^/]*\.json$",
            r"(^|/)\.firebaserc$",
            r"(^|/)firestore\.(rules|indexes\.json)$",
            r"(^|/)[^/]*(firebase|firestore)[^/]*\.(py|js|ts|jsx|tsx|mjs)$",
            r"(^|/)functions/(index|main)\.(js|ts|py)$",
        ],
    },
    {
        "key": "database",
        "label": "Database",
        "group": "APIs & Integrations",
        "description": "SQLAlchemy, Prisma, Mongoose, Supabase, raw SQL and migrations.",
        "patterns": [
            r"(^|/)[^/]*\.sql$",
            r"(^|/)(prisma|migrations|alembic|supabase|drizzle)/",
            r"(^|/)schema\.prisma$",
            r"(^|/)(models?|db|database|schema|schemas)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)[^/]*(sqlalchemy|mongoose|supabase|sqlite|postgres|knex|drizzle)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)[^/]*\.(db|sqlite|sqlite3)$",
        ],
    },
    {
        "key": "payments",
        "label": "Payments",
        "group": "APIs & Integrations",
        "description": "Stripe or Square checkout, billing, and payment webhooks.",
        "patterns": [
            r"(^|/)[^/]*(stripe|square|checkout|billing|payment|payments|donate|donation)[^/]*\.(py|js|ts|jsx|tsx|mjs|html)$",
            r"(^|/)(payments?|billing|checkout)/",
        ],
    },
    {
        "key": "api_clients",
        "label": "API Clients",
        "group": "APIs & Integrations",
        "description": "Wrappers around external APIs: requests/axios/fetch clients, third-party SDK glue.",
        "patterns": [
            r"(^|/)[^/]*(_client|-client|client|api_client|apiclient|http|fetcher|fetch_)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)(api|apis|clients|integrations|services|lib/api)/",
            r"(^|/)[^/]*(github|google|calendar|sheets|drive|airtable|notion|zapier|hubspot|salesforce|mailchimp)[^/]*\.(py|js|ts|mjs)$",
        ],
    },
    {
        "key": "ai_integration",
        "label": "AI Integration",
        "group": "APIs & Integrations",
        "description": "OpenAI or Anthropic SDK usage, prompt files, AI analyzers and summarizers.",
        "patterns": [
            r"(^|/)[^/]*(openai|anthropic|claude|gpt|llm|ai_analyzer|analyzer|summarizer|embedding|embeddings)[^/]*\.(py|js|ts|mjs)$",
            r"(^|/)(prompts?|ai|llm)/",
            r"(^|/)[^/]*prompt[^/]*\.(py|js|ts|txt|md)$",
        ],
    },
    # --- Platform & Ops ---
    {
        "key": "logging",
        "label": "Logging",
        "group": "Platform & Ops",
        "description": "Logging config, Winston/Pino setup, data/logs folders, log rotation.",
        "patterns": [
            r"(^|/)(logging|logger|log|logs|winston|pino|log_config|logging_config)[^/]*\.(py|js|ts|mjs|json|ya?ml|conf|ini)$",
            r"(^|/)(logs?|data/logs?)/",
            r"(^|/)[^/]*\.log$",
            r"(^|/)(action_log|audit_log|error_log)[^/]*\.(py|js|ts|json)$",
        ],
    },
    {
        "key": "deployment",
        "label": "Deployment",
        "group": "Platform & Ops",
        "description": "netlify.toml, vercel.json, Dockerfile, fly.toml, firebase.json, GitHub Actions workflows.",
        "patterns": [
            r"(^|/)(netlify\.toml|vercel\.json|fly\.toml|render\.ya?ml|railway\.(json|toml)|app\.ya?ml|procfile|heroku\.yml)$",
            r"(^|/)(dockerfile|docker-compose[^/]*\.ya?ml|\.dockerignore)$",
            r"(^|/)firebase\.json$",
            r"(^|/)\.github/workflows/[^/]+\.ya?ml$",
            r"(^|/)(deploy|deployment)[^/]*\.(sh|ps1|py|js|ya?ml)$",
            r"(^|/)netlify/functions/",
        ],
    },
    {
        "key": "scheduling_automation",
        "label": "Scheduling & Automation",
        "group": "Platform & Ops",
        "description": "Cron jobs, scheduled functions, GitHub Actions on a schedule, background workers.",
        "patterns": [
            r"(^|/)[^/]*(cron|schedule|scheduler|scheduled|worker|workers|jobs?|tasks?|celery|apscheduler|queue)[^/]*\.(py|js|ts|mjs|ya?ml)$",
            r"(^|/)(cron|jobs|tasks|workers|scheduled)/",
            r"(^|/)\.github/workflows/[^/]*(nightly|daily|weekly|schedule|cron)[^/]*\.ya?ml$",
        ],
    },
    {
        "key": "pdf_docs_export",
        "label": "PDF & Document Export",
        "group": "Platform & Ops",
        "description": "ReportLab, pdfkit, Puppeteer-to-PDF, python-docx exports.",
        "patterns": [
            r"(^|/)[^/]*(pdf|reportlab|pdfkit|weasyprint|docx|export|exporter|printable)[^/]*\.(py|js|ts|mjs|html)$",
            r"(^|/)(pdf|pdfs|exports?)/",
        ],
    },
    {
        "key": "crm_contacts",
        "label": "CRM & Contacts",
        "group": "APIs & Integrations",
        "description": "Contact / people records, leads, pipelines, follow-up reminders.",
        "patterns": [
            r"(^|/)(crm|contacts?|leads?|pipeline|prospects?|people)/",
            r"(^|/)[^/]*(crm|contact|lead|prospect|followup|follow_up)[^/]*\.(py|js|ts|json|sql)$",
        ],
    },
    {
        "key": "privacy_redaction",
        "label": "Privacy & PII Redaction",
        "group": "Security",
        "description": "Anonymizing documents, PII detection and redaction, data minimization.",
        "patterns": [
            r"(^|/)[^/]*(anonymi[sz]|redact|pii|scrub|deidentif|sanitiz)[^/]*\.(py|js|ts|json|md)$",
            r"(^|/)(redaction|anonymi[sz]er|pii)/",
        ],
    },
    # --- Media / Video ---
    {
        "key": "video_production",
        "label": "Video Production",
        "group": "Media / Video",
        "description": "Remotion / ffmpeg render pipelines, video scripts, captions and subtitles.",
        "patterns": [
            r"(^|/)remotion\.config\.[a-z]+$",
            r"(^|/)(videos?|remotion|render|renders|clips|footage|storyboards?)/",
            r"(^|/)[^/]*(ffmpeg|remotion|video|storyboard|b-?roll)[^/]*\.(py|js|ts|tsx|jsx|sh|json|md)$",
            r"\.(srt|vtt)$",
        ],
    },
    {
        "key": "audio_transcription",
        "label": "Audio & Transcription",
        "group": "Media / Video",
        "description": "Recording, Whisper-style transcription, audio processing.",
        "patterns": [
            r"(^|/)(audio|transcripts?|transcriptions?|recordings?)/",
            r"(^|/)[^/]*(whisper|transcri|speech|diariz|audio)[^/]*\.(py|js|ts|json|md)$",
        ],
    },
    # --- Courseware / Learning ---
    {
        "key": "courseware",
        "label": "Courseware",
        "group": "Courseware / Learning",
        "description": "Lessons, curricula, standards alignment, quizzes, gradebooks.",
        "patterns": [
            r"(^|/)(lessons?|curriculum|curricula|courses?|coursework|syllabus|syllabi|quizzes|quiz|units|standards|gradebook)/",
            r"(^|/)[^/]*(lesson|curriculum|syllabus|quiz|rubric|gradebook|standards?)[^/]*\.(py|js|ts|json|md|html|csv)$",
            r"(^|/)imsmanifest\.xml$",
        ],
    },
    {
        "key": "scripts_launchers",
        "label": "Scripts & Launchers",
        "group": "Platform & Ops",
        "description": "start.command, start.ps1, Makefile, setup and install scripts.",
        "patterns": [
            r"(^|/)(start|run|setup|install|launch|bootstrap)[^/]*\.(command|ps1|sh|bat|cmd)$",
            r"(^|/)(makefile|justfile|taskfile\.ya?ml)$",
            r"(^|/)(scripts?|bin|tools)/[^/]+\.(sh|ps1|py|js|command|bat)$",
            r"(^|/)(setup_mac|setup_pc|run_local|get_latest[^/]*)\.md$",
        ],
    },
]

_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (cap["key"], [re.compile(p) for p in cap["patterns"]]) for cap in CAPABILITIES
]
_BY_KEY: dict[str, dict] = {cap["key"]: cap for cap in CAPABILITIES}


def capability(key: str) -> dict | None:
    return _BY_KEY.get(key)


def is_skipped(path: str) -> bool:
    """True when any directory segment of the path is a vendor/build dir."""
    parts = path.split("/")
    return any(p.lower() in SKIP_SEGMENTS for p in parts[:-1])


def detect_capabilities(file_paths: list[str]) -> dict[str, dict]:
    """Match every non-vendor path against every capability.

    Returns {key: {"hits": int, "evidence": [path, ...]}} for every capability
    (zero-hit capabilities included, so callers can iterate uniformly).
    Evidence keeps the first MAX_EVIDENCE matching paths in tree order.
    """
    result = {key: {"hits": 0, "evidence": []} for key, _ in _COMPILED}
    for raw in file_paths or []:
        if not isinstance(raw, str) or not raw:
            continue
        if is_skipped(raw):
            continue
        path = raw.lower()
        for key, regexes in _COMPILED:
            if any(rx.search(path) for rx in regexes):
                bucket = result[key]
                bucket["hits"] += 1
                if len(bucket["evidence"]) < MAX_EVIDENCE:
                    bucket["evidence"].append(raw)
    return result


def scan_repo_components(client, repo: dict) -> dict | None:
    """Fetch one repo's file tree and detect capabilities. Returns None when
    the tree couldn't be read (rate limit, 5xx) so the caller keeps that
    repo's previous detection instead of recording "has nothing". A dead PAT
    (GitHubAuthError) propagates to the app-wide reconnect handler."""
    owner = repo.get("owner", "")
    name = repo.get("name", "")
    ref = repo.get("default_branch") or None
    try:
        paths = client.get_all_file_paths(owner, name, ref=ref, strict=True)
    except gh.GitHubAuthError:
        raise
    except Exception as e:  # noqa: BLE001 - isolation is the whole point
        logger.warning("components: tree fetch failed for %s/%s@%s: %s", owner, name, ref, e)
        return None
    try:
        return detect_capabilities(paths or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("components: detection failed for %s/%s: %s", owner, name, e)
        return detect_capabilities([])


def _full_name(repo: dict) -> str:
    return repo.get("full_name") or f"{repo.get('owner', '')}/{repo.get('name', '')}"


def inventory(repos: list[dict], detections: dict[str, dict]) -> dict:
    """Combine per-repo detections into a capability-centric inventory.

    repos: the scan's repo dicts (owner, name, full_name, pushed_at, ...).
    detections: {full_name: detect_capabilities(...) result}.

    Returns:
      capabilities: [{key, label, group, description, repo_count, best_source,
                      repos: [{full_name, owner, name, hits, evidence, pushed_at}]}]
                    in CAPABILITIES order.
      groups: [{name, capabilities: [...]}] in GROUPS order (empty groups kept).
      repo_counts: [{full_name, owner, name, capability_count, capabilities}]
                   sorted by capability_count desc.
      coverage: {key: repo_count}.
      gaps: [capability dicts with zero repos].
      repos_scanned: number of repos with a detection entry.
    """
    by_name = {_full_name(r): r for r in repos or []}
    scanned = [fn for fn in by_name if fn in (detections or {})]

    cap_rows: list[dict] = []
    coverage: dict[str, int] = {}
    repo_counts: dict[str, dict] = {
        fn: {
            "full_name": fn,
            "owner": by_name[fn].get("owner", ""),
            "name": by_name[fn].get("name", ""),
            "capability_count": 0,
            "capabilities": [],
        }
        for fn in scanned
    }

    for cap in CAPABILITIES:
        key = cap["key"]
        entries = []
        for fn in scanned:
            det = (detections.get(fn) or {}).get(key) or {}
            hits = int(det.get("hits", 0) or 0)
            if hits <= 0:
                continue
            repo = by_name[fn]
            entries.append({
                "full_name": fn,
                "owner": repo.get("owner", ""),
                "name": repo.get("name", ""),
                "hits": hits,
                "evidence": list(det.get("evidence", []))[:MAX_EVIDENCE],
                "pushed_at": repo.get("pushed_at", "") or "",
            })
            repo_counts[fn]["capability_count"] += 1
            repo_counts[fn]["capabilities"].append(key)
        # Sort: hits desc, then most recently pushed first.
        entries.sort(key=lambda e: (-e["hits"], _neg_ts(e["pushed_at"]), e["full_name"]))
        coverage[key] = len(entries)
        cap_rows.append({
            "key": key,
            "label": cap["label"],
            "group": cap["group"],
            "description": cap["description"],
            "repo_count": len(entries),
            "best_source": entries[0] if entries else None,
            "others": entries[1:],
            "repos": entries,
        })

    groups = [
        {"name": g, "capabilities": [c for c in cap_rows if c["group"] == g]}
        for g in GROUPS
    ]
    gaps = [c for c in cap_rows if c["repo_count"] == 0]
    found = [c for c in cap_rows if c["repo_count"] > 0]

    rc_list = sorted(repo_counts.values(),
                     key=lambda r: (-r["capability_count"], r["full_name"]))

    return {
        "capabilities": cap_rows,
        "groups": groups,
        "repo_counts": rc_list,
        "coverage": coverage,
        "gaps": gaps,
        "repos_scanned": len(scanned),
        "capabilities_found": len(found),
        "extraction_order": suggest_extraction_order(cap_rows, len(scanned)),
    }


def _neg_ts(ts: str) -> str:
    """Sort helper: invert an ISO timestamp so newer sorts first under an
    ascending sort. Uses a character-wise complement so empty stays last."""
    if not ts:
        return "~"  # '~' sorts after any digit, so unknown dates go last
    return "".join(chr(0x7F - ord(c)) for c in ts)


def suggest_extraction_order(cap_rows: list[dict], repos_scanned: int) -> list[dict]:
    """Rank capabilities for extraction.

    Heuristic: score = repo_count x spread, where spread = repo_count /
    repos_scanned (the fraction of repos that have it). A capability living in
    many repos is both the most duplicated (biggest payoff) and the most
    battle-tested (lowest risk), so it should come out first. Zero-repo
    capabilities are excluded (nothing to extract).
    """
    total = max(repos_scanned, 1)
    ranked = []
    for c in cap_rows:
        n = c["repo_count"]
        if n == 0:
            continue
        spread = n / total
        ranked.append({
            "key": c["key"],
            "label": c["label"],
            "group": c["group"],
            "repo_count": n,
            "spread": round(spread, 2),
            "score": round(n * spread, 2),
            "best_source": c["best_source"]["full_name"] if c["best_source"] else None,
        })
    ranked.sort(key=lambda r: (-r["score"], -r["repo_count"], r["label"]))
    return ranked


def compose_markdown(inv: dict) -> str:
    """Paste-ready Component Inventory document."""
    lines = [
        "# Component Inventory",
        "",
        "> Generated by RepoDoctor from each repo's file tree. Evidence is",
        "> path-based only (no file contents were read), so treat a hit as",
        "> \"this repo has files that look like this capability,\" not as a",
        "> dependency audit.",
        "",
        f"Repos scanned: {inv.get('repos_scanned', 0)} · "
        f"Capabilities found: {inv.get('capabilities_found', 0)} · "
        f"Gaps: {len(inv.get('gaps', []))}",
        "",
    ]

    for group in inv.get("groups", []):
        lines += [f"## {group['name']}", ""]
        caps = group.get("capabilities", [])
        if not caps:
            lines += ["_No capabilities defined in this bucket._", ""]
            continue
        for cap in caps:
            lines.append(f"### {cap['label']}")
            lines.append(f"_{cap['description']}_")
            lines.append("")
            best = cap.get("best_source")
            if not best:
                lines += ["- **Best source:** none found (gap)", ""]
                continue
            lines.append(f"- **Best source:** `{best['full_name']}` ({best['hits']} hit(s))")
            if best.get("evidence"):
                lines.append("  - Evidence: " + ", ".join(f"`{p}`" for p in best["evidence"]))
            others = cap.get("others", [])
            if others:
                lines.append("- **Also in:** " + ", ".join(
                    f"`{o['full_name']}` ({o['hits']})" for o in others))
            else:
                lines.append("- **Also in:** no other repos")
            lines.append("")

    gaps = inv.get("gaps", [])
    lines += ["## Gaps (no repo has this yet)", ""]
    if gaps:
        for g in gaps:
            lines.append(f"- {g['label']} ({g['group']})")
    else:
        lines.append("- None. Every defined capability exists in at least one repo.")
    lines.append("")

    lines += [
        "## Suggested extraction order",
        "",
        "Heuristic: score = repos that have it x spread (fraction of all repos), "
        "so the most duplicated, most battle-tested capabilities come out first.",
        "",
    ]
    order = inv.get("extraction_order", [])
    if order:
        for i, r in enumerate(order, 1):
            src = f" · start from `{r['best_source']}`" if r.get("best_source") else ""
            lines.append(f"{i}. **{r['label']}**: {r['repo_count']} repo(s), "
                         f"spread {int(r['spread'] * 100)}%, score {r['score']}{src}")
    else:
        lines.append("_Nothing to extract yet. Run a components scan first._")
    lines.append("")
    return "\n".join(lines)
