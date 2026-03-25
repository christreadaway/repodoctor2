/**
 * RepoDoctor — Express.js serverless app for Netlify Functions.
 * Port of the Flask application.
 */

const path = require('path');
const express = require('express');
const serverless = require('serverless-http');
const nunjucks = require('nunjucks');
const cookieSession = require('cookie-session');

const { GitHubClient, scanRepoLite } = require('./lib/github-client');
const models = require('./lib/models');
const specCleaner = require('./lib/spec-cleaner');

const app = express();

// --- Nunjucks template engine ---
const viewsDir = path.join(__dirname, 'views');
const env = nunjucks.configure(viewsDir, {
  autoescape: true,
  express: app,
});

// Custom filters
env.addFilter('truncate', (str, len) => {
  if (!str) return '';
  if (str.length <= len) return str;
  return str.substring(0, len) + '...';
});

env.addFilter('dateonly', (str) => {
  if (!str) return '—';
  return str.substring(0, 10);
});

// --- Middleware ---
app.use(express.urlencoded({ extended: true }));
app.use(express.json());

app.use(cookieSession({
  name: 'rd_session',
  keys: [process.env.FLASK_SECRET_KEY || 'repodoctor-dev-key-change-me'],
  maxAge: 30 * 60 * 1000,
  sameSite: 'lax',
}));

// Flash message middleware
app.use((req, res, next) => {
  if (!req.session.flash) req.session.flash = [];
  res.locals.messages = req.session.flash;
  req.session.flash = [];
  req.flash = (category, message) => {
    if (!req.session.flash) req.session.flash = [];
    req.session.flash.push([category, message]);
  };
  next();
});

// Inject common template variables
app.use((req, res, next) => {
  res.locals.authenticated = req.session.authenticated || false;
  res.locals.github_user = req.session.github_user || '';
  res.locals.preferences = models.getPreferences();
  res.locals.endpoint = req.path;
  next();
});

// --- In-memory state ---
let githubClient = null;
let credentials = null;
let scanResults = null;

function tryEnvCredentials() {
  if (githubClient) return true;
  const pat = process.env.GITHUB_PAT || '';
  const anthropicKey = process.env.ANTHROPIC_API_KEY || '';
  if (pat && anthropicKey) {
    credentials = { github_pat: pat, anthropic_key: anthropicKey };
    githubClient = new GitHubClient(pat);
    return true;
  }
  return false;
}

function requireAuth(req, res, next) {
  if (!req.session.authenticated) return res.redirect('/login');
  // Restore credentials from env vars after cold start
  if (!githubClient) tryEnvCredentials();
  next();
}

// --- Auth Routes ---

app.get('/login', (req, res) => {
  const envMode = !!process.env.GITHUB_PAT && !!process.env.ANTHROPIC_API_KEY;
  if (envMode && req.session.authenticated) return res.redirect('/');
  res.render('login.html', { has_credentials: envMode });
});

app.post('/login', async (req, res) => {
  const envMode = !!process.env.GITHUB_PAT && !!process.env.ANTHROPIC_API_KEY;
  const sitePassword = process.env.SITE_PASSWORD || '';

  if (envMode) {
    const entered = req.body.password || '';
    if (sitePassword && entered !== sitePassword) {
      req.flash('error', 'Wrong password. Try again.');
      return res.redirect('/login');
    }
    tryEnvCredentials();
    req.session.authenticated = true;
    try {
      const userInfo = await githubClient.verifyToken();
      if (userInfo) req.session.github_user = userInfo.login || '';
    } catch { /* ignore */ }
    return res.redirect('/');
  }

  // Non-env mode: first-time setup
  const password = req.body.password || '';
  const pat = (req.body.github_pat || '').trim();
  const anthropicKey = (req.body.anthropic_key || '').trim();

  if (!password || !pat || !anthropicKey) {
    req.flash('error', 'All fields are required.');
    return res.redirect('/login');
  }
  if (password.length < 4) {
    req.flash('error', 'Password must be at least 4 characters.');
    return res.redirect('/login');
  }

  const testClient = new GitHubClient(pat);
  const userInfo = await testClient.verifyToken();
  if (!userInfo) {
    req.flash('error', 'Invalid GitHub PAT. Check your token and try again.');
    return res.redirect('/login');
  }
  if (!userInfo._scopes.includes('repo')) {
    req.flash('error', `GitHub PAT needs 'repo' scope. Current scopes: ${userInfo._scopes}`);
    return res.redirect('/login');
  }

  credentials = { github_pat: pat, anthropic_key: anthropicKey };
  githubClient = new GitHubClient(pat);
  req.session.authenticated = true;
  req.session.github_user = userInfo.login || '';
  req.flash('success', `Welcome, ${userInfo.login}! Connected.`);
  return res.redirect('/');
});

app.get('/logout', (req, res) => {
  githubClient = null;
  credentials = null;
  req.session = null;
  res.redirect('/login');
});

// --- Dashboard ---

app.get('/', requireAuth, (req, res) => {
  res.render('dashboard.html', {
    scan_results: scanResults,
    session_cost: models.sessionCost.toDict(),
  });
});

app.post('/scan', requireAuth, async (req, res) => {
  if (!githubClient) {
    req.flash('error', 'Not authenticated with GitHub.');
    return res.redirect('/');
  }

  const prefs = models.getPreferences();
  const excluded = new Set(prefs.excluded_repos || []);

  let repos;
  try {
    repos = await githubClient.getRepos();
  } catch (e) {
    req.flash('error', `GitHub API error: ${e.message}`);
    return res.redirect('/');
  }

  const filteredRepos = repos.filter(r => !excluded.has(r.full_name) && !excluded.has(r.name));

  // Process repos in parallel batches to stay within timeout
  const BATCH_SIZE = 10;
  const results = [];
  for (let i = 0; i < filteredRepos.length; i += BATCH_SIZE) {
    const batch = filteredRepos.slice(i, i + BATCH_SIZE);
    const batchResults = await Promise.all(batch.map(async (repo) => {
      try {
        return await scanRepoLite(githubClient, repo);
      } catch (e) {
        return {
          owner: repo.owner.login,
          name: repo.name,
          full_name: repo.full_name,
          default_branch: repo.default_branch || 'main',
          private: repo.private || false,
          html_url: repo.html_url || '',
          description: repo.description || '',
          created_at: repo.created_at || '',
          updated_at: repo.updated_at || '',
          total_branch_count: 0,
          non_default_branch_count: 0,
          branch_names: [],
          required_files: {},
          files_present: 0,
          files_total: 6,
          error: e.message,
        };
      }
    }));
    results.push(...batchResults);
  }

  results.sort((a, b) => (b.total_branch_count || 0) - (a.total_branch_count || 0));

  scanResults = {
    repos: results,
    total_repos: results.length,
    total_branches: results.reduce((sum, r) => sum + (r.total_branch_count || 0), 0),
    repos_missing_files: results.filter(r => (r.files_present || 0) < 4).length,
  };

  models.saveScan(scanResults);
  models.logAction('scan', 'all', 'all', `Scanned ${results.length} repos, ${scanResults.total_branches} total branches`);
  req.flash('success', `Scan complete: ${results.length} repos, ${scanResults.total_branches} total branches found.`);
  return res.redirect('/');
});

// --- Repo Detail ---

app.get('/repo/:owner/:name', requireAuth, async (req, res) => {
  const { owner, name } = req.params;
  if (!githubClient) {
    req.flash('error', 'Not authenticated with GitHub.');
    return res.redirect('/');
  }

  let repoInfo = null;
  if (scanResults) {
    repoInfo = scanResults.repos.find(r => r.owner === owner && r.name === name);
  }
  if (!repoInfo) {
    req.flash('error', 'Repo not found. Try scanning first.');
    return res.redirect('/');
  }

  const ref = repoInfo.default_branch || 'main';
  const rootFiles = await githubClient.getRootFiles(owner, name, ref);
  const fileMap = {};
  for (const f of rootFiles) {
    const fl = f.toLowerCase();
    const dot = fl.lastIndexOf('.');
    const stem = dot > 0 ? fl.substring(0, dot) : fl;
    fileMap[stem] = f;
  }

  const specFiles = { PRODUCT_SPEC: null, SESSION_NOTES: null };
  const rawSpecs = {};

  for (const key of Object.keys(specFiles)) {
    const actualName = fileMap[key.toLowerCase()];
    if (actualName) {
      let content = await githubClient.getFileContent(owner, name, actualName, ref);
      if (content) {
        if (content.length > 10000) content = content.substring(0, 10000) + '\n\n... (truncated)';
        rawSpecs[key] = content;
        specFiles[key] = specCleaner.cleanMarkdown(content);
      }
    }
  }

  const whatsNext = specCleaner.extractWhatsNext(rawSpecs, []);

  res.render('repo_detail.html', {
    repo: repoInfo,
    specs: specFiles,
    whats_next: whatsNext,
    conversations: [],
  });
});

// --- Settings ---

app.get('/settings', requireAuth, (req, res) => {
  res.render('settings.html', {
    specs: models.listSpecs(),
    session_cost: models.sessionCost.toDict(),
  });
});

app.post('/settings', requireAuth, (req, res) => {
  const action = req.body.action;

  if (action === 'save_preferences') {
    const prefs = models.getPreferences();
    prefs.local_root = req.body.local_root || '~/claudesync2';
    prefs.ai_model = req.body.ai_model || 'claude-haiku-4-5-20251001';
    prefs.display_mode = req.body.display_mode || 'plain_english';
    const excluded = req.body.excluded_repos || '';
    prefs.excluded_repos = excluded.split(',').map(s => s.trim()).filter(Boolean);
    models.savePreferences(prefs);
    req.flash('success', 'Preferences saved.');
  } else if (action === 'save_spec') {
    const specRepo = (req.body.spec_repo || '').trim();
    const specContent = (req.body.spec_content || '').trim();
    if (specRepo && specContent) {
      models.saveSpec(specRepo, specContent);
      req.flash('success', `Spec saved for ${specRepo}.`);
    }
  } else if (action === 'reset_credentials') {
    githubClient = null;
    credentials = null;
    req.flash('success', 'Credentials reset. You will need to re-enter them.');
    return res.redirect('/logout');
  }

  return res.redirect('/settings');
});

// --- Projects ---

app.get('/projects', requireAuth, (req, res) => {
  const summaries = models.getProjectSummaries();
  const repos = scanResults ? scanResults.repos : [];
  res.render('projects.html', {
    repos,
    summaries,
    scan_results: scanResults,
  });
});

// Redirect GET requests to /projects (form is POST-only)
app.get('/projects/generate', requireAuth, (req, res) => {
  res.redirect('/projects');
});

app.post('/projects/generate', requireAuth, async (req, res) => {
  if (!githubClient || !credentials) {
    req.flash('error', 'Not authenticated.');
    return res.redirect('/projects');
  }
  if (!scanResults) {
    req.flash('error', 'Run a scan first from the Dashboard.');
    return res.redirect('/projects');
  }

  const repos = scanResults.repos || [];

  // Helper to generate summary for a single repo
  async function generateOneSummary(repo) {
    const { owner, name } = repo;
    const ref = repo.default_branch || 'main';

    const rootFiles = await githubClient.getRootFiles(owner, name, ref);
    const fileMap = {};
    for (const f of rootFiles) {
      const fl = f.toLowerCase();
      const dot = fl.lastIndexOf('.');
      const stem = dot > 0 ? fl.substring(0, dot) : fl;
      fileMap[stem] = f;
    }

    const specContent = {};
    for (const key of ['product_spec', 'business_spec', 'project_status', 'session_notes']) {
      const actualName = fileMap[key];
      if (actualName) {
        const content = await githubClient.getFileContent(owner, name, actualName, ref);
        if (content) specContent[key] = content.substring(0, 5000);
      }
    }

    const contextParts = [];
    if (repo.description) contextParts.push(`GitHub description: ${repo.description}`);
    for (const [key, content] of Object.entries(specContent)) {
      contextParts.push(`--- ${key.toUpperCase()} ---\n${content}`);
    }

    if (!contextParts.length) {
      models.saveProjectSummary(name, {
        what_it_does: `${name} — no spec files or description available.`,
        how_finished: 'Unknown — no spec files found.',
        next_steps: ['Add PRODUCT_SPEC.md with project description', 'Add SESSION_NOTES.md with session tracking'],
      });
      return 'skipped';
    }

    const contextText = contextParts.join('\n\n');
    try {
      const aiResp = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': credentials.anthropic_key,
          'anthropic-version': '2023-06-01',
        },
        body: JSON.stringify({
          model: 'claude-haiku-4-5-20251001',
          max_tokens: 500,
          messages: [{
            role: 'user',
            content: `Project: ${name}\n\n${contextText}\n\n` +
              'Based on the above, return ONLY valid JSON with:\n' +
              '1. "what_it_does": 1-2 sentence description of what this project does\n' +
              '2. "how_finished": 1-2 sentence assessment of how complete/finished the project is\n' +
              '3. "next_steps": array of up to 5 short bullet strings for what needs to be built or tested next\n' +
              'Return raw JSON only, no markdown fencing.',
          }],
        }),
      });

      const aiData = await aiResp.json();
      let raw = aiData.content[0].text.trim();
      if (raw.startsWith('```')) {
        raw = raw.includes('\n') ? raw.split('\n').slice(1).join('\n') : raw.substring(3);
        if (raw.endsWith('```')) raw = raw.slice(0, -3).trim();
      }
      const summary = JSON.parse(raw);
      if (summary.next_steps && summary.next_steps.length > 5) {
        summary.next_steps = summary.next_steps.slice(0, 5);
      }
      models.saveProjectSummary(name, summary);
      return 'generated';
    } catch (e) {
      models.saveProjectSummary(name, {
        what_it_does: repo.description || `${name} — summary generation failed.`,
        how_finished: 'Unknown — AI summary could not be generated.',
        next_steps: [`Error: ${String(e.message || e).substring(0, 100)}`],
      });
      return 'skipped';
    }
  }

  // Process repos in parallel batches of 5 (each makes GitHub + AI calls)
  const BATCH_SIZE = 5;
  let generated = 0;
  let skipped = 0;

  for (let i = 0; i < repos.length; i += BATCH_SIZE) {
    const batch = repos.slice(i, i + BATCH_SIZE);
    const results = await Promise.all(batch.map(generateOneSummary));
    for (const r of results) {
      if (r === 'generated') generated++;
      else skipped++;
    }
  }

  req.flash('success', `Generated summaries for ${generated} projects (${skipped} skipped/fallback).`);
  models.logAction('generate_summaries', 'all', 'all', `Generated ${generated}, skipped ${skipped}`);
  return res.redirect('/projects');
});

// --- Mac Setup ---

app.get('/mac-setup', requireAuth, (req, res) => {
  res.render('mac_setup.html');
});

// --- API Routes ---

app.get('/api/session-cost', requireAuth, (req, res) => {
  res.json(models.sessionCost.toDict());
});

app.get('/api/debug-files/:owner/:name', requireAuth, async (req, res) => {
  const { owner, name } = req.params;
  if (!githubClient) return res.status(401).json({ error: 'Not authenticated' });

  let defaultBranch = 'main';
  if (scanResults) {
    const found = scanResults.repos.find(r => r.owner === owner && r.name === name);
    if (found) defaultBranch = found.default_branch || 'main';
  }

  const rootFiles = await githubClient.getRootFiles(owner, name, defaultBranch);
  const { results: requiredFiles, actualNames } = await githubClient.checkRequiredFiles(owner, name, defaultBranch);

  const stemMap = {};
  for (const f of rootFiles) {
    const fl = f.toLowerCase();
    const dot = fl.lastIndexOf('.');
    const stem = dot > 0 ? fl.substring(0, dot) : fl;
    stemMap[stem] = f;
  }

  res.json({
    repo: `${owner}/${name}`,
    default_branch: defaultBranch,
    root_files_from_api: rootFiles,
    root_file_count: rootFiles.length,
    stem_map: stemMap,
    required_files_result: requiredFiles,
    actual_names: actualNames,
    files_present: Object.values(requiredFiles).filter(Boolean).length,
    files_total: Object.keys(requiredFiles).length,
  });
});

// --- Export handler ---
module.exports.handler = serverless(app);
