/**
 * RepoDoctor — Express.js serverless app for Netlify Functions.
 * Port of the Flask application.
 */

const path = require('path');
const crypto = require('crypto');
const express = require('express');
const serverless = require('serverless-http');
const nunjucks = require('nunjucks');
const cookieSession = require('cookie-session');

const { GitHubClient, GitHubAuthError, scanRepoLite, fetchRepoDocs } = require('./lib/github-client');
const models = require('./lib/models');
const specCleaner = require('./lib/spec-cleaner');

const app = express();

// Netlify terminates TLS at the edge and forwards to this function over HTTP
// with X-Forwarded-Proto: https. Trusting the proxy lets Express treat the
// original request as secure, so req.ip reflects the real client IP and the
// Secure session cookie below is actually emitted.
app.set('trust proxy', true);

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

// SECURITY: never fall back to a constant signing key — anyone who reads the
// repo could forge an authenticated session cookie. When FLASK_SECRET_KEY is
// unset, derive a stable key from the other deployment secrets: a random
// per-process key would differ across concurrent Lambda instances and bounce
// users to /login whenever a request lands on a different instance.
function sessionSigningKey() {
  if (process.env.FLASK_SECRET_KEY) return process.env.FLASK_SECRET_KEY;
  const material = [process.env.GITHUB_PAT, process.env.ANTHROPIC_API_KEY, process.env.SITE_PASSWORD]
    .filter(Boolean).join('|');
  if (material) {
    console.warn('FLASK_SECRET_KEY is not set — deriving the session key from other env secrets. ' +
      'Set FLASK_SECRET_KEY in the Netlify environment.');
    return crypto.createHash('sha256').update('repodoctor-session-key:' + material).digest('hex');
  }
  console.warn('FLASK_SECRET_KEY is not set and no env secrets exist — using a random per-process key.');
  return crypto.randomBytes(32).toString('hex');
}
app.use(cookieSession({
  name: 'rd_session',
  keys: [sessionSigningKey()],
  maxAge: 30 * 60 * 1000,
  sameSite: 'lax',
  // Secure in production (Netlify is HTTPS at the edge; with 'trust proxy'
  // above, cookie-session sees the request as secure and emits the cookie).
  // Relaxed only under the Netlify CLI dev server, which serves plain HTTP —
  // there a Secure cookie throws "Cannot send secure cookie over unencrypted
  // connection" and 500s every page. Defaults to secure unless explicitly in
  // that dev context, so production is never silently downgraded.
  secure: process.env.NETLIFY_DEV !== 'true',
  httpOnly: true,
}));

/**
 * Express 4 does not forward rejected promises from async handlers to error
 * middleware — an unhandled rejection hangs the request until the function
 * times out. Wrap every async route so failures flash + redirect instead.
 */
function asyncRoute(fn, redirectTo = '/') {
  return async (req, res, next) => {
    try {
      await fn(req, res, next);
    } catch (e) {
      // Full stack to the Netlify function log — the flash below is a
      // truncated one-liner and useless for actual debugging.
      console.error(`Route ${req.method} ${req.path} failed:`, e);
      if (e instanceof GitHubAuthError) {
        const envMode = !!process.env.GITHUB_PAT;
        req.flash('error',
          'GitHub authentication failed — your Personal Access Token is no longer valid ' +
          '(revoked, expired, or missing the repo scope). ' +
          (envMode ? 'Update GITHUB_PAT in the Netlify environment and redeploy.'
                   : 'Log out and re-enter a fresh token.'));
      } else {
        req.flash('error', `Unexpected error: ${String(e.message || e).substring(0, 200)}`);
      }
      if (!res.headersSent) res.redirect(redirectTo);
    }
  };
}

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

// --- Login throttle + constant-time compare ---

// Constant-time password comparison. Hash both sides to a fixed length first
// so timingSafeEqual never throws on a length mismatch (and length isn't
// leaked through timing).
function safeEqual(a, b) {
  const ha = crypto.createHash('sha256').update(String(a)).digest();
  const hb = crypto.createHash('sha256').update(String(b)).digest();
  return crypto.timingSafeEqual(ha, hb);
}

// In-memory brute-force throttle keyed by client IP. NOTE: serverless
// instances are ephemeral and a determined attacker's requests may land on
// different instances, so this is a speed bump, not an airtight limit — pair
// it with a long, high-entropy SITE_PASSWORD.
const LOGIN_MAX_ATTEMPTS = 5;
const LOGIN_LOCKOUT_MS = 15 * 60 * 1000;
const loginAttempts = new Map(); // ip -> { count, resetAt }

function loginLockMs(ip) {
  const rec = loginAttempts.get(ip);
  if (!rec) return 0;
  if (Date.now() > rec.resetAt) { loginAttempts.delete(ip); return 0; }
  return rec.count >= LOGIN_MAX_ATTEMPTS ? rec.resetAt - Date.now() : 0;
}

function recordLoginFailure(ip) {
  const now = Date.now();
  // Bound the map so a flood of distinct keys can't grow it without limit:
  // once it's large, drop every entry whose lockout window has expired.
  if (loginAttempts.size > 1000) {
    for (const [k, v] of loginAttempts) {
      if (now > v.resetAt) loginAttempts.delete(k);
    }
  }
  const rec = loginAttempts.get(ip);
  if (!rec || now > rec.resetAt) {
    loginAttempts.set(ip, { count: 1, resetAt: now + LOGIN_LOCKOUT_MS });
  } else {
    rec.count += 1;
  }
}

function clearLoginFailures(ip) {
  loginAttempts.delete(ip);
}

// Netlify's edge sets x-nf-client-connection-ip to the real client IP. Prefer
// it over req.ip: under 'trust proxy' req.ip is the left-most X-Forwarded-For
// entry, which a client can spoof to get a fresh throttle bucket every request
// (bypassing the lockout) or, if XFF is ever absent, collapse all clients into
// one bucket and lock everyone out. Fall back to req.ip for local dev.
function clientIp(req) {
  return req.headers['x-nf-client-connection-ip'] || req.ip || 'unknown';
}

// --- Auth Routes ---

app.get('/login', (req, res) => {
  const envMode = !!process.env.GITHUB_PAT && !!process.env.ANTHROPIC_API_KEY;
  if (envMode && req.session.authenticated) return res.redirect('/');
  res.render('login.html', { has_credentials: envMode });
});

app.post('/login', asyncRoute(async (req, res) => {
  const envMode = !!process.env.GITHUB_PAT && !!process.env.ANTHROPIC_API_KEY;
  const sitePassword = process.env.SITE_PASSWORD || '';

  if (envMode) {
    // SECURITY: an unset SITE_PASSWORD must not mean "no password" — that
    // would let any visitor log in and read private repos on the deployer's
    // PAT. Refuse until it's configured.
    if (!sitePassword) {
      req.flash('error', 'SITE_PASSWORD is not configured — set it in the Netlify environment before logging in.');
      return res.redirect('/login');
    }
    const ip = clientIp(req);
    const lockMs = loginLockMs(ip);
    if (lockMs > 0) {
      req.flash('error', `Too many failed attempts. Try again in ${Math.ceil(lockMs / 60000)} minute(s).`);
      return res.redirect('/login');
    }
    const entered = req.body.password || '';
    if (!safeEqual(entered, sitePassword)) {
      recordLoginFailure(ip);
      req.flash('error', 'Wrong password. Try again.');
      return res.redirect('/login');
    }
    clearLoginFailures(ip);
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
  if (password.length < 8) {
    req.flash('error', 'Password must be at least 8 characters.');
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
}, '/login'));

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

app.post('/scan', requireAuth, asyncRoute(async (req, res) => {
  if (!githubClient) {
    req.flash('error', 'Not authenticated with GitHub.');
    return res.redirect('/');
  }

  const prefs = models.getPreferences();
  const excluded = new Set(prefs.excluded_repos || []);

  // Failures (including a dead PAT) surface via the asyncRoute wrapper.
  const repos = await githubClient.getRepos();

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
        if (e instanceof GitHubAuthError) throw e;
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
          pushed_at: repo.pushed_at || '',
          docs_updated: null,
          total_branch_count: 0,
          non_default_branch_count: 0,
          henry_branch_count: 0,
          non_henry_branch_count: 0,
          branch_names: [],
          required_files: {},
          files_present: 0,
          files_total: 5,
          error: e.message,
        };
      }
    }));
    results.push(...batchResults);
  }

  // Sort + total by non-henry branch count, matching the Python dashboard.
  results.sort((a, b) => (b.non_henry_branch_count || 0) - (a.non_henry_branch_count || 0));

  scanResults = {
    repos: results,
    total_repos: results.length,
    total_branches: results.reduce((sum, r) => sum + (r.non_henry_branch_count || 0), 0),
    // "missing any required doc" — the required set is 5 files now.
    repos_missing_files: results.filter(r => (r.files_present || 0) < (r.files_total || 5)).length,
  };

  models.saveScan(scanResults);
  models.logAction('scan', 'all', 'all', `Scanned ${results.length} repos, ${scanResults.total_branches} total branches`);
  req.flash('success', `Scan complete: ${results.length} repos, ${scanResults.total_branches} total branches found.`);
  return res.redirect('/');
}));

// --- Repo Detail ---

app.get('/repo/:owner/:name', requireAuth, asyncRoute(async (req, res) => {
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
  // Reuse the doc paths the scan already resolved; fall back to a fresh
  // lookup for scans saved before actual_names was persisted.
  const docs = await fetchRepoDocs(githubClient, owner, name, ref, {
    maxChars: 10000,
    actualNames: repoInfo.actual_names || null,
  });

  // Same three docs the Python route reads — without PROJECT_STATUS the
  // What's Next extractor's highest-priority source was dead code here.
  const specFiles = { PRODUCT_SPEC: null, PROJECT_STATUS: null, SESSION_NOTES: null };
  const rawSpecs = {};
  for (const key of Object.keys(specFiles)) {
    let content = docs[key.toLowerCase()];
    if (content) {
      if (content.length >= 10000) content += '\n\n... (truncated)';
      rawSpecs[key] = content;
      specFiles[key] = specCleaner.cleanMarkdown(content);
    }
  }

  const whatsNext = specCleaner.extractWhatsNext(rawSpecs, []);

  res.render('repo_detail.html', {
    repo: repoInfo,
    specs: specFiles,
    whats_next: whatsNext,
    conversations: [],
  });
}));

// --- Settings ---

app.get('/settings', requireAuth, (req, res) => {
  res.render('settings.html', {
    specs: models.listSpecs(),
    session_cost: models.sessionCost.toDict(),
  });
});

app.post('/settings', requireAuth, (req, res) => {
  // Guard req.body: body-parser leaves it undefined for a foreign/empty
  // Content-Type, and this handler isn't wrapped by asyncRoute.
  const action = (req.body || {}).action;

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

app.post('/projects/generate', requireAuth, asyncRoute(async (req, res) => {
  if (!githubClient || !credentials) {
    req.flash('error', 'Not authenticated.');
    return res.redirect('/projects');
  }
  if (!scanResults) {
    req.flash('error', 'Run a scan first from the Dashboard.');
    return res.redirect('/projects');
  }

  const repos = scanResults.repos || [];

  const aiModel = models.getPreferences().ai_model || 'claude-haiku-4-5-20251001';

  // Helper to generate summary for a single repo
  async function generateOneSummary(repo) {
    const { owner, name } = repo;
    const ref = repo.default_branch || 'main';

    // GitHub fetches go inside try/catch too: a single rejected fetch would
    // otherwise reject the whole Promise.all batch and hang the request.
    let specContent = {};
    try {
      specContent = await fetchRepoDocs(githubClient, owner, name, ref, {
        maxChars: 5000,
        actualNames: repo.actual_names || null,
      });
    } catch (e) {
      if (e instanceof GitHubAuthError) throw e;
      models.saveProjectSummary(name, {
        what_it_does: repo.description || `${name} — summary generation failed.`,
        how_finished: 'Unknown — could not fetch repo docs.',
        next_steps: [`Error: ${String(e.message || e).substring(0, 100)}`],
      });
      return 'skipped';
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
          model: aiModel,
          max_tokens: 500,
          // Sonnet 5 runs adaptive thinking by default when `thinking` is
          // omitted, which spends output tokens and can truncate our JSON
          // under this 500-token budget. Disable it. Opus 4.8 / Haiku 4.5
          // already default to no thinking.
          ...(aiModel.includes('sonnet-5') ? { thinking: { type: 'disabled' } } : {}),
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
      if (!aiResp.ok) {
        // Surface the API's real error instead of a TypeError on undefined.
        throw new Error(aiData?.error?.message || `Anthropic API HTTP ${aiResp.status}`);
      }
      const textBlock = (aiData.content || []).find(b => b.type === 'text');
      let raw = (textBlock?.text || '').trim();
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
}, '/projects'));

// --- Mac Setup ---

app.get('/mac-setup', requireAuth, (req, res) => {
  res.render('mac_setup.html');
});

// --- API Routes ---

app.get('/api/session-cost', requireAuth, (req, res) => {
  res.json(models.sessionCost.toDict());
});

app.get('/api/debug-files/:owner/:name', requireAuth, async (req, res, next) => {
  try {
    await debugFiles(req, res);
  } catch (e) {
    console.error(`Route ${req.method} ${req.path} failed:`, e);
    res.status(502).json({ error: String(e.message || e).substring(0, 300) });
  }
});

async function debugFiles(req, res) {
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
}

// --- Export handler ---
module.exports.handler = serverless(app);
