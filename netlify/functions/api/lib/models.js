/**
 * In-memory data models for RepoDoctor (serverless).
 * All data is ephemeral — resets on cold start.
 */

const DEFAULT_PREFS = {
  local_root: '~/claudesync2',
  sort_repos_by: 'branch_count',
  sort_branches_by: 'classification',
  excluded_repos: [],
  ai_model: 'claude-haiku-4-5-20251001',
  display_mode: 'plain_english',
};

// In-memory stores
let preferences = { ...DEFAULT_PREFS };
let scanHistory = { scans: [] };
let projectSummaries = {};
let specs = {};
let actionLog = [];

const sessionCost = {
  totalInputTokens: 0,
  totalOutputTokens: 0,
  totalCost: 0,
  analysesCount: 0,
  add(inputTokens, outputTokens, cost) {
    this.totalInputTokens += inputTokens;
    this.totalOutputTokens += outputTokens;
    this.totalCost += cost;
    this.analysesCount++;
  },
  toDict() {
    return {
      total_input_tokens: this.totalInputTokens,
      total_output_tokens: this.totalOutputTokens,
      total_cost: Math.round(this.totalCost * 10000) / 10000,
      analyses_count: this.analysesCount,
    };
  },
};

function getPreferences() {
  return { ...DEFAULT_PREFS, ...preferences };
}

function savePreferences(prefs) {
  preferences = { ...prefs };
}

function saveScan(scanData) {
  scanData.timestamp = new Date().toISOString();
  scanHistory.scans.push(scanData);
  if (scanHistory.scans.length > 50) {
    scanHistory.scans = scanHistory.scans.slice(-50);
  }
}

function logAction(actionType, repo, branch, details = '') {
  actionLog.push({
    type: actionType,
    repo,
    branch,
    details,
    timestamp: new Date().toISOString(),
  });
}

function getProjectSummaries() {
  return { ...projectSummaries };
}

function saveProjectSummary(repoName, summary) {
  summary._generated_at = new Date().toISOString();
  projectSummaries[repoName] = summary;
}

function getSpec(repoName) {
  return specs[repoName] || null;
}

function saveSpec(repoName, content) {
  specs[repoName] = content;
}

function listSpecs() {
  return Object.keys(specs);
}

module.exports = {
  getPreferences,
  savePreferences,
  saveScan,
  logAction,
  getProjectSummaries,
  saveProjectSummary,
  getSpec,
  saveSpec,
  listSpecs,
  sessionCost,
};
