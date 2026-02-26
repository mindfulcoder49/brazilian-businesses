/* ── Brazilian Business Finder — Frontend App ──────────────────────────── */

const API = '';  // same-origin; backend serves this file

// ── State ────────────────────────────────────────────────────────────────────
let currentRunId = null;
let ws = null;
let allCandidates = [];
let sortCol = 'brazil_score';
let sortDir = 'desc';
let statsInterval = null;

// ── DOM refs ─────────────────────────────────────────────────────────────────
const btnStart        = document.getElementById('btn-start');
const btnStop         = document.getElementById('btn-stop');
const btnRefresh      = document.getElementById('btn-refresh');
const btnEnrich       = document.getElementById('btn-enrich');
const btnScore        = document.getElementById('btn-score');
const btnClearLog     = document.getElementById('btn-clear-log');
const btnExport       = document.getElementById('btn-export');
const logStream       = document.getElementById('log-stream');
const logFilter       = document.getElementById('log-level-filter');
const logAutoscroll   = document.getElementById('log-autoscroll');
const runInfo         = document.getElementById('run-info');
const statusIndicator = document.getElementById('status-indicator');
const statsSummary    = document.getElementById('stats-summary');
const candidateFilter = document.getElementById('candidate-filter');
const tbody           = document.getElementById('candidates-tbody');
const noMsg           = document.getElementById('no-candidates');
const countBadge      = document.getElementById('candidate-count');
const enrichStatus    = document.getElementById('enrich-status');

// Search stats
const statQueries    = document.getElementById('stat-queries');
const statResults    = document.getElementById('stat-results');
const statCandidates = document.getElementById('stat-candidates');
const statDupes      = document.getElementById('stat-dupes');

// Enrichment stats
const statEnriched   = document.getElementById('stat-enriched');
const statPending    = document.getElementById('stat-pending');
const statTotalE     = document.getElementById('stat-total-e');

// Score stats
const statScored      = document.getElementById('stat-scored');
const statHigh        = document.getElementById('stat-high');
const statScorePending = document.getElementById('stat-score-pending');

// ── Utility ───────────────────────────────────────────────────────────────────
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString('en-US', { hour12: false });
}

function fmtJSON(data) {
  if (data === null || data === undefined) return '';
  if (typeof data === 'string') return data;
  return JSON.stringify(data);
}

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ── Log stream ────────────────────────────────────────────────────────────────
function appendLog(entry) {
  const { level = 'INFO', event = '', data, timestamp } = entry;
  if (event === 'PING') return;

  const levelFilter = logFilter.value;
  const hide = levelFilter && level !== levelFilter;

  const el = document.createElement('div');
  el.className = `log-entry log-${level}${hide ? ' hidden' : ''}`;
  el.dataset.level = level;

  const dataStr = data ? fmtJSON(data) : '';

  el.innerHTML = `
    <span class="log-ts">${fmtTime(timestamp)}</span>
    <span class="log-level">${level}</span>
    <span class="log-event">${event}</span>
    <span class="log-data">${escHtml(dataStr)}</span>
  `;
  logStream.appendChild(el);

  if (logAutoscroll.checked) {
    logStream.scrollTop = logStream.scrollHeight;
  }

  // Update stats on certain events
  if (event === 'RESULTS_PROCESSED' && data) {
    updateStatsFromLog(data);
  }
  if (event === 'NEW_CANDIDATE') {
    refreshCandidates();
  }
  if (event === 'RUN_COMPLETE' || event === 'RUN_STOPPED_BY_USER') {
    setStatus('done');
    btnStart.disabled = false;
    btnStop.disabled = true;
    stopStatsPolling();
    refreshCandidates();
  }
}

function updateStatsFromLog(data) {
  if (data.total_queries_run !== undefined) statQueries.textContent = data.total_queries_run;
  if (data.total_candidates !== undefined)  statCandidates.textContent = data.total_candidates;
}

logFilter.addEventListener('change', () => {
  const level = logFilter.value;
  document.querySelectorAll('.log-entry').forEach(el => {
    el.classList.toggle('hidden', level !== '' && el.dataset.level !== level);
  });
});

btnClearLog.addEventListener('click', () => { logStream.innerHTML = ''; });

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS(runId) {
  if (ws) { ws.close(); ws = null; }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/logs/${runId}`);

  ws.onopen = () => appendLog({ level: 'INFO', event: 'WS_CONNECTED', timestamp: new Date().toISOString() });

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      appendLog(msg);
    } catch { /* ignore malformed */ }
  };

  ws.onclose = () => appendLog({ level: 'WARN', event: 'WS_DISCONNECTED', timestamp: new Date().toISOString() });

  ws.onerror = (e) => appendLog({ level: 'ERROR', event: 'WS_ERROR', timestamp: new Date().toISOString() });
}

// ── Status indicator ──────────────────────────────────────────────────────────
function setStatus(s) {
  statusIndicator.className = `status-${s}`;
  const labels = { idle: '● Idle', running: '● Running', stopped: '● Stopped', done: '● Done' };
  statusIndicator.textContent = labels[s] || s;
}

// ── Stats polling ─────────────────────────────────────────────────────────────
function startStatsPolling(runId) {
  stopStatsPolling();
  statsInterval = setInterval(() => fetchRunStats(runId), 5000);
}

function stopStatsPolling() {
  if (statsInterval) { clearInterval(statsInterval); statsInterval = null; }
}

async function fetchRunStats(runId) {
  try {
    const data = await apiFetch(`/api/runs/${runId}`);
    const qs = data.query_stats || {};
    statQueries.textContent    = qs.done || 0;
    statResults.textContent    = qs.total_results || 0;
    statCandidates.textContent = data.candidate_count || 0;
    statDupes.textContent      = qs.total_dupes || 0;
    statsSummary.textContent   = `Run: ${runId.slice(0, 8)}… | Q: ${qs.done||0} | C: ${data.candidate_count||0}`;
  } catch { /* ignore */ }
}

// ── Enrichment (Phase 2) ──────────────────────────────────────────────────────
let enrichPollInterval = null;

async function fetchEnrichStatus() {
  try {
    const data = await apiFetch('/api/enrich/status');
    const e = data.enrichment || {};
    statEnriched.textContent = e.enriched ?? '—';
    statPending.textContent  = e.pending  ?? '—';
    statTotalE.textContent   = e.total    ?? '—';

    if (data.running) {
      btnEnrich.textContent = '⏳ Enriching…';
      btnEnrich.disabled = true;
      if (!enrichPollInterval) {
        enrichPollInterval = setInterval(fetchEnrichStatus, 3000);
      }
    } else {
      if (enrichPollInterval) { clearInterval(enrichPollInterval); enrichPollInterval = null; }
      btnEnrich.textContent = (e.pending > 0)
        ? `⬆ Enrich ${e.pending} Pending`
        : (e.total > 0 ? 'All Enriched ✓' : '⬆ Enrich All Pending');
      btnEnrich.disabled = (e.pending === 0);
      if (e.enriched > 0) refreshCandidates();
    }
  } catch { /* ignore */ }
}

btnEnrich.addEventListener('click', async () => {
  btnEnrich.disabled = true;
  btnEnrich.textContent = 'Starting…';
  try {
    const data = await apiFetch('/api/enrich', { method: 'POST' });
    appendLog({ level: 'INFO', event: 'ENRICH_STARTED', data: data, timestamp: new Date().toISOString() });
    await fetchEnrichStatus();
  } catch (e) {
    appendLog({ level: 'ERROR', event: 'ENRICH_FAILED', data: e.message, timestamp: new Date().toISOString() });
    btnEnrich.disabled = false;
    btnEnrich.textContent = '⬆ Enrich All Pending';
  }
});

// ── Scoring (Phase 3) ─────────────────────────────────────────────────────────
let scorePollInterval = null;

async function fetchScoreStatus() {
  try {
    const data = await apiFetch('/api/score/status');
    const s = data.scores || {};
    statScored.textContent       = s.scored ?? '—';
    statHigh.textContent         = s.high_confidence ?? '—';
    statScorePending.textContent = s.pending ?? '—';

    if (data.running) {
      const prog = data.progress || {};
      btnScore.textContent = prog.total > 0 ? `⏳ ${prog.done}/${prog.total}` : '⏳ Scoring…';
      btnScore.disabled = true;
      if (!scorePollInterval) {
        scorePollInterval = setInterval(fetchScoreStatus, 4000);
      }
    } else {
      if (scorePollInterval) { clearInterval(scorePollInterval); scorePollInterval = null; }
      btnScore.textContent = (s.pending > 0) ? `⭐ Score ${s.pending} Pending` : 'All Scored ✓';
      btnScore.disabled = (s.pending === 0);
    }
  } catch { /* ignore */ }
}

btnScore.addEventListener('click', async () => {
  btnScore.disabled = true;
  btnScore.textContent = 'Starting…';
  try {
    const data = await apiFetch('/api/score', { method: 'POST' });
    appendLog({ level: 'INFO', event: 'SCORE_STARTED', data: data, timestamp: new Date().toISOString() });
    await fetchScoreStatus();
  } catch (e) {
    appendLog({ level: 'ERROR', event: 'SCORE_FAILED', data: e.message, timestamp: new Date().toISOString() });
    btnScore.disabled = false;
    btnScore.textContent = '⭐ Score All Pending';
  }
});

// ── Start / Stop ──────────────────────────────────────────────────────────────
btnStart.addEventListener('click', async () => {
  btnStart.disabled = true;
  logStream.innerHTML = '';

  try {
    const data = await apiFetch('/api/runs', { method: 'POST' });
    currentRunId = data.run_id;

    runInfo.textContent = `Run ID: ${currentRunId} | Seed queries: ${data.seed_queries}`;
    runInfo.classList.remove('hidden');

    setStatus('running');
    btnStop.disabled = false;

    connectWS(currentRunId);
    startStatsPolling(currentRunId);
  } catch (e) {
    btnStart.disabled = false;
    appendLog({ level: 'ERROR', event: 'START_FAILED', data: e.message, timestamp: new Date().toISOString() });
  }
});

btnStop.addEventListener('click', async () => {
  if (!currentRunId) return;
  btnStop.disabled = true;
  try {
    await apiFetch(`/api/runs/${currentRunId}/stop`, { method: 'POST' });
    appendLog({ level: 'WARN', event: 'STOP_REQUESTED', timestamp: new Date().toISOString() });
  } catch (e) {
    appendLog({ level: 'ERROR', event: 'STOP_FAILED', data: e.message, timestamp: new Date().toISOString() });
  }
});

// btnRefresh handler defined near init() below

// ── Candidates ────────────────────────────────────────────────────────────────
async function refreshCandidates() {
  try {
    const data = await apiFetch('/api/candidates?limit=2000');
    allCandidates = data.candidates || [];
    renderCandidates();
  } catch { /* ignore */ }
}

function renderCandidates() {
  const filterText = candidateFilter.value.toLowerCase();

  let rows = allCandidates.filter(c => {
    if (!filterText) return true;
    return (c.display_name || '').toLowerCase().includes(filterText) ||
           (c.formatted_address || '').toLowerCase().includes(filterText);
  });

  // Sort — nulls always last regardless of direction
  rows.sort((a, b) => {
    let av = a[sortCol], bv = b[sortCol];
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (av < bv) return sortDir === 'asc' ? -1 : 1;
    if (av > bv) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  countBadge.textContent = rows.length;
  noMsg.style.display = rows.length === 0 ? '' : 'none';

  const renderedRows = rows.map(c => {
    const hitClass = c.hit_count > 1 ? 'multi' : '';
    const hitSources = JSON.stringify(c.query_sources || []).replace(/"/g, '&quot;');
    const hitName   = escHtml(c.display_name || c.place_id).replace(/"/g, '&quot;');
    const sources = (c.query_sources || []).slice(0, 4).map(s =>
      `<span class="source-pill" title="${escHtml(s)}">${escHtml(s)}</span>`
    ).join('');
    const mapLink = c.google_maps_uri
      ? `<a class="map-link" href="${escHtml(c.google_maps_uri)}" target="_blank" rel="noreferrer">↗</a>`
      : '';
    const status = c.business_status
      ? `<span class="status-tag status-${c.business_status}">${c.business_status.replace(/_/g, ' ')}</span>`
      : '—';
    const type = c.primary_type
      ? `<span class="type-tag">${c.primary_type}</span>`
      : '—';
    const enrichBadge = c.enriched
      ? ''
      : '<span style="font-size:9px; color:#546e7a; margin-left:4px;">(pending)</span>';

    const score = c.brazil_score;
    const scoreColor = score === null || score === undefined ? '#455a64'
      : score >= 90 ? '#00c853' : score >= 75 ? '#43a047'
      : score >= 50 ? '#f57c00' : score >= 20 ? '#e53935' : '#880e4f';
    const scoreBadge = score !== null && score !== undefined
      ? `<span title="${escHtml(c.score_reason||'')}" style="font-family:monospace;font-weight:700;font-size:13px;color:${scoreColor}">${score}</span>`
      : '<span style="color:#546e7a;font-size:11px">—</span>';

    return `<tr>
      <td><strong>${escHtml(c.display_name || c.place_id)}</strong>${enrichBadge}</td>
      <td>${escHtml(c.formatted_address || '—')}</td>
      <td>${type}</td>
      <td>${scoreBadge}</td>
      <td><span class="hit-badge ${hitClass}" data-name="${hitName}" data-sources="${hitSources}">${c.hit_count}</span></td>
      <td><div class="sources-list">${sources}${c.query_sources && c.query_sources.length > 4 ? `<span class="source-pill">+${c.query_sources.length - 4}</span>` : ''}</div></td>
      <td>${mapLink}</td>
    </tr>`;
  }).join('');

  tbody.innerHTML = renderedRows;

  tbody.querySelectorAll('.hit-badge').forEach(badge => {
    badge.addEventListener('click', () => {
      console.log('[hit-badge] direct click fired', badge.dataset);
      const name    = badge.dataset.name || '';
      const sources = JSON.parse(badge.dataset.sources || '[]');
      showSourcesModal(name, sources);
    });
  });
}

candidateFilter.addEventListener('input', renderCandidates);

// ── Sortable table headers ────────────────────────────────────────────────────
document.querySelectorAll('#candidates-table th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (sortCol === col) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = col;
      sortDir = (col === 'hit_count' || col === 'brazil_score') ? 'desc' : 'asc';
    }
    document.querySelectorAll('th').forEach(t => t.classList.remove('sorted-asc', 'sorted-desc'));
    th.classList.add(`sorted-${sortDir}`);
    renderCandidates();
  });
});

// ── Hit badge → sources popup ─────────────────────────────────────────────────

function showSourcesModal(name, sources) {
  console.log('[hit-badge] showSourcesModal called, backdrop:', document.getElementById('sources-modal-backdrop'));
  document.getElementById('sources-modal').innerHTML = `
    <div class="sm-header">
      <span class="sm-title">${name}</span>
      <button class="sm-close" id="sm-close-btn">✕</button>
    </div>
    <div class="sm-body">
      <div class="sm-count">${sources.length} matched quer${sources.length === 1 ? 'y' : 'ies'}</div>
      ${sources.map(s => `<div class="sm-pill">${escHtml(s)}</div>`).join('')}
    </div>
  `;
  document.getElementById('sm-close-btn').addEventListener('click', closeSourcesModal);
  document.getElementById('sources-modal-backdrop').classList.add('open');
}

function closeSourcesModal() {
  document.getElementById('sources-modal-backdrop').classList.remove('open');
}

document.getElementById('sources-modal-backdrop').addEventListener('click', e => {
  if (e.target.id === 'sources-modal-backdrop') closeSourcesModal();
});

// ── CSV Export ────────────────────────────────────────────────────────────────
btnExport.addEventListener('click', () => {
  if (!allCandidates.length) return;
  const headers = ['place_id', 'display_name', 'formatted_address', 'primary_type',
                   'latitude', 'longitude', 'business_status', 'hit_count', 'query_sources'];
  const rows = allCandidates.map(c =>
    headers.map(h => {
      const v = h === 'query_sources' ? (c[h] || []).join(' | ') : (c[h] ?? '');
      return `"${String(v).replace(/"/g, '""')}"`;
    }).join(',')
  );
  const csv = [headers.join(','), ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `brazilian_businesses_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
});

// Refresh button covers search stats, enrichment, and scoring
btnRefresh.addEventListener('click', async () => {
  if (currentRunId) await fetchRunStats(currentRunId);
  await fetchEnrichStatus();
  await fetchScoreStatus();
  await refreshCandidates();
});

// ── Init ───────────────────────────────────────────────────────────────────────
(async function init() {
  await refreshCandidates();
  await fetchEnrichStatus();
  await fetchScoreStatus();

  // Reconnect to any active run
  try {
    const data = await apiFetch('/api/runs');
    const activeRun = (data.runs || []).find(r => r.status === 'running');
    if (activeRun) {
      currentRunId = activeRun.run_id;
      runInfo.textContent = `Reconnected to run: ${currentRunId}`;
      runInfo.classList.remove('hidden');
      setStatus('running');
      btnStart.disabled = true;
      btnStop.disabled = false;
      connectWS(currentRunId);
      startStatsPolling(currentRunId);
    }
  } catch { /* ignore */ }
})();
