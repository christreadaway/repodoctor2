"""
Docs Explorer — quick browse + read of the Markdown files in each repo.

Lists every .md file in a repo (via the git tree API, one call), skipping
vendor/build dirs, and renders a chosen file to safe HTML with md_render. The
file list is grouped by folder (root first) so the common project docs
(README, PRODUCT_SPEC, PROJECT_STATUS, SESSION_NOTES, CLAUDE) are one click
away, and picks a sensible default file to show on first open.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MD_EXTS = (".md", ".markdown")

# Folders that are never project docs — a repo with node_modules can hide
# thousands of package READMEs that would bury the real docs.
_SKIP_SEGMENTS = {
    "node_modules", ".git", "venv", ".venv", "env", ".env", "__pycache__",
    "dist", "build", "target", "vendor", "site-packages", ".next", ".nuxt",
    ".cache", "coverage", ".tox", "bower_components",
}

# Root docs surfaced first, in this order, when present.
_PREFERRED = (
    "README.md", "PRODUCT_SPEC.md", "PROJECT_STATUS.md", "SESSION_NOTES.md",
    "CLAUDE.md", "PROJECT_RULES.md",
)


def _is_md(path: str) -> bool:
    return path.lower().endswith(MD_EXTS)


def _is_skipped(path: str) -> bool:
    parts = path.split("/")
    return any(p in _SKIP_SEGMENTS for p in parts[:-1])


def _file_entry(path: str) -> dict:
    parts = path.split("/")
    directory = "/".join(parts[:-1])
    return {
        "path": path,
        "name": parts[-1],
        "dir": directory,          # "" for root
        "depth": len(parts) - 1,
    }


def _sort_key(entry: dict):
    """Root files first (preferred docs ahead of the rest), then by folder,
    then by filename — all case-insensitively."""
    is_root = entry["dir"] == ""
    name_upper = entry["name"].upper()
    pref = _PREFERRED.index(entry["name"]) if entry["name"] in _PREFERRED else len(_PREFERRED)
    return (
        0 if is_root else 1,
        entry["dir"].lower(),
        pref,
        name_upper,
    )


def list_md_files(client, owner: str, name: str, ref: str | None = None) -> list[dict]:
    """Every Markdown file in the repo at `ref`, vendor dirs excluded, sorted
    root-first. Returns [] on any tree-fetch failure (logged)."""
    try:
        paths = client.get_all_file_paths(owner, name, ref=ref)
    except Exception as e:  # noqa: BLE001 - a bad tree fetch shouldn't 500 the page
        logger.warning("docs: tree fetch failed for %s/%s@%s: %s", owner, name, ref, e)
        return []
    entries = [_file_entry(p) for p in paths if _is_md(p) and not _is_skipped(p)]
    entries.sort(key=_sort_key)
    return entries


def pick_default(files: list[dict]) -> str | None:
    """The file to show when none is selected: a preferred root doc if present,
    otherwise the first file in sorted order."""
    if not files:
        return None
    by_path = {f["path"]: f for f in files}
    for name in _PREFERRED:
        if name in by_path:
            return name
    return files[0]["path"]


def group_by_dir(files: list[dict]) -> list[dict]:
    """Group files by folder for the sidebar: [{'label','dir','files'}], root
    ('/') first, then folders alphabetically."""
    groups: dict[str, list[dict]] = {}
    for f in files:
        groups.setdefault(f["dir"], []).append(f)

    ordered: list[dict] = []
    if "" in groups:
        ordered.append({"label": "/", "dir": "", "files": groups.pop("")})
    for directory in sorted(groups, key=str.lower):
        ordered.append({"label": directory + "/", "dir": directory, "files": groups[directory]})
    return ordered


def is_allowed_path(path: str, files: list[dict]) -> bool:
    """Defense in depth: only render a path that is actually in the repo's md
    listing, so a crafted ?path= can't fetch something unexpected."""
    return any(f["path"] == path for f in files)
