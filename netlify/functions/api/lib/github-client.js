/**
 * GitHub API client for RepoDoctor (Node.js port).
 * Uses native fetch (Node 18+).
 */

const GITHUB_API = 'https://api.github.com';

class GitHubClient {
  constructor(pat) {
    this.headers = {
      Authorization: `token ${pat}`,
      Accept: 'application/vnd.github.v3+json',
    };
  }

  async _get(url, params = {}) {
    const u = new URL(url);
    for (const [k, v] of Object.entries(params)) {
      u.searchParams.set(k, String(v));
    }
    let resp = await fetch(u.toString(), { headers: this.headers });
    if (resp.status === 403) {
      const text = await resp.text();
      if (text.toLowerCase().includes('rate limit')) {
        const resetAt = resp.headers.get('X-RateLimit-Reset');
        let wait = 5;
        if (resetAt) {
          wait = Math.max(1, Math.min(60, parseInt(resetAt) - Math.floor(Date.now() / 1000) + 1));
        }
        await new Promise(r => setTimeout(r, wait * 1000));
        resp = await fetch(u.toString(), { headers: this.headers });
      }
    }
    return resp;
  }

  async verifyToken() {
    const resp = await this._get(`${GITHUB_API}/user`);
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
    const resp = await this._get(`${GITHUB_API}/repos/${owner}/${repo}/contents/${path}`, params);
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

  async checkRequiredFiles(owner, repo, ref) {
    const rootFiles = await this.getRootFiles(owner, repo, ref);
    const stemToActual = {};
    for (const f of rootFiles) {
      const fl = f.toLowerCase();
      const dot = fl.lastIndexOf('.');
      const stem = dot > 0 ? fl.substring(0, dot) : fl;
      stemToActual[stem] = f;
    }

    const required = {
      'CLAUDE.md': 'claude',
      'LICENSE': 'license',
      'PRODUCT_SPEC.md': 'product_spec',
      'SESSION_NOTES.md': 'session_notes',
    };

    const results = {};
    const actualNames = {};
    for (const [displayName, stem] of Object.entries(required)) {
      const found = stem in stemToActual;
      results[displayName] = found;
      if (found) actualNames[displayName] = stemToActual[stem];
    }
    return { results, actualNames };
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
    total_branch_count: totalBranchCount,
    non_default_branch_count: Math.max(0, totalBranchCount - 1),
    branch_names: branches.map(b => b.name),
    required_files: requiredFiles,
    files_present: Object.values(requiredFiles).filter(Boolean).length,
    files_total: Object.keys(requiredFiles).length,
    docs_updated: docsUpdated,
  };
}

module.exports = { GitHubClient, scanRepoLite };
