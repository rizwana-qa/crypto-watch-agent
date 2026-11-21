/**
 * Crypto Watch Agent: Google Apps Script web app.
 *
 * Four actions, all sent as a POST with a JSON body to  <web app URL>?key=<KEY>
 *   history   returns the agent's own recent decisions from the Agent tab (the agent's memory)
 *   save      adds one row to the Agent tab and, when alert is true, sends an email. The row has 10 cells: the last one, "Review",
 *             is the agent's short review status (at most 200 characters). It is optional: a save without a "review" field still
 *             works and leaves the cell empty. An Agent tab that has only the old 9 columns gets the header "Review" added in
 *             column J on the next call; the rows already there are not touched, and the history action reads old and new rows alike.
 *   report    emails the executive brief: the brief itself as the HTML body and the full interactive HTML page as an attachment
 *   append    adds rows to one of three extra tabs: market_data (the Kraken numbers per coin), cross_asset
 *             (correlation between coins) and scenarios (every scenario result). Only these three tab names are
 *             accepted, so a request can never create or change any other tab.
 *
 * Deploy: Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 * After EVERY change: Deploy > Manage deployments > edit > Version: New version > Deploy.
 *
 * Script Properties (Project Settings > Script properties). Nothing secret is stored in this file:
 *   KEY             a long random string (at least 16 characters); the same string goes in the SHEET_URL secret
 *   SPREADSHEET_ID  the id of the Google Sheet (the part of its URL between /d/ and /edit)
 *   EMAIL_TO        optional; defaults to the account that owns this script
 *
 * Every reply is JSON: {success: true, ...} or {success: false, error: "CODE"}. Replies never contain the key.
 * Run IDs: when a save or an append carries a run ID that is already in that tab's Run ID column (a GitHub "Re-run jobs"
 * keeps the same run ID), nothing is written and no email is sent, and the reply is {success: true, duplicate: true}.
 * A payload with no run ID, or with the default "local", is never treated as a duplicate.
 * Cells are written as text, so a message that starts with "=" is never run as a formula.
 */

const SHEET_NAME = 'Agent';
const HEADERS = ['Time (UTC)', 'Version', 'Run ID', 'Alert', 'Coins', 'Message', 'Sources', 'Reasoning', 'Blocked reason', 'Review'];
const MAX_REVIEW_CHARS = 200;   // the Review cell; agent.py sends at most this many
const MAX_BODY_CHARS = 20000;
const MAX_REPORT_CHARS = 600000;   // the executive brief page, sent only with action "report"
const MAX_SUMMARY_CHARS = 100000;   // the email body; Gmail clips an HTML email near 102 KB, and report.py keeps it under 90 KB
const MAX_TEXT_CHARS = 1000;
const MAX_HISTORY_ROWS = 20;
const MIN_KEY_LENGTH = 16;
const MAX_APPEND_ROWS = 60;
const NO_RUN_ID = 'local';   // what agent.py sends when GITHUB_RUN_ID is not set; it is not a real run, so it is never a duplicate
const LOCK_WAIT_MS = 30000;
// The extra tabs. The header row is written once, when the tab is created. Rows must have exactly this many cells.
const EXTRA_TABS = {
  market_data: ['Time (UTC)', 'Run ID', 'Version', 'Coin', 'As of (UTC day)', 'Last close (USD)', 'Daily return %',
    'Volatility 30d (daily %)', 'Distance from 30d avg %', 'Max drawdown 30d %', 'Rank: volatility', 'Rank: distance',
    'Rank: drawdown', 'Alert breaches', 'Trend', 'Change 30d %', 'Change 60d %', 'Volume vs 30d avg (x)', 'Rank: volume',
    'Context flags', 'Relative strength vs group %', 'Rank: relative strength', 'History days', 'Status'],
  cross_asset: ['Time (UTC)', 'Run ID', 'Version', 'As of (UTC day)', 'Pair', 'Correlation 30d', 'Rank', 'Flag'],
  context_data: ['Time (UTC)', 'Run ID', 'Version', 'Coin', 'Kraken mid (USD)', 'Spread %', 'Bid depth in band (USD)',
    'Ask depth in band (USD)', 'Depth band % of price', 'Funding rate % per hour', 'Funding pays', 'Open interest (USD, approx)', 'Implied vol % (annualised)',
    'Implied vol rank (1y)', 'Market volume 24h (USD)', 'Market volume vs 30d avg (x)', 'Market volume rank', 'CoinGecko price (USD)',
    'Price difference %', 'Fear and greed', 'Fear and greed label', 'Alert-rank days %', 'Alert episodes', 'Context flags', 'Status'],
  scenarios: ['Time (UTC)', 'Run ID', 'Version', 'No.', 'Scenario', 'Result', 'Expected', 'Alert', 'Coins', 'Message',
    'Sources', 'Blocked reason', 'AI calls'],
};

function doGet() {
  return reply_({ success: false, error: 'POST_REQUIRED' });
}

function doPost(e) {
  try {
    const props = PropertiesService.getScriptProperties();
    const key = String(props.getProperty('KEY') || '').trim();
    const supplied = e && e.parameter ? String(e.parameter.key || '') : '';
    if (key.length < MIN_KEY_LENGTH || supplied !== key) return reply_({ success: false, error: 'UNAUTHORIZED' });

    const contents = e.postData ? e.postData.contents : '';
    if (typeof contents !== 'string' || contents.length === 0 || contents.length > MAX_REPORT_CHARS) return reply_({ success: false, error: 'INVALID_REQUEST' });
    let request;
    try {
      request = JSON.parse(contents);
    } catch (ignored) {
      return reply_({ success: false, error: 'INVALID_REQUEST' });
    }
    if (request === null || typeof request !== 'object' || Array.isArray(request)) return reply_({ success: false, error: 'INVALID_REQUEST' });
    if (request.action !== 'report' && contents.length > MAX_BODY_CHARS) return reply_({ success: false, error: 'INVALID_REQUEST' });

    if (request.action === 'history') return reply_(history_(request, props));
    if (request.action === 'save') return reply_(save_(request, props));
    if (request.action === 'report') return reply_(report_(request, props));
    if (request.action === 'append') return reply_(append_(request, props));
    return reply_({ success: false, error: 'INVALID_ACTION' });
  } catch (err) {
    console.error('doPost failed: ' + (err && err.name ? err.name : 'Error'));   // never log the request or the key
    return reply_({ success: false, error: 'INTERNAL_ERROR' });
  }
}

function reply_(body) {
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}

function clean_(value) {
  return String(value === null || value === undefined ? '' : value).slice(0, MAX_TEXT_CHARS);
}

// The run ID to guard, or '' when the payload has none (empty, or the "local" default).
function runKey_(value) {
  const id = clean_(value).trim();
  return id === NO_RUN_ID ? '' : id;
}

// Runs work() with the script lock taken when there is a run ID, so two calls with the same run ID cannot both pass the check.
function withRunLock_(runId, work) {
  if (!runId) return work();
  const lock = LockService.getScriptLock();
  lock.waitLock(LOCK_WAIT_MS);
  try {
    return work();
  } finally {
    lock.releaseLock();
  }
}

// True when the tab already has this run ID. Reads only the Run ID column (the header row is skipped).
function hasRunId_(sheet, headers, runId) {
  const last = sheet.getLastRow();
  if (last < 2) return false;
  const column = headers.indexOf('Run ID') + 1;
  return sheet.getRange(2, column, last - 1, 1).getValues().some(function (r) { return String(r[0]).trim() === runId; });
}

function sheet_(props) {
  const id = String(props.getProperty('SPREADSHEET_ID') || '').trim();
  const spreadsheet = SpreadsheetApp.openById(id);
  // The tab name is matched without regard to letter case, so "agent" and "Agent" are the same tab.
  const wanted = SHEET_NAME.toLowerCase();
  let sheet = spreadsheet.getSheets().filter(function (s) { return s.getName().trim().toLowerCase() === wanted; })[0];
  if (!sheet) sheet = spreadsheet.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) sheet.appendRow(HEADERS);
  addReviewColumn_(sheet);
  return sheet;
}

// An Agent tab made before the Review column existed has 9 columns. Add the header cell (column J) and nothing else: the rows
// that are already there stay as they are, and their Review cell stays empty. A tab that already has 10 or more columns is left alone.
function addReviewColumn_(sheet) {
  if (sheet.getLastColumn() >= HEADERS.length) return;
  if (sheet.getMaxColumns() < HEADERS.length) sheet.insertColumnsAfter(sheet.getMaxColumns(), HEADERS.length - sheet.getMaxColumns());
  sheet.getRange(1, HEADERS.length).setValue(HEADERS[HEADERS.length - 1]);
}

// The agent's memory: the last `limit` rows, oldest first.
function history_(request, props) {
  const limit = Math.min(Math.max(Number(request.limit) || 5, 1), MAX_HISTORY_ROWS);
  const sheet = sheet_(props);
  const last = sheet.getLastRow();
  if (last < 2) return { success: true, decisions: [] };
  const first = Math.max(2, last - limit + 1);
  const rows = sheet.getRange(first, 1, last - first + 1, HEADERS.length).getValues();
  const decisions = rows.map(function (row) {
    const decision = {
      time: String(row[0]),
      alert: String(row[3]).toUpperCase() === 'TRUE',
      coins: String(row[4]).split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      message: String(row[5]),
      sources: String(row[6]).split('|').map(function (s) { return s.trim(); }).filter(Boolean),
    };
    if (request.detail === true) {   // only the executive brief asks for these; the agent's memory stays small
      decision.version = String(row[1]);
      decision.run_id = String(row[2]);
      decision.reasoning = String(row[7]);
      decision.blocked_reason = String(row[8]);
    }
    return decision;
  });
  return { success: true, decisions: decisions };
}

// One row per decision, and an email when there is an alert.
function save_(request, props) {
  if (typeof request.alert !== 'boolean' || !Array.isArray(request.coins) || !Array.isArray(request.sources)) {
    return { success: false, error: 'INVALID_PAYLOAD' };
  }
  const row = [
    clean_(request.time), clean_(request.version), clean_(request.run_id), request.alert ? 'TRUE' : 'FALSE',
    request.coins.map(clean_).join(', '), clean_(request.message), request.sources.map(clean_).join(' | '),
    clean_(request.reasoning), clean_(request.blocked_reason), clean_(request.review).slice(0, MAX_REVIEW_CHARS),
  ];
  const runId = runKey_(row[2]);
  return withRunLock_(runId, function () {
    const sheet = sheet_(props);
    if (runId && hasRunId_(sheet, HEADERS, runId)) return { success: true, duplicate: true };
    const rowNumber = sheet.getLastRow() + 1;
    sheet.getRange(rowNumber, 1, 1, HEADERS.length).setNumberFormat('@').setValues([row]);
    SpreadsheetApp.flush();

    let emailed = false;
    if (request.alert) {
      const to = String(props.getProperty('EMAIL_TO') || '').trim() || Session.getEffectiveUser().getEmail();
      MailApp.sendEmail({
        to: to,
        subject: 'Crypto watch alert: ' + row[4],
        body: row[5] + '\n\nCoins: ' + row[4] + '\nSources: ' + row[6] + '\nRun: ' + row[2] + ' (' + row[1] + ')\n\n' +
              'This project is for education only and is not financial advice.',
        name: 'Crypto Watch Agent',
      });
      emailed = true;
    }
    return { success: true, row: rowNumber, emailed: emailed };
  });
}

// The executive brief: the brief itself as the email body and the full interactive page as an attachment.
function report_(request, props) {
  const page = request.html;
  const summary = request.summary_html;
  if (typeof page !== 'string' || page.length === 0 || page.length > MAX_REPORT_CHARS ||
      typeof summary !== 'string' || summary.length === 0 || summary.length > MAX_SUMMARY_CHARS) {
    return { success: false, error: 'INVALID_PAYLOAD' };
  }
  const to = String(props.getProperty('EMAIL_TO') || '').trim() || Session.getEffectiveUser().getEmail();
  MailApp.sendEmail({
    to: to,
    subject: clean_(request.subject) || 'Crypto watch executive brief',
    body: 'Open this email in an HTML-capable mail program, and open the attached HTML file in a browser for the charts. ' +
          'This project is for education only and is not financial advice.',
    htmlBody: summary,
    attachments: [Utilities.newBlob(page, 'text/html', 'crypto-watch-executive-brief.html')],
    name: 'Crypto Watch Agent',
  });
  return { success: true, emailed: true };
}

// A cell: finite numbers stay numbers (so the Sheet can chart them), everything else is text. Text that starts with
// = + - or @ gets a leading apostrophe, so it is never run as a formula.
function cell_(value) {
  if (typeof value === 'number' && isFinite(value)) return value;
  if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE';
  const text = clean_(value);
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

// Rows for one of the extra tabs. Only the tab names in EXTRA_TABS are accepted.
function append_(request, props) {
  const headers = Object.prototype.hasOwnProperty.call(EXTRA_TABS, request.tab) ? EXTRA_TABS[request.tab] : null;
  if (!headers || !Array.isArray(request.rows) || request.rows.length === 0 || request.rows.length > MAX_APPEND_ROWS) {
    return { success: false, error: 'INVALID_PAYLOAD' };
  }
  const rows = [];
  for (let i = 0; i < request.rows.length; i++) {
    const row = request.rows[i];
    if (!Array.isArray(row) || row.length !== headers.length) return { success: false, error: 'INVALID_PAYLOAD' };
    rows.push(row.map(cell_));
  }
  // All rows of one request come from one run, so the first row's Run ID stands for the request.
  const runId = runKey_(rows[0][headers.indexOf('Run ID')]);
  return withRunLock_(runId, function () {
    const spreadsheet = SpreadsheetApp.openById(String(props.getProperty('SPREADSHEET_ID') || '').trim());
    let sheet = spreadsheet.getSheetByName(request.tab);
    if (!sheet) {
      sheet = spreadsheet.insertSheet(request.tab);
      sheet.appendRow(headers);
      sheet.setFrozenRows(1);
      sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold');
    }
    if (runId && hasRunId_(sheet, headers, runId)) return { success: true, duplicate: true };
    const first = sheet.getLastRow() + 1;
    sheet.getRange(first, 1, rows.length, headers.length).setValues(rows);
    SpreadsheetApp.flush();
    return { success: true, added: rows.length, first_row: first };
  });
}

// Run this ONCE from the editor (Run > authorizeOnce) to grant the Sheet and email permissions.
function authorizeOnce() {
  const props = PropertiesService.getScriptProperties();
  sheet_(props);
  MailApp.getRemainingDailyQuota();
  console.log('Authorized. Sheet reachable: ' + SHEET_NAME);
}
