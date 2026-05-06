"""
Firestore configuration detector.

For each repo, determines:
  - whether the project uses Firestore (deps / config files / spec mentions),
  - which Firestore-related files are present,
  - what's missing for a fully-configured site,
  - per-repo step-by-step instructions for finishing the setup.

Detection is based on the GitHub trees API and small, targeted file fetches
(package.json, requirements.txt, .firebaserc, firebase.json,
firestore.indexes.json). All work is read-only.
"""

import json
import logging

logger = logging.getLogger(__name__)


# Files we look for anywhere in the tree (shallowest match wins).
_FIRESTORE_FILENAMES = (
    "firebase.json",
    "firestore.rules",
    "firestore.indexes.json",
    ".firebaserc",
)

# Dep names that signal Firestore use.
_JS_FIRESTORE_PKGS = (
    "firebase",
    "firebase-admin",
    "firebase-functions",
    "firebase-tools",
    "@google-cloud/firestore",
    "@firebase/firestore",
)
_PY_FIRESTORE_PKGS = (
    "firebase-admin",
    "google-cloud-firestore",
)


def _find_first(paths: list[str], filename: str) -> str | None:
    """Return the shallowest path matching filename (case-insensitive),
    or None if not found. Vendor/build dirs are filtered upstream."""
    target = filename.lower()
    matches = []
    for p in paths:
        pl = p.lower()
        if pl == target or pl.endswith("/" + target):
            matches.append(p)
    if not matches:
        return None
    matches.sort(key=lambda p: (p.count("/"), len(p)))
    return matches[0]


def _filtered_paths(paths: list[str]) -> list[str]:
    """Drop vendor/build segments so we don't pick up Firebase configs from
    node_modules or vendored copies."""
    skip = {
        "node_modules", ".git", "venv", ".venv", "env", ".env",
        "__pycache__", "dist", "build", "target", "vendor", "site-packages",
        ".next", ".nuxt", ".cache", "coverage", ".tox", "bower_components",
    }
    out = []
    for p in paths:
        parts = p.split("/")
        if any(seg in skip for seg in parts[:-1]):
            continue
        out.append(p)
    return out


def _safe_json_loads(text: str) -> dict | list | None:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _extract_project_id(firebaserc_text: str) -> str | None:
    data = _safe_json_loads(firebaserc_text)
    if not isinstance(data, dict):
        return None
    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        return None
    pid = projects.get("default")
    if pid:
        return str(pid)
    for v in projects.values():
        if v:
            return str(v)
    return None


def _extract_hosting_site(firebase_json_text: str) -> str | None:
    data = _safe_json_loads(firebase_json_text)
    if not isinstance(data, dict):
        return None
    hosting = data.get("hosting")
    if isinstance(hosting, dict):
        return hosting.get("site") or hosting.get("target")
    if isinstance(hosting, list) and hosting:
        first = hosting[0]
        if isinstance(first, dict):
            return first.get("site") or first.get("target")
    return None


def _firebase_json_mentions_firestore(firebase_json_text: str) -> bool:
    data = _safe_json_loads(firebase_json_text)
    if isinstance(data, dict) and "firestore" in data:
        return True
    return "firestore" in firebase_json_text.lower()


def _count_indexes(indexes_json_text: str) -> int:
    data = _safe_json_loads(indexes_json_text)
    if isinstance(data, dict):
        idx = data.get("indexes")
        if isinstance(idx, list):
            return len(idx)
    return 0


def _matched_js_deps(package_json_text: str) -> list[str]:
    data = _safe_json_loads(package_json_text)
    if not isinstance(data, dict):
        # Fall back to substring scan
        return [p for p in _JS_FIRESTORE_PKGS if f'"{p}"' in package_json_text]
    deps: dict = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key) or {}
        if isinstance(section, dict):
            deps.update(section)
    return [p for p in _JS_FIRESTORE_PKGS if p in deps]


def _matched_py_deps(text: str) -> list[str]:
    if not text:
        return []
    low = text.lower()
    return [p for p in _PY_FIRESTORE_PKGS if p in low]


def detect_firestore_status(client, owner: str, name: str, ref: str) -> dict:
    """Run detection for a single repo. Returns the dict that the template
    consumes (also stored in the firestore-data cache)."""
    try:
        raw_paths = client.get_all_file_paths(owner, name, ref=ref)
    except Exception as e:
        logger.warning("firestore detect: get_all_file_paths failed for %s/%s: %s", owner, name, e)
        raw_paths = []
    paths = _filtered_paths(raw_paths)

    firebase_json_path = _find_first(paths, "firebase.json")
    firestore_rules_path = _find_first(paths, "firestore.rules")
    firestore_indexes_path = _find_first(paths, "firestore.indexes.json")
    firebaserc_path = _find_first(paths, ".firebaserc")
    package_json_path = _find_first(paths, "package.json")
    requirements_path = _find_first(paths, "requirements.txt")
    pyproject_path = _find_first(paths, "pyproject.toml")

    project_id: str | None = None
    site_domain: str | None = None
    indexes_count = 0
    firebase_json_has_firestore = False

    if firebaserc_path:
        content = client.get_file_content(owner, name, firebaserc_path, ref=ref) or ""
        project_id = _extract_project_id(content)

    if firebase_json_path:
        content = client.get_file_content(owner, name, firebase_json_path, ref=ref) or ""
        site_domain = _extract_hosting_site(content)
        firebase_json_has_firestore = _firebase_json_mentions_firestore(content)

    if firestore_indexes_path:
        content = client.get_file_content(owner, name, firestore_indexes_path, ref=ref) or ""
        indexes_count = _count_indexes(content)

    js_deps: list[str] = []
    if package_json_path:
        content = client.get_file_content(owner, name, package_json_path, ref=ref) or ""
        js_deps = _matched_js_deps(content)

    py_deps: list[str] = []
    if requirements_path or pyproject_path:
        text = ""
        if requirements_path:
            text += (client.get_file_content(owner, name, requirements_path, ref=ref) or "") + "\n"
        if pyproject_path:
            text += client.get_file_content(owner, name, pyproject_path, ref=ref) or ""
        py_deps = _matched_py_deps(text)

    indicators = []
    if firebase_json_path:
        suffix = " (firestore configured)" if firebase_json_has_firestore else ""
        indicators.append(f"firebase.json present{suffix}")
    if firestore_rules_path:
        indicators.append("firestore.rules present")
    if firestore_indexes_path:
        plural = "" if indexes_count == 1 else "es"
        indicators.append(f"firestore.indexes.json present ({indexes_count} index{plural})")
    if firebaserc_path:
        suffix = f" → {project_id}" if project_id else " (no project ID)"
        indicators.append(f".firebaserc present{suffix}")
    if js_deps:
        indicators.append(f"package.json deps: {', '.join(sorted(js_deps))}")
    if py_deps:
        indicators.append(f"Python deps: {', '.join(sorted(py_deps))}")

    # "Uses Firestore" if we have any direct Firestore artifact, OR firebase
    # configs/deps that strongly imply Firestore as part of the Firebase stack.
    direct = bool(firestore_rules_path or firestore_indexes_path or firebase_json_has_firestore)
    firebase_signal = bool(
        firebase_json_path or firebaserc_path or
        any(d in ("firebase", "firebase-admin", "firebase-functions", "firebase-tools",
                  "@google-cloud/firestore", "@firebase/firestore") for d in js_deps) or
        py_deps
    )
    uses_firestore = direct or firebase_signal

    missing: list[str] = []
    if not firebaserc_path or not project_id:
        missing.append("Firebase project not linked (.firebaserc)")
    if not firestore_rules_path:
        missing.append("firestore.rules (security rules)")
    if not firestore_indexes_path:
        missing.append("firestore.indexes.json (composite indexes)")
    if not firebase_json_path:
        missing.append("firebase.json (Firebase config)")
    if firebase_json_path and not firebase_json_has_firestore:
        missing.append('firebase.json has no "firestore" section')

    if uses_firestore:
        status = "needs_setup" if missing else "configured"
    else:
        status = "not_using"

    instructions = _build_instructions(
        repo_name=name,
        project_id=project_id,
        site_domain=site_domain,
        firebase_json_path=firebase_json_path,
        firestore_rules_path=firestore_rules_path,
        firestore_indexes_path=firestore_indexes_path,
        firebaserc_path=firebaserc_path,
        firebase_json_has_firestore=firebase_json_has_firestore,
        indexes_count=indexes_count,
    )

    return {
        "owner": owner,
        "name": name,
        "uses_firestore": uses_firestore,
        "status": status,
        "indicators": indicators,
        "missing": missing,
        "project_id": project_id,
        "site_domain": site_domain,
        "indexes_count": indexes_count,
        "files": {
            "firebase_json": firebase_json_path,
            "firestore_rules": firestore_rules_path,
            "firestore_indexes": firestore_indexes_path,
            "firebaserc": firebaserc_path,
        },
        "instructions": instructions,
    }


def _build_instructions(
    repo_name: str,
    project_id: str | None,
    site_domain: str | None,
    firebase_json_path: str | None,
    firestore_rules_path: str | None,
    firestore_indexes_path: str | None,
    firebaserc_path: str | None,
    firebase_json_has_firestore: bool,
    indexes_count: int,
) -> list[dict]:
    """Numbered, copy-pastable steps to finish Firestore setup for one repo.
    Each step is {title, detail}; details may contain console URLs and CLI commands."""
    steps: list[dict] = []

    suggested_id = repo_name.lower().replace("_", "-")

    # 1. Firebase project linked & exists
    if not firebaserc_path or not project_id:
        steps.append({
            "title": "Create / link a Firebase project",
            "detail": (
                f"Open https://console.firebase.google.com and click 'Add project'. "
                f"Suggested project ID: '{suggested_id}'. "
                f"Then from the repo root run: `firebase login` (once), then `firebase use --add` "
                f"to pick the project and write a .firebaserc file."
            ),
        })
    else:
        steps.append({
            "title": f"Confirm Firebase project: {project_id}",
            "detail": (
                f"Open https://console.firebase.google.com/project/{project_id}/overview "
                f"and confirm the project exists and you have access."
            ),
        })

    # 2. Enable Firestore Database (the manual console-only step)
    console_url = (
        f"https://console.firebase.google.com/project/{project_id}/firestore"
        if project_id
        else "https://console.firebase.google.com → your project → Firestore Database"
    )
    steps.append({
        "title": "Enable Firestore Database in the console",
        "detail": (
            f"Go to {console_url}. Click 'Create database'. "
            f"Pick **Native mode** (not Datastore mode), region **nam5 (us-central)** unless you "
            f"specifically need a different one, and start in **production mode**. "
            f"This is the manual step that can only be done in the Firebase console."
        ),
    })

    # 3. firebase.json with firestore section
    if not firebase_json_path:
        steps.append({
            "title": "Add firebase.json",
            "detail": (
                "Run `firebase init firestore` from repo root — picks the existing project, "
                "creates firebase.json with a `firestore` block, plus default firestore.rules "
                "and firestore.indexes.json. Commit all three."
            ),
        })
    elif not firebase_json_has_firestore:
        steps.append({
            "title": "Add a firestore section to firebase.json",
            "detail": (
                f"`{firebase_json_path}` exists but has no `firestore` key. Run `firebase init firestore` "
                f"again to add one (it'll merge), or hand-edit to add: "
                f'`"firestore": {{"rules": "firestore.rules", "indexes": "firestore.indexes.json"}}`.'
            ),
        })

    # 4. Security rules
    if firestore_rules_path:
        steps.append({
            "title": "Deploy security rules",
            "detail": (
                f"Rules file already in repo at `{firestore_rules_path}`. "
                f"Skim it once, then deploy: `firebase deploy --only firestore:rules`."
            ),
        })
    else:
        steps.append({
            "title": "Create and deploy security rules",
            "detail": (
                "Add `firestore.rules` at repo root. Start strict (deny by default, then "
                "allow-list per collection for authenticated users). Then run "
                "`firebase deploy --only firestore:rules`."
            ),
        })

    # 5. Indexes
    if firestore_indexes_path:
        plural = "" if indexes_count == 1 else "es"
        steps.append({
            "title": "Deploy composite indexes",
            "detail": (
                f"`{firestore_indexes_path}` defines {indexes_count} index{plural}. "
                f"Run: `firebase deploy --only firestore:indexes`."
            ),
        })
    else:
        steps.append({
            "title": "Indexes (only if you have compound queries)",
            "detail": (
                "Skip for now if your code only does single-field queries. If a compound query "
                "fails at runtime, Firestore prints a console URL that creates the missing index "
                "in one click — or pre-create `firestore.indexes.json` and run "
                "`firebase deploy --only firestore:indexes`."
            ),
        })

    # 6. Hosting (only if firebase.json has a hosting site)
    if site_domain and project_id:
        steps.append({
            "title": f"Verify hosting site: {site_domain}",
            "detail": (
                f"firebase.json defines a hosting site '{site_domain}'. Confirm it at "
                f"https://console.firebase.google.com/project/{project_id}/hosting/sites."
            ),
        })

    return steps
