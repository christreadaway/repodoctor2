"""
GitHub API client for RepDoctor2.
Handles all GitHub REST API v3 interactions.
"""

import datetime
import requests

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self, pat: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github.v3+json",
        })

    def verify_token(self) -> dict | None:
        """Verify the PAT and return user info, or None if invalid."""
        resp = self.session.get(f"{GITHUB_API}/user")
        if resp.status_code == 200:
            data = resp.json()
            scopes = resp.headers.get("X-OAuth-Scopes", "")
            data["_scopes"] = scopes
            return data
        return None

    def get_repos(self) -> list[dict]:
        """Fetch all repositories accessible to the authenticated user."""
        repos = []
        page = 1
        while True:
            resp = self.session.get(
                f"{GITHUB_API}/user/repos",
                params={
                    "per_page": 100,
                    "page": page,
                    "sort": "updated",
                    "direction": "desc",
                    "affiliation": "owner,collaborator",
                },
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            repos.extend(batch)
            page += 1
        return repos

    def get_branches(self, owner: str, repo: str) -> list[dict]:
        """Fetch all branches for a repo."""
        branches = []
        page = 1
        while True:
            resp = self.session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/branches",
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            branches.extend(batch)
            page += 1
        return branches

    def compare_branches(self, owner: str, repo: str, base: str, head: str) -> dict | None:
        """Compare two branches. Returns comparison data or None on error."""
        resp = self.session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}",
        )
        if resp.status_code == 200:
            return resp.json()
        return None

    def get_pulls(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        """Fetch pull requests for a repo."""
        resp = self.session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": 100},
        )
        if resp.status_code == 200:
            return resp.json()
        return []

    def get_tags(self, owner: str, repo: str) -> list[dict]:
        """Fetch tags for a repo."""
        tags = []
        page = 1
        while True:
            resp = self.session.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/tags",
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            tags.extend(batch)
            page += 1
        return tags

    def check_claude_md(self, owner: str, repo: str) -> bool:
        """Check if CLAUDE.md exists in the repo root."""
        resp = self.session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/CLAUDE.md",
        )
        return resp.status_code == 200

    def create_archive_tag(
        self, owner: str, repo: str, branch_name: str, commit_sha: str, message: str
    ) -> dict | None:
        """Create an archive tag for a branch."""
        today = datetime.date.today().isoformat()
        tag_name = f"archive/{branch_name}/{today}"

        # Create the tag object
        resp = self.session.post(
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
        ref_resp = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
            json={
                "ref": f"refs/tags/{tag_name}",
                "sha": tag_data["sha"],
            },
        )
        if ref_resp.status_code not in (200, 201):
            return None

        return {"tag_name": tag_name, "sha": commit_sha, "date": today}

    def get_default_branch_commits(self, owner: str, repo: str, default_branch: str, count: int = 10) -> list[dict]:
        """Get recent commits on the default branch."""
        resp = self.session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits",
            params={"sha": default_branch, "per_page": count},
        )
        if resp.status_code == 200:
            return resp.json()
        return []

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

        last_commit_date = None
        last_commit_author = None
        if comparison.get("commits"):
            last_commit = comparison["commits"][-1]
            last_commit_date = last_commit["commit"]["committer"]["date"]
            last_commit_author = (
                last_commit["commit"]["author"]["name"]
                if last_commit["commit"].get("author")
                else "Unknown"
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
