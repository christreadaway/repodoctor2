"""
GitHub API client for RepoDoctor.
Handles all GitHub REST API v3 interactions.
"""

import datetime
import logging
import time
import requests

GITHUB_API = "https://api.github.com"
logger = logging.getLogger(__name__)


class GitHubAuthError(Exception):
    """Raised when GitHub returns 401 Unauthorized.

    Surfaces as a clear remediation message in the UI: the PAT is no longer
    valid (revoked, expired, or wrong scopes) and the user needs to reset
    their stored credentials.
    """


class GitHubClient:
    def __init__(self, pat: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github.v3+json",
        })

    # Default (connect, read) timeout for every GitHub call. Without this,
    # one stalled connection hangs a scan (or leaks a ThreadPool worker on
    # the Stats/Firestore pages) forever with no error.
    REQUEST_TIMEOUT = (10, 30)

    @staticmethod
    def _rate_limit_wait(resp: requests.Response) -> int:
        """Seconds to wait before retrying a rate-limited response."""
        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(max(1, int(retry_after)), 60)
        reset_at = resp.headers.get("X-RateLimit-Reset")
        if reset_at:
            try:
                return min(max(1, int(reset_at) - int(time.time()) + 1), 60)
            except ValueError:
                pass
        return 5

    def _get(self, url: str, raise_on_auth_error: bool = True, **kwargs) -> requests.Response:
        """Make a GET request with rate-limit retry.

        Raises GitHubAuthError on 401 by default so auth failures surface as
        a clear UI message instead of silently producing empty results across
        every endpoint that calls into the client. Use raise_on_auth_error=False
        for probes like verify_token() that intentionally inspect 401s.
        """
        kwargs.setdefault("timeout", self.REQUEST_TIMEOUT)
        resp = self.session.get(url, **kwargs)
        # If rate-limited, wait and retry once. GitHub signals primary rate
        # limits as 429 and secondary/abuse limits as 403 with "rate limit"
        # in the body — handle both.
        if resp.status_code == 429 or (
            resp.status_code == 403 and "rate limit" in resp.text.lower()
        ):
            wait = self._rate_limit_wait(resp)
            logger.warning("GitHub rate limit hit (HTTP %d), waiting %ds", resp.status_code, wait)
            time.sleep(wait)
            resp = self.session.get(url, **kwargs)
        if raise_on_auth_error and resp.status_code == 401:
            raise GitHubAuthError(
                f"GitHub returned 401 Unauthorized for {url}. "
                "Personal Access Token is invalid, expired, or missing required scopes."
            )
        return resp

    def _post(self, url: str, json: dict) -> requests.Response:
        """POST with the same timeout and 401→GitHubAuthError handling as
        _get, so write endpoints can't hang forever or silently swallow a
        dead PAT."""
        resp = self.session.post(url, json=json, timeout=self.REQUEST_TIMEOUT)
        if resp.status_code == 401:
            raise GitHubAuthError(
                f"GitHub returned 401 Unauthorized for {url}. "
                "Personal Access Token is invalid, expired, or missing required scopes."
            )
        return resp

    def _get_paginated(self, url: str, label: str, params: dict | None = None,
                       max_pages: int | None = None) -> list[dict]:
        """Page through a list endpoint until an empty page. Non-200 pages
        are logged (a silent break would truncate results invisibly)."""
        items: list[dict] = []
        page = 1
        while max_pages is None or page <= max_pages:
            merged = {"per_page": 100, "page": page, **(params or {})}
            resp = self._get(url, params=merged)
            if resp.status_code != 200:
                logger.warning("%s page %d failed: HTTP %d — returning %d items fetched so far",
                               label, page, resp.status_code, len(items))
                break
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def verify_token(self) -> dict | None:
        """Verify the PAT and return user info, or None if invalid."""
        resp = self._get(f"{GITHUB_API}/user", raise_on_auth_error=False)
        if resp.status_code == 200:
            data = resp.json()
            scopes = resp.headers.get("X-OAuth-Scopes", "")
            data["_scopes"] = scopes
            return data
        return None

    def get_repos(self) -> list[dict]:
        """Fetch all repositories accessible to the authenticated user.

        Raises GitHubAuthError (via _get) if GitHub returns 401 so the caller
        can show a clear remediation message instead of silently returning
        an empty list.
        """
        return self._get_paginated(
            f"{GITHUB_API}/user/repos", "get_repos",
            params={"sort": "updated", "direction": "desc",
                    "affiliation": "owner,collaborator"},
        )

    def get_branches(self, owner: str, repo: str) -> list[dict]:
        """Fetch all branches for a repo."""
        return self._get_paginated(
            f"{GITHUB_API}/repos/{owner}/{repo}/branches",
            f"get_branches {owner}/{repo}",
        )

    def compare_branches(self, owner: str, repo: str, base: str, head: str) -> dict | None:
        """Compare two branches. Returns comparison data or None on error."""
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}",
        )
        if resp.status_code == 200:
            return resp.json()
        return None

    def get_pulls(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        """Fetch all pull requests for a repo (paginated)."""
        return self._get_paginated(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            f"get_pulls {owner}/{repo}", params={"state": state},
        )

    def get_tags(self, owner: str, repo: str) -> list[dict]:
        """Fetch tags for a repo."""
        return self._get_paginated(
            f"{GITHUB_API}/repos/{owner}/{repo}/tags",
            f"get_tags {owner}/{repo}",
        )

    def check_claude_md(self, owner: str, repo: str) -> bool:
        """Check if CLAUDE.md exists in the repo root."""
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/CLAUDE.md",
        )
        return resp.status_code == 200

    def get_file_content(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        """Fetch the text content of a file from the repo. Returns None if not found."""
        import base64
        from urllib.parse import quote
        params = {}
        if ref:
            params["ref"] = ref
        # Preserve path separators but encode spaces, '#', '?', etc.
        encoded_path = quote(path, safe="/")
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{encoded_path}",
            params=params,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        # A directory path returns a JSON array, not an object; guard the shape
        # so .get() doesn't raise AttributeError on a list.
        if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
            try:
                return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            except Exception:
                return None
        return None

    def get_root_files(self, owner: str, repo: str, ref: str | None = None) -> list[str]:
        """Fetch the list of file/dir names in the repo root."""
        params = {}
        if ref:
            params["ref"] = ref
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents",
            params=params,
        )
        if resp.status_code != 200:
            logger.warning("get_root_files failed for %s/%s (ref=%s): HTTP %d", owner, repo, ref, resp.status_code)
            return []
        data = resp.json()
        if not isinstance(data, list):
            logger.warning("get_root_files for %s/%s returned non-list: %s", owner, repo, type(data).__name__)
            return []
        names = [item["name"] for item in data if isinstance(item, dict)]
        logger.debug("get_root_files %s/%s: found %d items: %s", owner, repo, len(names), names)
        return names

    def get_all_file_paths(self, owner: str, repo: str, ref: str | None = None) -> list[str]:
        """Return every file path in the repo at ref (recursive).

        Uses the git trees recursive API so subfolders are included.
        """
        tree_ref = ref or "HEAD"
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{tree_ref}",
            params={"recursive": "1"},
        )
        if resp.status_code != 200:
            logger.warning("get_all_file_paths failed for %s/%s (ref=%s): HTTP %d",
                           owner, repo, tree_ref, resp.status_code)
            return []
        data = resp.json()
        if data.get("truncated"):
            logger.warning("Tree for %s/%s is truncated; some deep files may be missed.",
                           owner, repo)
        return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

    # Directories we never want to treat as project-spec locations.
    _SKIP_PATH_SEGMENTS = {
        "node_modules", ".git", "venv", ".venv", "env", ".env",
        "__pycache__", "dist", "build", "target", "vendor", "site-packages",
        ".next", ".nuxt", ".cache", "coverage", ".tox", "bower_components",
    }

    def _is_ignored_path(self, path: str) -> bool:
        parts = path.split("/")
        return any(p in self._SKIP_PATH_SEGMENTS for p in parts[:-1])

    def check_required_files(self, owner: str, repo: str, ref: str | None = None) -> tuple[dict[str, bool], dict[str, str]]:
        """Check which required project files exist anywhere in the repo.

        Searches recursively and prefers root-level matches, falling back to the
        shallowest/shortest subfolder path. Vendor/build dirs are skipped.

        Returns:
            (results, actual_names) where results maps display_name -> bool,
            and actual_names maps display_name -> full path (only for found files).
        """
        all_paths = self.get_all_file_paths(owner, repo, ref=ref)

        # Only these extensions count as project docs. Without this filter,
        # .github/workflows/claude.yml satisfies "CLAUDE.md", license.py
        # satisfies "LICENSE", etc. — and the YAML then gets fed into AI
        # prompts as the doc content.
        _DOC_EXTENSIONS = {".md", ".txt", ".rst", ""}

        # stem -> list of (path, depth)
        stem_to_paths: dict[str, list[tuple[str, int]]] = {}
        for path in all_paths:
            if self._is_ignored_path(path):
                continue
            filename = path.rsplit("/", 1)[-1]
            fl = filename.lower()
            dot = fl.rfind(".")
            stem = fl[:dot] if dot > 0 else fl
            ext = fl[dot:] if dot > 0 else ""
            if ext not in _DOC_EXTENSIONS:
                continue
            depth = path.count("/")
            stem_to_paths.setdefault(stem, []).append((path, depth))

        required = {
            "CLAUDE.md": ["claude"],
            "LICENSE": ["license"],
            # business_spec.md is treated as an equivalent product spec file.
            "PRODUCT_SPEC.md": ["product_spec", "business_spec"],
            "PROJECT_STATUS.md": ["project_status"],
            "SESSION_NOTES.md": ["session_notes"],
        }
        results: dict[str, bool] = {}
        actual_names: dict[str, str] = {}
        for display_name, stems in required.items():
            matches: list[tuple[str, int]] = []
            for stem in stems:
                matches.extend(stem_to_paths.get(stem, []))
            if matches:
                # Prefer shallowest, then shortest path string
                matches.sort(key=lambda m: (m[1], len(m[0])))
                results[display_name] = True
                actual_names[display_name] = matches[0][0]
            else:
                results[display_name] = False

        found_count = sum(1 for v in results.values() if v)
        logger.debug("check_required_files %s/%s: %d/%d found. results=%s",
                      owner, repo, found_count, len(required), actual_names)
        return results, actual_names

    def create_archive_tag(
        self, owner: str, repo: str, branch_name: str, commit_sha: str, message: str
    ) -> dict | None:
        """Create an archive tag for a branch."""
        today = datetime.date.today().isoformat()
        tag_name = f"archive/{branch_name}/{today}"

        # Create the tag object
        resp = self._post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/tags",
            json={
                "tag": tag_name,
                "message": message,
                "object": commit_sha,
                "type": "commit",
            },
        )
        if resp.status_code not in (200, 201):
            return None
        tag_data = resp.json()

        # Create the reference
        ref_resp = self._post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
            json={
                "ref": f"refs/tags/{tag_name}",
                "sha": tag_data["sha"],
            },
        )
        if ref_resp.status_code not in (200, 201):
            return None

        return {"tag_name": tag_name, "sha": commit_sha, "date": today}

    def get_last_commit_for_path(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        """Get the ISO timestamp of the most recent commit that touched the given file path.
        Returns None if the file has no commits or doesn't exist."""
        params = {"path": path, "per_page": 1}
        if ref:
            params["sha"] = ref
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits",
            params=params,
        )
        if resp.status_code == 200:
            commits = resp.json()
            if commits:
                commit = commits[0].get("commit") or {}
                committer = commit.get("committer") or {}
                return committer.get("date")
        return None

    def get_last_commit_date(self, owner: str, repo: str, ref: str | None = None) -> str | None:
        """Get the ISO timestamp of the most recent commit on the given branch."""
        params = {"per_page": 1}
        if ref:
            params["sha"] = ref
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits",
            params=params,
        )
        if resp.status_code == 200:
            commits = resp.json()
            if commits:
                commit = commits[0].get("commit") or {}
                committer = commit.get("committer") or {}
                return committer.get("date")
        return None

    def get_default_branch_commits(self, owner: str, repo: str, default_branch: str, count: int = 10) -> list[dict]:
        """Get recent commits on the default branch."""
        resp = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits",
            params={"sha": default_branch, "per_page": count},
        )
        if resp.status_code == 200:
            return resp.json()
        return []

    def get_language_bytes(self, owner: str, repo: str) -> dict[str, int]:
        """Return {language: byte_count} as reported by GitHub.

        We treat the sum of bytes as a proxy for "code size". It's not
        line-count, but it's a cheap single-call metric per repo.
        """
        resp = self._get(f"{GITHUB_API}/repos/{owner}/{repo}/languages")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
        return {}

    def get_commits_since(self, owner: str, repo: str, since_iso: str,
                          ref: str | None = None, max_pages: int = 3) -> list[dict]:
        """Fetch commits on `ref` since the given ISO timestamp.

        Returns up to max_pages * 100 commits. Each commit dict contains at
        minimum a `commit.author.date` / `commit.committer.date`.
        """
        params: dict = {"since": since_iso}
        if ref:
            params["sha"] = ref
        return self._get_paginated(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits",
            f"get_commits_since {owner}/{repo}", params=params, max_pages=max_pages,
        )

    def branch_last_commit(self, owner: str, repo: str, branch: str,
                           comparison: dict) -> tuple[str | None, str | None]:
        """(date, author) of the newest commit on a compared branch.

        The compare API returns at most 250 commits, oldest-first — for
        bigger branches commits[-1] is NOT the tip, which would misclassify
        an actively-developed branch as STALE. When truncated, fetch the
        real tip date. Shared by scan_repo and the Henry page.
        """
        commits = comparison.get("commits") or []
        if not commits:
            return None, None
        lc_commit = commits[-1].get("commit", {})
        committer = lc_commit.get("committer") or {}
        date = committer.get("date")
        author = (lc_commit.get("author") or {}).get("name", "Unknown")
        if comparison.get("total_commits", 0) > len(commits):
            try:
                tip_date = self.get_last_commit_date(owner, repo, ref=branch)
                if tip_date:
                    date = tip_date
            except GitHubAuthError:
                raise
            except Exception as e:
                logger.warning("branch tip-date fetch failed for %s/%s@%s: %s — "
                               "using the (older) compare-API date", owner, repo, branch, e)
        return date, author

    def classify_branch(self, comparison: dict, last_commit_date: str | None, has_pr: bool) -> str:
        """Classify a branch based on comparison data."""
        ahead = comparison.get("ahead_by", 0)
        behind = comparison.get("behind_by", 0)

        if has_pr:
            return "ACTIVE PR"

        if ahead == 0:
            return "SAFE TO DELETE"

        # Check staleness (> 30 days)
        if last_commit_date:
            try:
                last_dt = datetime.datetime.fromisoformat(last_commit_date.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                if (now - last_dt).days > 30 and ahead > 0:
                    return "STALE"
            except (ValueError, TypeError):
                pass

        if ahead > 0 and behind > 0:
            return "DIVERGED"

        if ahead > 0 and behind == 0:
            return "AHEAD ONLY"

        return "AHEAD ONLY"


# The four spec docs every AI feature reads, mapped to the prompt keys
# used by project summaries, the tracker generator, and chat briefs.
DOC_KEYS = {
    "PRODUCT_SPEC.md": "product_spec",
    "PROJECT_STATUS.md": "project_status",
    "SESSION_NOTES.md": "session_notes",
    "CLAUDE.md": "claude",
}

README_VARIANTS = ("README.md", "readme.md", "README.rst", "README")


def fetch_repo_docs(
    client: GitHubClient,
    owner: str,
    repo: str,
    ref: str,
    max_chars: int,
    actual_paths: dict | None = None,
    include_readme: str = "never",
) -> dict:
    """Fetch the spec docs (and optionally the README) one repo at a time —
    the single doc-fetch path shared by project summaries, the tracker
    generator, and chat briefs, so truncation, recursive-path lookup, and
    fallback behavior stay identical everywhere.

    include_readme: "never" | "always" | "if_no_docs".
    Returns {"docs": {key: text}, "readme": str} with each text truncated
    to max_chars. Individual fetch failures are logged and skipped.
    """
    docs: dict[str, str] = {}

    # Recursive search so specs in subfolders are found. Callers that
    # already ran check_required_files pass actual_paths to skip the
    # second tree walk.
    if actual_paths is None:
        _, actual_paths = client.check_required_files(owner, repo, ref=ref)

    for filename, key in DOC_KEYS.items():
        path = actual_paths.get(filename)
        if not path:
            continue
        try:
            content = client.get_file_content(owner, repo, path, ref=ref)
        except GitHubAuthError:
            raise
        except Exception as e:
            logger.warning("doc fetch failed: %s in %s/%s: %s", path, owner, repo, e)
            continue
        if content:
            docs[key] = content[:max_chars]

    readme = ""
    if include_readme == "always" or (include_readme == "if_no_docs" and not docs):
        for readme_name in README_VARIANTS:
            try:
                content = client.get_file_content(owner, repo, readme_name, ref=ref)
            except GitHubAuthError:
                raise
            except Exception:
                continue
            if content:
                readme = content[:max_chars]
                break

    return {"docs": docs, "readme": readme}


def scan_repo_lite(client: GitHubClient, repo: dict) -> dict:
    """Lightweight scan: branch count, required file checks, code size."""
    owner = repo["owner"]["login"]
    name = repo["name"]
    default_branch = repo.get("default_branch", "main")

    branches = client.get_branches(owner, name)
    total_branch_count = len(branches)
    non_default_count = total_branch_count - 1 if total_branch_count > 0 else 0

    # Branches with "henry" in the name are excluded from the dashboard count
    # (per Chris's preference). They're still listed in branch_names so the
    # Henry page can find them, but they don't inflate the per-repo or
    # cross-repo totals on My Repos. Default branch is never treated as
    # "henry" even if its name happens to contain the substring.
    henry_branch_count = sum(
        1 for b in branches
        if "henry" in b["name"].lower() and b["name"] != default_branch
    )
    non_henry_branch_count = total_branch_count - henry_branch_count

    required_files, actual_names = client.check_required_files(owner, name, ref=default_branch)

    # Code size via /languages (bytes per language).
    languages = client.get_language_bytes(owner, name)
    code_size_bytes = sum(languages.values())

    # Check if SESSION_NOTES.md and PRODUCT_SPEC.md are up to date.
    # "Up to date" = docs updated within 7 days of latest repo activity.
    # "Stale" = docs last updated more than 7 days before the latest commit.
    docs_updated = None  # None = can't determine / docs not present, True = yes, False = no
    has_product_spec = required_files.get("PRODUCT_SPEC.md", False)
    has_session_notes = required_files.get("SESSION_NOTES.md", False)

    doc_filenames = set()
    if has_session_notes:
        doc_filenames.add(actual_names.get("SESSION_NOTES.md", "SESSION_NOTES.md"))
    if has_product_spec:
        doc_filenames.add(actual_names.get("PRODUCT_SPEC.md", "PRODUCT_SPEC.md"))

    if doc_filenames:
        # Get the latest commit on the default branch (any file)
        latest_commit_ts = client.get_last_commit_date(owner, name, ref=default_branch)

        if latest_commit_ts:
            latest_commit_dt = datetime.datetime.fromisoformat(latest_commit_ts.replace("Z", "+00:00"))
            staleness_threshold = datetime.timedelta(days=7)
            all_fresh = True

            for real_name in doc_filenames:
                doc_ts = client.get_last_commit_for_path(owner, name, real_name, ref=default_branch)
                if doc_ts:
                    doc_dt = datetime.datetime.fromisoformat(doc_ts.replace("Z", "+00:00"))
                    # Stale only if doc was updated more than 7 days before the latest commit.
                    if doc_dt < (latest_commit_dt - staleness_threshold):
                        all_fresh = False
                else:
                    all_fresh = False

            docs_updated = all_fresh

    return {
        "owner": owner,
        "name": name,
        "full_name": repo["full_name"],
        "default_branch": default_branch,
        "private": repo.get("private", False),
        "html_url": repo.get("html_url", ""),
        "description": repo.get("description", ""),
        "created_at": repo.get("created_at", ""),
        "updated_at": repo.get("updated_at", ""),
        # pushed_at is the actual last-push timestamp; updated_at only tracks
        # metadata changes (stars, settings) and does NOT change on push.
        "pushed_at": repo.get("pushed_at", ""),
        "total_branch_count": total_branch_count,
        "non_default_branch_count": non_default_count,
        "henry_branch_count": henry_branch_count,
        "non_henry_branch_count": non_henry_branch_count,
        "branch_names": [b["name"] for b in branches],
        "required_files": required_files,
        "files_present": sum(1 for v in required_files.values() if v),
        "files_total": len(required_files),
        "docs_updated": docs_updated,
        "code_size_bytes": code_size_bytes,
        "languages": languages,
    }


def scan_repo(client: GitHubClient, repo: dict) -> dict:
    """Scan a single repo and return structured branch data."""
    owner = repo["owner"]["login"]
    name = repo["name"]
    default_branch = repo.get("default_branch", "main")

    branches = client.get_branches(owner, name)
    pulls = client.get_pulls(owner, name)
    pr_branches = {pr["head"]["ref"] for pr in pulls}

    branch_data = []
    for branch in branches:
        bname = branch["name"]
        if bname == default_branch:
            continue

        comparison = client.compare_branches(owner, name, default_branch, bname)
        if comparison is None:
            continue

        last_commit_date, last_commit_author = client.branch_last_commit(
            owner, name, bname, comparison,
        )

        has_pr = bname in pr_branches
        classification = client.classify_branch(comparison, last_commit_date, has_pr)

        files_changed = []
        for f in comparison.get("files", []):
            files_changed.append({
                "filename": f["filename"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "status": f["status"],
            })

        commit_messages = []
        for c in comparison.get("commits", []):
            commit_messages.append({
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0],
                "author": c["commit"]["author"]["name"] if c["commit"].get("author") else "Unknown",
                "date": c["commit"]["committer"]["date"] if c["commit"].get("committer") else None,
            })

        branch_data.append({
            "name": bname,
            "classification": classification,
            "ahead_by": comparison.get("ahead_by", 0),
            "behind_by": comparison.get("behind_by", 0),
            "last_commit_date": last_commit_date,
            "last_commit_author": last_commit_author,
            "has_pr": has_pr,
            "commit_sha": branch["commit"]["sha"],
            "files_changed": files_changed,
            "commit_messages": commit_messages,
        })

    # Sort: DIVERGED > AHEAD ONLY > STALE > SAFE TO DELETE > ACTIVE PR
    sort_order = {"DIVERGED": 0, "AHEAD ONLY": 1, "STALE": 2, "SAFE TO DELETE": 3, "ACTIVE PR": 4}
    branch_data.sort(key=lambda b: sort_order.get(b["classification"], 5))

    has_claude_md = client.check_claude_md(owner, name)

    return {
        "owner": owner,
        "name": name,
        "full_name": repo["full_name"],
        "default_branch": default_branch,
        "private": repo.get("private", False),
        "html_url": repo.get("html_url", ""),
        "description": repo.get("description", ""),
        "updated_at": repo.get("updated_at", ""),
        "branches": branch_data,
        "branch_count": len(branch_data),
        "has_claude_md": has_claude_md,
    }
