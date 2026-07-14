/**
 * GitHub API client for RepoDoctor (Node.js port).
 * Uses native fetch (Node 18+).
 */

const GITHUB_API = 'https://api.github.com';

/**
 * Raised on GitHub 401 — mirrors the Python GitHubAuthError so routes can
 * show the PAT remediation message instead of a silent empty scan.
 */
class GitHubAuthError extends Error {}

// Directories we never treat as project-spec locations (mirrors Python).
const SKIP_SEGMENTS = new Set([
  'node_modules', '.git', 'venv', '.venv', 'env', '.env',
  '__pycache__', 'dist', 'build', 'target', 'vendor', 'site-packages',
  '.next', '.nuxt', '.cache', 'coverage', '.tox', 'bower_components',
]);

// Only doc-like extensions count — claude.yml must not satisfy CLAUDE.md.
const DOC_EXTENSIONS = new Set(['.md', '.txt', '.rst', '']);

// Same 5 required docs (and the business_spec alias) as the Python app.
const REQUIRED_DOCS = {
  'CLAUDE.md': ['claude'],
  'LICENSE': ['license'],
  'PRODUCT_SPEC.md': ['product_spec', 'business_spec'],
  'PROJECT_STATUS.md': ['project_status'],
  'SESSION_NOTES.md': ['session_notes'],
};

// The doc set every AI feature reads — mirrors Python's github_client.DOC_KEYS.
const DOC_KEYS = {
  'PRODUCT_SPEC.md': 'product_spec',
  'PROJECT_STATUS.md': 'project_status',
  'SESSION_NOTES.md': 'session_notes',
  'CLAUDE.md': 'claude',
};

/**
 * Match a list of file paths against REQUIRED_DOCS. Prefers shallowest,
 * then shortest path — same tiebreak as the Python client.
 */
function matchRequiredDocs(paths) {
  const stemToPaths = {};
  for (const p of paths) {
    const parts = p.split('/');
    if (parts.slice(0, -1).some(seg => SKIP_SEGMENTS.has(seg))) continue;
    const fl = parts[parts.length - 1].toLowerCase();
    const dot = fl.lastIndexOf('.');
    const stem = dot > 0 ? fl.substring(0, dot) : fl;
    const ext = dot > 0 ? fl.substring(dot) : '';
    if (!DOC_EXTENSIONS.has(ext)) continue;
    (stemToPaths[stem] = stemToPaths[stem] || []).push({ path: p, depth: parts.length - 1 });
  }

  const results = {};
  const actualNames = {};
  for (const [displayName, stems] of Object.entries(REQUIRED_DOCS)) {
    const matches = stems.flatMap(s => stemToPaths[s] || []);
    if (matches.length) {
      matches.sort((a, b) => a.depth - b.depth || a.path.length - b.path.length);
      results[displayName] = true;
      actualNames[displayName] = matches[0].path;
    } else {
      results[displayName] = false;
    }
  }
  return { results, actualNames };
}

/**
 * Fetch the spec docs for one repo — the shared doc-fetch path for the
 * repo-detail and summary routes (mirrors Python's fetch_repo_docs).
 * Pass actualNames (e.g. from a saved scan) to skip the lookup entirely.
 */
async function fetchRepoDocs(client, owner, repo, ref, { maxChars = 5000, actualNames = null, keys = DOC_KEYS } = {}) {
  if (!actualNames) {
    ({ actualNames } = await client.checkRequiredFiles(owner, repo, ref));
  }
  const docs = {};
  for (const [displayName, key] of Object.entries(keys)) {
    const p = actualNames[displayName];
    if (p) {
      const content = await client.getFileContent(owner, repo, p, ref);
      if (content) docs[key] = content.substring(0, maxChars);
    }
  }
  return docs;
}

class GitHubClient {
  constructor(pat) {
    this.headers = {
      Authorization: `token ${pat}`,
      Accept: 'application/vnd.github.v3+json',
    };
  }

  async _get(url, params = {}, { raiseOnAuthError = true } = {}) {
    const u = new URL(url);
    for (const [k, v] of Object.entries(params)) {
      u.searchParams.set(k, String(v));
    }
    let resp = await fetch(u.toString(), { headers: this.headers });
    // Rate-limit retry: GitHub uses 429 for primary limits and 403 with
    // "rate limit" in the body for secondary limits.
    if (resp.status === 429 || resp.status === 403) {
      const text = await resp.text();
      if (resp.status === 429 || text.toLowerCase().includes('rate limit')) {
        const retryAfter = parseInt(resp.headers.get('Retry-After') || '', 10);
        const resetAt = parseInt(resp.headers.get('X-RateLimit-Reset') || '', 10);
        let wait = 5;
        if (Number.isFinite(retryAfter)) {
          wait = Math.max(1, retryAfter);
        } else if (Number.isFinite(resetAt)) {
          wait = Math.max(1, resetAt - Math.floor(Date.now() / 1000) + 1);
        }
        // Netlify functions are hard-capped at 10s (free) / 26s (paid).
        // Sleeping a minute would burn the whole budget and still fail —
        // fail fast with the rate-limit response instead.
        if (wait > 8) {
          console.warn(`GitHub rate limited (HTTP ${resp.status}); reset in ~${wait}s — not retrying inside the function budget`);
          return resp;
        }
        await new Promise(r => setTimeout(r, wait * 1000));
        resp = await fetch(u.toString(), { headers: this.headers });
      }
    }
    // Explicit flag (like the Python client's raise_on_auth_error) rather
    // than a URL match — a repo literally named "user" must not suppress
    // the auth error.
    if (resp.status === 401 && raiseOnAuthError) {
      throw new GitHubAuthError(
        `GitHub returned 401 Unauthorized for ${u.pathname}. ` +
        'Personal Access Token is invalid, expired, or missing required scopes.'
      );
    }
    return resp;
  }

  async verifyToken() {
    // A probe: it inspects the 401 rather than throwing.
    const resp = await this._get(`${GITHUB_API}/user`, {}, { raiseOnAuthError: false });
    if (resp.status === 200) {
      const data = await resp.json();
      data._scopes = resp.headers.get('X-OAuth-Scopes') || '';
      return data;
    }
    return null;
  }

  async getRepos() {
    const repos = [];
    let page = 1;
    while (true) {
      const resp = await this._get(`${GITHUB_API}/user/repos`, {
        per_page: 100,
        page,
        sort: 'updated',
        direction: 'desc',
        affiliation: 'owner,collaborator',
      });
      if (resp.status !== 200) break;
      const batch = await resp.json();
      if (!batch.length) break;
      repos.push(...batch);
      page++;
    }
    return repos;
  }

  async getBranches(owner, repo) {
    const branches = [];
    let page = 1;
    while (true) {
      const resp = await this._get(`${GITHUB_API}/repos/${owner}/${repo}/branches`, {
        per_page: 100,
        page,
      });
      if (resp.status !== 200) break;
      const batch = await resp.json();
      if (!batch.length) break;
      branches.push(...batch);
      page++;
    }
    return branches;
  }

  async getFileContent(owner, repo, path, ref) {
    const params = {};
    if (ref) params.ref = ref;
    // Encode each segment so '#'/'?' in filenames can't truncate the URL,
    // while preserving '/' separators (mirrors Python's quote(path, safe='/')).
    const encodedPath = path.split('/').map(encodeURIComponent).join('/');
    const resp = await this._get(`${GITHUB_API}/repos/${owner}/${repo}/contents/${encodedPath}`, params);
    if (resp.status !== 200) return null;
    const data = await resp.json();
    if (data.encoding === 'base64' && data.content) {
      try {
        return Buffer.from(data.content, 'base64').toString('utf-8');
      } catch {
        return null;
      }
    }
    return null;
  }

  async getRootFiles(owner, repo, ref) {
    const params = {};
    if (ref) params.ref = ref;
    const resp = await this._get(`${GITHUB_API}/repos/${owner}/${repo}/contents`, params);
    if (resp.status !== 200) return [];
    const data = await resp.json();
    if (!Array.isArray(data)) return [];
    return data.filter(item => typeof item === 'object').map(item => item.name);
  }

  /**
   * Every file path in the repo at ref (recursive, via the git trees API) —
   * mirrors the Python client so specs in subfolders are found.
   */
  async getAllFilePaths(owner, repo, ref) {
    const treeRef = ref || 'HEAD';
    const resp = await this._get(
      `${GITHUB_API}/repos/${owner}/${repo}/git/trees/${encodeURIComponent(treeRef)}`,
      { recursive: '1' }
    );
    if (resp.status !== 200) return [];
    const data = await resp.json();
    return (data.tree || []).filter(i => i.type === 'blob').map(i => i.path);
  }

  async checkRequiredFiles(owner, repo, ref) {
    // Root-first, matching where these docs almost always live: the root
    // listing is a tiny payload, while the recursive tree can be megabytes
    // on a big repo — costly inside a 10s/26s Netlify function. Fall back
    // to the full tree only when something is missing at the root, so
    // specs kept in docs/ etc. still score the same as the Python app.
    const rootFiles = await this.getRootFiles(owner, repo, ref);
    const rootMatch = matchRequiredDocs(rootFiles);
    if (Object.values(rootMatch.results).every(Boolean)) return rootMatch;

    const allPaths = await this.getAllFilePaths(owner, repo, ref);
    if (!allPaths.length) return rootMatch; // tree fetch failed — keep root results
    return matchRequiredDocs(allPaths);
  }

  async getLastCommitForPath(owner, repo, path, ref) {
    const params = { path, per_page: 1 };
    if (ref) params.sha = ref;
    const resp = await this._get(`${GITHUB_API}/repos/${owner}/${repo}/commits`, params);
    if (resp.status === 200) {
      const commits = await resp.json();
      if (commits.length) return commits[0].commit.committer.date;
    }
    return null;
  }

  async getLastCommitDate(owner, repo, ref) {
    const params = { per_page: 1 };
    if (ref) params.sha = ref;
    const resp = await this._get(`${GITHUB_API}/repos/${owner}/${repo}/commits`, params);
    if (resp.status === 200) {
      const commits = await resp.json();
      if (commits.length) return commits[0].commit.committer.date;
    }
    return null;
  }
}

async function scanRepoLite(client, repo) {
  const owner = repo.owner.login;
  const name = repo.name;
  const defaultBranch = repo.default_branch || 'main';

  const branches = await client.getBranches(owner, name);
  const totalBranchCount = branches.length;
  // Branches named "henry" are excluded from dashboard counts, matching the
  // Python scan (default branch is never treated as henry).
  const henryBranchCount = branches.filter(
    b => b.name.toLowerCase().includes('henry') && b.name !== defaultBranch
  ).length;

  const { results: requiredFiles, actualNames } = await client.checkRequiredFiles(owner, name, defaultBranch);

  // Check doc staleness
  let docsUpdated = null;
  const hasProductSpec = requiredFiles['PRODUCT_SPEC.md'] || false;
  const hasSessionNotes = requiredFiles['SESSION_NOTES.md'] || false;

  const docFilenames = new Set();
  if (hasSessionNotes) docFilenames.add(actualNames['SESSION_NOTES.md'] || 'SESSION_NOTES.md');
  if (hasProductSpec) docFilenames.add(actualNames['PRODUCT_SPEC.md'] || 'PRODUCT_SPEC.md');

  if (docFilenames.size > 0) {
    const latestCommitTs = await client.getLastCommitDate(owner, name, defaultBranch);
    if (latestCommitTs) {
      const latestDt = new Date(latestCommitTs);
      const threshold = 7 * 24 * 60 * 60 * 1000;
      let allFresh = true;

      for (const realName of docFilenames) {
        const docTs = await client.getLastCommitForPath(owner, name, realName, defaultBranch);
        if (docTs) {
          const docDt = new Date(docTs);
          if (docDt.getTime() < latestDt.getTime() - threshold) {
            allFresh = false;
          }
        } else {
          allFresh = false;
        }
      }
      docsUpdated = allFresh;
    }
  }

  return {
    owner,
    name,
    full_name: repo.full_name,
    default_branch: defaultBranch,
    private: repo.private || false,
    html_url: repo.html_url || '',
    description: repo.description || '',
    created_at: repo.created_at || '',
    updated_at: repo.updated_at || '',
    pushed_at: repo.pushed_at || '',
    total_branch_count: totalBranchCount,
    non_default_branch_count: Math.max(0, totalBranchCount - 1),
    henry_branch_count: henryBranchCount,
    non_henry_branch_count: totalBranchCount - henryBranchCount,
    branch_names: branches.map(b => b.name),
    required_files: requiredFiles,
    // Resolved doc paths — persisted so detail/summary pages don't have to
    // re-run the lookup (and possibly a full-tree fetch) per page view.
    actual_names: actualNames,
    files_present: Object.values(requiredFiles).filter(Boolean).length,
    files_total: Object.keys(requiredFiles).length,
    docs_updated: docsUpdated,
  };
}

module.exports = { GitHubClient, GitHubAuthError, scanRepoLite, fetchRepoDocs, DOC_KEYS };
