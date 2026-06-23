// Copyright (C) 2025 Luis González Fernández
// SPDX-License-Identifier: GPL-3.0-or-later

// ── Icons ──────────────────────────────────────────────────────────────
const ICO = {
  diag: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>',
  skeptic: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
  rebuttal: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
  mod: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="M7 21h10M12 3v18M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/></svg>',
  final: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg>',
  err: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
};

const AGENT_CFG = {
  diagnostic_agent:           { cls: 'a-diag',     ico: ICO.diag },
  skeptic_agent:              { cls: 'a-skeptic',   ico: ICO.skeptic },
  diagnostic_rebuttal_agent:  { cls: 'a-rebuttal',  ico: ICO.rebuttal },
};

const NEXT_LABEL = {
  diagnostic_agent:          'Skeptical Reviewer',
  skeptic_agent:             'Rebuttal',
  diagnostic_rebuttal_agent: 'Moderator',
};

const STATUS_CFG = {
  continue:               { lbl: 'Continue',      cls: 'db-continue' },
  final_diagnosis:        { lbl: 'Diagnosis',     cls: 'db-final_diagnosis' },
  needs_more_data:        { lbl: 'Missing data',  cls: 'db-needs_more_data' },
  propose_fix:            { lbl: 'Fix ready',     cls: 'db-propose_fix' },
  structured_uncertainty: { lbl: 'Uncertainty',   cls: 'db-structured_uncertainty' },
};

const RISK_CLS = { critical:'rc', high:'rh', medium:'rm', low:'rl' };

const APPROVAL_LBL = {
  auto:     ['ap-auto',     'auto'],
  approved: ['ap-approved', 'approved'],
  rejected: ['ap-rejected', 'rejected'],
  timeout:  ['ap-timeout',  'timeout'],
  cached:   ['ap-cached',   'cached'],
};

// ── State ──────────────────────────────────────────────────────────────
let source        = null;   // EventSource of the run being watched
let curRound      = 1;
let maxRounds     = 4;
let lastDecision  = null;
let viewRunId     = null;   // run currently displayed in the thread
let isLive        = false;  // true → watching a live run (buttons active)
let activeTab     = 'debate';
let defaultApproval = true;
const apprCards   = {};     // call_id → approval card element
let hitlBanner    = null;   // active HITL banner element
let liveAgent     = null;   // streaming agent card: { el, body, buffer, node, role, timer }
const toolCards   = {};     // call_id → running tool-call card (awaiting result)
const toolCardGroups = {};  // call_id → grouped tool-call container
let activeToolGroup = null; // consecutive tool-call group currently receiving cards

// ── DOM ────────────────────────────────────────────────────────────────
const form        = document.getElementById('run-form');
const thread      = document.getElementById('thread');
const histPanel   = document.getElementById('hist-panel');
const pill        = document.getElementById('status-pill');
const pillTxt     = document.getElementById('status-text');
const btn         = document.getElementById('start-button');
const stopBtn     = document.getElementById('stop-button');
const emptyState  = document.getElementById('empty-state');
const typing      = document.getElementById('typing-indicator');
const typingLbl   = document.getElementById('typing-label');
const tabDebate   = document.getElementById('tab-debate');
const tabMap      = document.getElementById('tab-map');
const tabHist     = document.getElementById('tab-hist');
const mapPanel    = document.getElementById('map-panel');
const btnExport   = document.getElementById('btn-export');
const btnResume   = document.getElementById('btn-resume');
const resumePanel = document.getElementById('resume-panel');

// ── Mobile sidebar toggle ─────────────────────────────────────────────
const sidebar = document.querySelector('.sidebar');
const mobileMenuBtn = document.getElementById('mobile-menu-btn');
const mobileOverlay = document.getElementById('mobile-overlay');

function initMobileSidebar() {
  if (!mobileMenuBtn || !sidebar || !mobileOverlay) return;

  mobileMenuBtn.addEventListener('click', () => {
    sidebar.classList.add('open');
    mobileOverlay.classList.add('open');
  });

  mobileOverlay.addEventListener('click', () => {
    sidebar.classList.remove('open');
    mobileOverlay.classList.remove('open');
  });

  // Auto-close when starting a debate
  form.addEventListener('submit', () => {
    sidebar.classList.remove('open');
    mobileOverlay.classList.remove('open');
  });

  // Close when clicking a conversation tab
  tabDebate.addEventListener('click', () => {
    sidebar.classList.remove('open');
    mobileOverlay.classList.remove('open');
  });
  tabHist.addEventListener('click', () => {
    sidebar.classList.remove('open');
    mobileOverlay.classList.remove('open');
  });
  tabMap.addEventListener('click', () => {
    sidebar.classList.remove('open');
    mobileOverlay.classList.remove('open');
  });
}

// ── Helpers ────────────────────────────────────────────────────────────
const esc = s => String(s || '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function md(text) {
  try {
    const html = marked.parse(String(text || ''), { breaks: true, gfm: true });
    return DOMPurify.sanitize(html);
  } catch { return '<p>' + esc(text) + '</p>'; }
}

function rlist(items) {
  if (!items || !items.length) return '<p class="msect-body it">—</p>';
  return '<ul class="mlist">' + items.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul>';
}

function setStatus(state, text) {
  pill.className = 'status-pill ' + state;
  pillTxt.textContent = text;
}

function showTyping(label) {
  typingLbl.textContent = label + ' analyzing...';
  typing.classList.remove('hidden');
  scrollBottom();
}
function hideTyping() { typing.classList.add('hidden'); }

// Auto-scroll only when the user is already near the bottom, so reading
// earlier cards isn't interrupted by streaming deltas. force=true overrides
// (used when a brand-new card is pushed).
function scrollBottom(force = false) {
  const near = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 220;
  if (force || near) thread.scrollTop = thread.scrollHeight;
}

function fmtDuration(ms) {
  if (ms == null) return '';
  return ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(1) + ' s';
}

function push(el) {
  el.classList.add('fresh');
  thread.insertBefore(el, typing);
  setTimeout(() => el.classList.remove('fresh'), 1500);
  scrollBottom(true);
}

function clearThread() {
  Array.from(thread.children).forEach(c => {
    if (c.id !== 'empty-state' && c.id !== 'typing-indicator') c.remove();
  });
  emptyState.classList.remove('hidden');
  hideTyping();
  Object.keys(apprCards).forEach(k => delete apprCards[k]);
  Object.keys(toolCards).forEach(k => delete toolCards[k]);
  Object.keys(toolCardGroups).forEach(k => delete toolCardGroups[k]);
  if (liveAgent && liveAgent.timer) clearTimeout(liveAgent.timer);
  liveAgent = null;
  activeToolGroup = null;
  hitlBanner = null;
  hidePostRunActions();
  hideResumePanel();
  HypoMap.reset();
}

function addRoundSep(round, total) {
  const d = document.createElement('div');
  d.className = 'round-sep';
  d.innerHTML = '<span>Round ' + round + ' <em>of ' + total + '</em></span>';
  thread.insertBefore(d, typing);
}

function showStop() { stopBtn.classList.remove('hidden'); }
function hideStop() { stopBtn.classList.add('hidden'); }

function closeSource() {
  if (source) { source.close(); source = null; }
}

function showPostRunActions() {
  btnExport.classList.remove('hidden');
  btnResume.classList.remove('hidden');
}
function hidePostRunActions() {
  btnExport.classList.add('hidden');
  btnResume.classList.add('hidden');
}

const CHEVRON = '<svg class="acard-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';

// ── Tab switching ──────────────────────────────────────────────────────
tabDebate.addEventListener('click', showDebateTab);
tabMap.addEventListener('click', showMapTab);
tabHist.addEventListener('click', showHistoryTab);

function showDebateTab() {
  activeTab = 'debate';
  tabDebate.classList.add('active');
  tabHist.classList.remove('active');
  tabMap.classList.remove('active');
  thread.style.display = 'flex';
  histPanel.classList.remove('active');
  mapPanel.classList.remove('active');
}

function showMapTab() {
  activeTab = 'map';
  tabMap.classList.add('active');
  tabDebate.classList.remove('active');
  tabHist.classList.remove('active');
  thread.style.display = 'none';
  histPanel.classList.remove('active');
  mapPanel.classList.add('active');
  HypoMap.show();
}

function showHistoryTab() {
  activeTab = 'history';
  tabHist.classList.add('active');
  tabDebate.classList.remove('active');
  tabMap.classList.remove('active');
  thread.style.display = 'none';
  histPanel.classList.add('active');
  mapPanel.classList.remove('active');
  loadHistory();
}

// ── Live option toggles ────────────────────────────────────────────────
// Changing these checkboxes while a debate is running updates the run's
// RunControl on the server: unchecking approval auto-approves pending
// requests; unchecking the pause releases a waiting round gate.
document.getElementById('require_approval').addEventListener('change', e => {
  syncRunOptions({ require_approval: e.target.checked });
});
document.getElementById('pause_rounds').addEventListener('change', e => {
  syncRunOptions({ pause_between_rounds: e.target.checked });
});

async function syncRunOptions(opts) {
  if (!isLive || !viewRunId) return;
  try {
    await fetch('/api/runs/' + viewRunId + '/options', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    });
  } catch (_) { /* next toggle retries */ }
}

// ── Stop / cancel ──────────────────────────────────────────────────────
stopBtn.addEventListener('click', stopRun);

async function stopRun() {
  if (!viewRunId || !isLive) return;
  stopBtn.disabled = true;
  try {
    await fetch('/api/runs/' + viewRunId, { method: 'DELETE' });
  } catch (_) { /* server will timeout and clean up */ }
  // run_cancelled SSE event drives the UI update
}

// ── Card builders ──────────────────────────────────────────────────────
function buildAgentCard(event) {
  const cfg = AGENT_CFG[event.node] || { cls: 'a-diag', ico: ICO.diag };
  const el = document.createElement('article');
  el.className = 'acard ' + cfg.cls;
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + cfg.ico + '</div>' +
        '<div><span class="agent-nm">' + esc(event.role) + '</span>' +
             '<span class="agent-sub">Round ' + curRound + '</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body">' + md(event.content) + '</div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

function buildReasoningCard(event) {
  const cfg = AGENT_CFG[event.agent_node] || { cls: 'a-diag', ico: ICO.diag };
  const el = document.createElement('article');
  el.className = 'acard acard--reasoning ' + cfg.cls;
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + cfg.ico + '</div>' +
        '<div><span class="agent-nm">' + esc(event.agent_role) + '</span>' +
             '<span class="agent-sub agent-sub--reasoning">Reasoning</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body">' + md(event.content) + '</div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

// ── Live streaming agent card ──────────────────────────────────────────
// Created on agent_turn_started, fed token deltas by agent_delta, and frozen
// in place by the turn's closing event (agent_reasoning / agent_completed).
function ensureLiveAgent(node, role) {
  // Reuse the same node's card (reactivate if frozen by reasoning)
  if (liveAgent && liveAgent.node === node) {
    if (!liveAgent.el.classList.contains('acard--streaming')) {
      // Reactivating from a frozen (reasoning) state: start a fresh streaming
      // iteration. Reset the buffer so we accumulate only the new LLM response.
      liveAgent.el.classList.add('acard--streaming');
      liveAgent.el.classList.remove('acard--reasoning');
      const sub = liveAgent.el.querySelector('.agent-sub');
      sub.classList.remove('agent-sub--reasoning');
      sub.classList.add('agent-sub--live');
      sub.textContent = 'Reasoning in real time…';
      liveAgent.buffer = '';
      if (liveAgent.timer) { clearTimeout(liveAgent.timer); liveAgent.timer = null; }
    }
    return liveAgent;
  }
  if (liveAgent) finalizeLiveAgent();
  const cfg = AGENT_CFG[node] || { cls: 'a-diag', ico: ICO.diag };
  const el = document.createElement('article');
  el.className = 'acard acard--streaming ' + cfg.cls;
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + cfg.ico + '</div>' +
        '<div><span class="agent-nm">' + esc(role) + '</span>' +
             '<span class="agent-sub agent-sub--live">Reasoning in real time…</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body"></div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  liveAgent = { el, body: el.querySelector('.card-body'), buffer: '', node, role, timer: null, frozenHTML: '' };
  hideTyping();
  push(el);
  return liveAgent;
}

function appendLiveDelta(ev) {
  const live = ensureLiveAgent(ev.agent_node, ev.agent_role);
  live.buffer += ev.delta || '';
  // Throttle markdown re-rendering: deltas arrive far faster than 8 fps.
  if (!live.timer) {
    live.timer = setTimeout(() => {
      live.timer = null;
      // Preserve frozen content (e.g., prior reasoning) while appending fresh deltas
      live.body.innerHTML = live.frozenHTML + md(live.buffer);
      scrollBottom();
    }, 120);
  }
}

// Freeze the streaming card with its definitive content.
// kind: 'reasoning' | 'final'. Returns false when there is no live card
// (e.g. replaying a stored run), so the caller builds a static card instead.
function finalizeLiveAgent(content, kind, subtitle) {
  if (!liveAgent) return false;
  const live = liveAgent;
  // Release the reference ONLY if the freeze is definitive ('final')
  // or if called without kind (agent change in ensureLiveAgent).
  // When kind === 'reasoning', we keep liveAgent active so that
  // ensureLiveAgent can reuse the card in the next LLM iteration
  // within the same ReAct loop.
  if (!kind || kind === 'final') {
    liveAgent = null;
  }
  if (live.timer) clearTimeout(live.timer);
  const text = content != null ? content : live.buffer;
  if (!String(text).trim()) {
    if (live.frozenHTML) {
      // Nothing new to show, but there is frozen reasoning from earlier
      // iterations: keep the card instead of removing it from the DOM.
      live.el.classList.remove('acard--streaming');
      live.el.classList.add('acard--reasoning');
      const sub = live.el.querySelector('.agent-sub');
      sub.classList.remove('agent-sub--live');
      sub.classList.add('agent-sub--reasoning');
      sub.textContent = 'Reasoning';
      return true;
    }
    live.el.remove();
    // 'final' close with no content or reasoning: return false so the
    // caller builds the static card (buildAgentCard) from the original event.
    return kind !== 'final';
  }
  live.el.classList.remove('acard--streaming');
  if (kind === 'reasoning') live.el.classList.add('acard--reasoning');
  live.body.innerHTML = md(text);
  if (kind === 'reasoning') {
    // Snapshot AFTER rendering so subsequent iterations append the definitive
    // rendered HTML, not stale partial streaming content.
    live.frozenHTML = live.body.innerHTML;
  }
  const sub = live.el.querySelector('.agent-sub');
  sub.classList.remove('agent-sub--live');
  if (kind === 'reasoning') {
    sub.classList.add('agent-sub--reasoning');
    sub.textContent = 'Reasoning';
  } else {
    sub.textContent = subtitle || ('Round ' + curRound);
  }
  scrollBottom();
  return true;
}

function buildUserCard(content) {
  const el = document.createElement('article');
  el.className = 'acard a-user';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.user + '</div>' +
        '<div><span class="agent-nm">Operator</span>' +
             '<span class="agent-sub">Comment between rounds</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body">' + md(content) + '</div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

function buildModCard(decision, round) {
  lastDecision = decision;
  const st  = STATUS_CFG[decision.status] || { lbl: decision.status, cls: 'db-structured_uncertainty' };
  const pct = Math.round((decision.confidence || 0) * 100);
  const barColor = '#1a1a1a';
  const rk  = RISK_CLS[decision.risk_level] || 'ru';

  const el = document.createElement('article');
  el.className = 'acard a-mod';

  let body = '';

  if (decision.leading_hypothesis) {
    body += '<div><div class="msect-title">Leading hypothesis</div>' +
            '<div class="msect-body">' + esc(decision.leading_hypothesis) + '</div></div>';
  }

  if (decision.next_step) {
    body += '<div><div class="msect-title">Next step</div>' +
            '<div class="msect-body">' + esc(decision.next_step) + '</div></div>';
  }

  const evRow = (decision.evidence && decision.evidence.length) || (decision.missing_evidence && decision.missing_evidence.length);
  if (evRow) {
    body += '<div class="mod-row2">';
    if (decision.evidence && decision.evidence.length) {
      body += '<div><div class="msect-title">Evidence (' + decision.evidence.length + ')</div>' +
              rlist(decision.evidence) + '</div>';
    }
    if (decision.missing_evidence && decision.missing_evidence.length) {
      body += '<div><div class="msect-title">Missing evidence</div>' +
              rlist(decision.missing_evidence) + '</div>';
    }
    body += '</div>';
  }

  if (decision.recommended_fix) {
    body += '<div class="fix-box"><div class="msect-title">Recommended fix</div>' +
            '<div class="msect-body">' + esc(decision.recommended_fix) + '</div></div>';
  }

  const rejRow = (decision.rejected_hypotheses && decision.rejected_hypotheses.length) || (decision.validation && decision.validation.length);
  if (rejRow) {
    body += '<div class="mod-row2">';
    if (decision.rejected_hypotheses && decision.rejected_hypotheses.length) {
      body += '<div><div class="msect-title">Rejected hypotheses</div>' +
              rlist(decision.rejected_hypotheses) + '</div>';
    }
    if (decision.validation && decision.validation.length) {
      body += '<div><div class="msect-title">Validation steps</div>' +
              rlist(decision.validation) + '</div>';
    }
    body += '</div>';
  }

  if (decision.stop_reason) {
    body += '<div><div class="msect-title">Stop reason</div>' +
            '<div class="msect-body it">' + esc(decision.stop_reason) + '</div></div>';
  }

  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.mod + '</div>' +
        '<div><span class="agent-nm">Moderator</span>' +
             '<span class="agent-sub">Round ' + round + ' · Decision</span></div>' +
      '</div>' +
      '<div style="display:flex;align-items:center;gap:7px">' +
        '<span class="rbadge ' + rk + '">' + esc(decision.risk_level || '?') + '</span>' +
        '<span class="dbadge ' + st.cls + '">' + st.lbl + '</span>' +
        CHEVRON +
      '</div>' +
    '</div>' +
    '<div class="conf-row">' +
      '<span class="conf-lbl">Confidence</span>' +
      '<div class="conf-track"><div class="conf-fill" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
      '<span class="conf-val">' + pct + '%</span>' +
    '</div>' +
    '<div class="mod-body">' + body + '</div>';

  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

function buildFinalCard(decision) {
  const pct  = Math.round((decision.confidence || 0) * 100);
  const rk   = RISK_CLS[decision.risk_level] || 'ru';
  const st   = STATUS_CFG[decision.status] || { lbl: decision.status, cls: 'db-structured_uncertainty' };
  const rnds = curRound;

  let extra = '';
  if (decision.recommended_fix) {
    extra = '<div class="mod-body" style="border-top:1px solid var(--border)">' +
            '<div><div class="msect-title">Recommended fix</div>' +
            '<div class="msect-body">' + esc(decision.recommended_fix) + '</div></div>' +
            '</div>';
  }

  const el = document.createElement('article');
  el.className = 'acard a-final';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.final + '</div>' +
        '<div><span class="agent-nm">Complete diagnosis</span>' +
             '<span class="agent-sub">Debate closed · ' + rnds + ' round' + (rnds > 1 ? 's' : '') + '</span></div>' +
      '</div>' +
      '<span class="dbadge ' + st.cls + '">' + st.lbl + '</span>' +
    '</div>' +
    '<div class="final-stats">' +
      '<div class="fstat"><span class="fstat-lbl">Confidence</span><span class="fstat-val">' + pct + '%</span></div>' +
      '<div class="fstat"><span class="fstat-lbl">Rounds</span><span class="fstat-val">' + rnds + '</span></div>' +
      '<div class="fstat"><span class="fstat-lbl">Risk</span><span class="fstat-val"><span class="rbadge ' + rk + '">' + esc(decision.risk_level || '?') + '</span></span></div>' +
    '</div>' +
    extra;

  return el;
}

function buildErrCard(msg) {
  const el = document.createElement('article');
  el.className = 'acard a-err';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.err + '</div>' +
        '<div><span class="agent-nm">Error</span><span class="agent-sub">Failure during the debate</span></div>' +
      '</div>' +
    '</div>' +
    '<div class="card-body">' + esc(msg) + '</div>';
  return el;
}

function buildInfoCard(title, msg) {
  const el = document.createElement('article');
  el.className = 'acard a-info';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.info + '</div>' +
        '<div><span class="agent-nm">' + esc(title) + '</span></div>' +
      '</div>' +
    '</div>' +
    '<div class="card-body">' + esc(msg) + '</div>';
  return el;
}

const ROLE_LABELS = {
  diagnostic_agent:         'Diagnosis',
  skeptic_agent:            'Skeptic',
  diagnostic_rebuttal_agent: 'Rebuttal',
  moderator_agent:          'Moderator',
  summarize_history:        'Summary',
};

function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1000)      return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function buildTokenStatsCard(tokenTotals, costEstimate) {
  if (!tokenTotals || !tokenTotals.by_node) return null;
  const byNode = tokenTotals.by_node;
  const total  = tokenTotals.total || {};
  const byNodeCost = (costEstimate || {}).by_node || {};
  const totalUsd = (costEstimate || {}).total_usd;

  const rows = Object.entries(byNode).map(([node, counts]) => {
    const nodeCost = byNodeCost[node] ? byNodeCost[node].estimated_usd : null;
    const costCell = nodeCost != null ? '$' + nodeCost.toFixed(4) : '—';
    return '<tr>' +
      '<td>' + esc(ROLE_LABELS[node] || node) + '</td>' +
      '<td>' + esc(fmtTokens(counts.input_tokens))  + '</td>' +
      '<td>' + esc(fmtTokens(counts.output_tokens)) + '</td>' +
      '<td>' + esc(fmtTokens(counts.total_tokens))  + '</td>' +
      '<td>' + esc(costCell) + '</td>' +
      '</tr>';
  }).join('');

  const totalCostCell = totalUsd != null ? '$' + totalUsd.toFixed(4) : '—';

  const el = document.createElement('div');
  el.className = 'token-stats-card';
  el.innerHTML =
    '<div class="token-stats-head">&#128200; Token consumption</div>' +
    '<table class="token-stats-table">' +
      '<thead><tr>' +
        '<th>Agent</th><th>Input</th><th>Output</th><th>Total</th><th>Est. cost</th>' +
      '</tr></thead>' +
      '<tbody>' + rows + '</tbody>' +
      '<tfoot><tr class="token-stats-total">' +
        '<td>TOTAL</td>' +
        '<td>' + esc(fmtTokens(total.input_tokens))  + '</td>' +
        '<td>' + esc(fmtTokens(total.output_tokens)) + '</td>' +
        '<td>' + esc(fmtTokens(total.total_tokens))  + '</td>' +
        '<td>' + esc(totalCostCell) + '</td>' +
      '</tr></tfoot>' +
    '</table>' +
    ((costEstimate && !costEstimate.has_prices)
      ? '<div class="token-stats-cost">Price unavailable for one or more models. Configure <code>MODEL_PRICES_FILE</code> for accurate estimates.</div>'
      : '');
  return el;
}

const TOOL_ICO = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';

function buildToolCallCard(ev) {
  const isErr = !!ev.error;
  const card  = document.createElement('div');
  card.className = 'tc-card' + (isErr ? ' tc-err' : '');

  const argsStr   = JSON.stringify(ev.args || {}, null, 2);
  const resultStr = String(ev.result || '');
  const ap = APPROVAL_LBL[ev.approval] || APPROVAL_LBL.auto;
  const apBadge = ev.approval && ev.approval !== 'auto'
    ? '<span class="tc-appr ' + ap[0] + '">' + ap[1] + '</span>' : '';
  const dur = fmtDuration(ev.duration_ms);
  const durBadge = dur ? '<span class="tc-dur">' + esc(dur) + '</span>' : '';

  card.innerHTML =
    '<div class="tc-head">' +
      '<span class="tc-ico">' + TOOL_ICO + '</span>' +
      '<span class="tc-name">' + esc(ev.tool_name) + '</span>' +
      apBadge + durBadge +
      '<span class="tc-agent">' + esc(ev.agent_role || ev.agent_node) + '</span>' +
      '<span class="tc-arrow">&#9654;</span>' +
    '</div>' +
    '<div class="tc-body">' +
      '<div class="tc-section-lbl">Arguments</div>' +
      '<pre class="tc-pre">' + esc(argsStr) + '</pre>' +
      '<div class="tc-section-lbl" style="margin-top:8px">Result' + (isErr ? ' (error)' : '') + '</div>' +
      '<pre class="tc-pre">' + esc(resultStr) + '</pre>' +
    '</div>';

  card.querySelector('.tc-head').addEventListener('click', () => {
    card.classList.toggle('open');
  });

  return card;
}

// Card shown the moment a tool starts executing (tool_call_started); the
// matching tool_call event replaces it in place with the full result card.
function buildRunningToolCard(ev) {
  const card = document.createElement('div');
  card.className = 'tc-card tc-running';
  card.innerHTML =
    '<div class="tc-head">' +
      '<span class="tc-spin"></span>' +
      '<span class="tc-name">' + esc(ev.tool_name) + '</span>' +
      '<span class="tc-status">running&hellip;</span>' +
      '<span class="tc-agent">' + esc(ev.agent_role || ev.agent_node) + '</span>' +
      '<span class="tc-arrow">&#9654;</span>' +
    '</div>' +
    '<div class="tc-body">' +
      '<div class="tc-section-lbl">Arguments</div>' +
      '<pre class="tc-pre">' + esc(JSON.stringify(ev.args || {}, null, 2)) + '</pre>' +
    '</div>';
  card.querySelector('.tc-head').addEventListener('click', () => card.classList.toggle('open'));
  return card;
}

function closeToolGroup() {
  activeToolGroup = null;
}

function ensureToolGroup(ev) {
  if (activeToolGroup) return activeToolGroup;

  const group = {
    el: document.createElement('div'),
    body: null,
    countEl: null,
    errEl: null,
    runEl: null,
    agentEl: null,
    count: 0,
    errors: 0,
    running: 0,
    agents: new Set(),
  };
  group.el.className = 'tc-group collapsed';
  group.el.innerHTML =
    '<div class="tc-group-head">' +
      '<span class="tc-group-ico">' + TOOL_ICO + '</span>' +
      '<span class="tc-group-title">Tools</span>' +
      '<span class="tc-group-count">0 calls</span>' +
      '<span class="tc-group-errors hidden">0 errors</span>' +
      '<span class="tc-group-running hidden">running&hellip;</span>' +
      '<span class="tc-group-agent"></span>' +
      '<span class="tc-group-arrow">&#9654;</span>' +
    '</div>' +
    '<div class="tc-group-body"></div>';
  group.body = group.el.querySelector('.tc-group-body');
  group.countEl = group.el.querySelector('.tc-group-count');
  group.errEl = group.el.querySelector('.tc-group-errors');
  group.runEl = group.el.querySelector('.tc-group-running');
  group.agentEl = group.el.querySelector('.tc-group-agent');
  group.el.querySelector('.tc-group-head').addEventListener('click', () => {
    group.el.classList.toggle('collapsed');
  });

  activeToolGroup = group;
  updateToolGroup(group, ev);
  push(group.el);
  return group;
}

function updateToolGroup(group, ev) {
  const agent = ev && (ev.agent_role || ev.agent_node);
  if (agent) group.agents.add(agent);

  const label = group.count === 1 ? '1 call' : group.count + ' calls';
  group.countEl.textContent = label;

  if (group.errors > 0) {
    group.el.classList.add('has-errors');
    group.errEl.classList.remove('hidden');
    group.errEl.textContent = group.errors === 1 ? '1 error' : group.errors + ' errors';
  } else {
    group.el.classList.remove('has-errors');
    group.errEl.classList.add('hidden');
  }

  if (group.running > 0) {
    group.runEl.classList.remove('hidden');
  } else {
    group.runEl.classList.add('hidden');
  }

  if (group.agents.size === 1) {
    group.agentEl.textContent = Array.from(group.agents)[0];
  } else if (group.agents.size > 1) {
    group.agentEl.textContent = 'Several agents';
  } else {
    group.agentEl.textContent = '';
  }
}

function appendToolCardToGroup(group, card, ev) {
  group.body.appendChild(card);
  updateToolGroup(group, ev);
}

// ── Tool approval cards ────────────────────────────────────────────────
function buildApprovalCard(ev) {
  const card = document.createElement('div');
  card.className = 'appr-card';
  card.innerHTML =
    '<div class="appr-head">' +
      '<span class="appr-name">&#9888; Approval required: ' + esc(ev.tool_name) + '</span>' +
      '<span class="appr-agent">' + esc(ev.agent_role || '') + '</span>' +
    '</div>' +
    '<div class="appr-body"><pre class="tc-pre">' +
      esc(JSON.stringify(ev.args || {}, null, 2)) +
    '</pre></div>' +
    '<div class="appr-actions">' +
      '<button class="btn-sm btn-approve" type="button">Approve</button>' +
      '<button class="btn-sm btn-reject" type="button">Reject</button>' +
    '</div>' +
    '<div class="appr-status hidden"></div>';

  const actions = card.querySelector('.appr-actions');
  if (!isLive) {
    actions.classList.add('hidden');
  } else {
    card.querySelector('.btn-approve').addEventListener('click', () => resolveApproval(ev.call_id, true, card));
    card.querySelector('.btn-reject').addEventListener('click', () => resolveApproval(ev.call_id, false, card));
  }
  apprCards[ev.call_id] = card;
  return card;
}

async function resolveApproval(callId, approved, card) {
  card.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    await fetch('/api/runs/' + viewRunId + '/approval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ call_id: callId, approved }),
    });
  } catch (_) {
    card.querySelectorAll('button').forEach(b => { b.disabled = false; });
  }
  // tool_approval_resolved SSE event updates the card
}

function markApprovalResolved(ev) {
  const card = apprCards[ev.call_id];
  if (!card) return;
  card.querySelector('.appr-actions').classList.add('hidden');
  const st = card.querySelector('.appr-status');
  st.classList.remove('hidden');
  if (ev.approved) {
    st.classList.add('ok');
    st.textContent = '✓ Approved by the operator — running...';
  } else {
    st.classList.add('no');
    st.textContent = ev.resolution === 'timeout'
      ? '✕ No response (timeout) — not executed'
      : '✕ Rejected by the operator — not executed';
  }
}

// ── HITL banner ────────────────────────────────────────────────────────
function showHitlBanner(round) {
  removeHitlBanner();
  const div = document.createElement('div');
  div.className = 'hitl-banner';
  div.innerHTML =
    '<div class="hitl-title">The debate is paused: add context before round ' + esc(String(round)) + '</div>' +
    '<textarea placeholder="Comment, new data, or instruction for the agents (optional)..."></textarea>' +
    '<div class="hitl-actions">' +
      '<button class="btn-sm btn-open" type="button" data-act="send">Send comment</button>' +
      '<button class="btn-sm btn-del" type="button" data-act="skip">Continue without comment</button>' +
    '</div>';
  div.querySelector('[data-act="send"]').addEventListener('click', () => submitComment(div.querySelector('textarea').value, div));
  div.querySelector('[data-act="skip"]').addEventListener('click', () => submitComment('', div));
  hitlBanner = div;
  thread.insertBefore(div, typing);
  scrollBottom();
}

function removeHitlBanner() {
  if (hitlBanner) { hitlBanner.remove(); hitlBanner = null; }
}

async function submitComment(text, div) {
  div.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    await fetch('/api/runs/' + viewRunId + '/comment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ comment: text }),
    });
  } catch (_) {
    div.querySelectorAll('button').forEach(b => { b.disabled = false; });
  }
  // user_comment SSE event removes the banner
}

// ── Event rendering ────────────────────────────────────────────────────
function renderEvent(ev) {
  if (ev.type === 'agent_turn_started') {
    ensureLiveAgent(ev.agent_node, ev.agent_role);
    return;
  }

  if (ev.type === 'agent_delta') {
    appendLiveDelta(ev);
    return;
  }

  if (ev.type === 'tool_call_started') {
    const group = ensureToolGroup(ev);
    const card = buildRunningToolCard(ev);
    if (ev.call_id) toolCards[ev.call_id] = card;
    if (ev.call_id) toolCardGroups[ev.call_id] = group;
    group.count += 1;
    group.running += 1;
    appendToolCardToGroup(group, card, ev);
    scrollBottom(true);
    return;
  }

  if (ev.type === 'tool_call') {
    const running = ev.call_id ? toolCards[ev.call_id] : null;
    const group = (ev.call_id && toolCardGroups[ev.call_id]) || ensureToolGroup(ev);
    const card = buildToolCallCard(ev);
    if (running) {
      // Update the "running…" card in place, keeping its expanded state.
      if (running.classList.contains('open')) card.classList.add('open');
      running.replaceWith(card);
      group.running = Math.max(0, group.running - 1);
      delete toolCards[ev.call_id];
      delete toolCardGroups[ev.call_id];
    } else {
      group.count += 1;
      appendToolCardToGroup(group, card, ev);
    }
    if (ev.error) group.errors += 1;
    updateToolGroup(group, ev);
    scrollBottom();
    return;
  }

  // agent_reasoning is emitted between ReAct iterations (after tool calls finish
  // and before a new LLM call). It must NOT close the active tool group, because
  // the same agent may do more tool calls right after — closing would create a
  // second group card.
  if (ev.type === 'agent_reasoning') {
    if (!finalizeLiveAgent(ev.content, 'reasoning')) push(buildReasoningCard(ev));
    return;
  }

  if (ev.type === 'hypothesis_update') {
    HypoMap.update(ev);
    return;
  }

  closeToolGroup();

  if (ev.type === 'tool_approval_request') {
    push(buildApprovalCard(ev));
    if (isLive) setStatus('waiting', 'Waiting for approval');
    return;
  }

  if (ev.type === 'tool_approval_resolved') {
    markApprovalResolved(ev);
    if (isLive) setStatus('running', 'Round ' + curRound + ' of ' + maxRounds);
    return;
  }

  if (ev.type === 'awaiting_user_input') {
    if (isLive) {
      hideTyping();
      showHitlBanner(ev.round);
      setStatus('waiting', 'Waiting for comment');
    }
    return;
  }

  if (ev.type === 'user_comment') {
    removeHitlBanner();
    if (ev.content) push(buildUserCard(ev.content));
    if (isLive) {
      setStatus('running', 'Round ' + curRound + ' of ' + maxRounds);
      showTyping('Primary Diagnosis');
    }
    return;
  }

  if (ev.type === 'run_resumed') {
    const banner = document.createElement('div');
    banner.className = 'resumed-banner';
    banner.innerHTML = '&#8635; Debate resumed with new evidence' +
      (ev.parent_topic ? ' &nbsp;&middot;&nbsp; <strong>' + esc(ev.parent_topic) + '</strong>' : '');
    thread.insertBefore(banner, typing);
    return;
  }

  if (ev.type === 'reasoning_effort_ignored') {
    push(buildInfoCard(
      'Thinking level ignored',
      'The model "' + (ev.model || '?') + '" of the ' + (ev.agent_role || '?') +
      ' agent does not support a thinking level; the value "' + (ev.requested_effort || '?') + '" was ignored.'
    ));
    return;
  }

  if (ev.type === 'agent_skipped') {
    hideTyping();
    // Close any open tool group to keep UI clean
    closeToolGroup();
    const card = document.createElement('div');
    card.className = 'info-card';
    card.innerHTML =
      '<div class="info-title">&#9888; ' + esc(ev.agent_role || ev.agent_node) + ' skipped</div>' +
      '<p class="info-body">' + esc(ev.rationale || 'Moderator decision') + '</p>';
    push(card);
    return;
  }

  if (ev.type === 'history_compressed') {
    const card = document.createElement('div');
    card.className = 'info-card';
    card.innerHTML =
      '<div class="info-title">&#128209; History compressed (round ' + esc(String(ev.round)) + ')</div>' +
      '<p class="info-body">Earlier rounds are summarized to reduce tokens.</p>';
    push(card);
    return;
  }

  hideTyping();

  if (ev.type === 'run_started') {
    maxRounds = ev.max_rounds;
    curRound  = 1;
    HypoMap.setTopic(ev.topic);
    if (isLive) setStatus('running', 'Round 1 of ' + maxRounds);

    const hdr = document.createElement('div');
    hdr.className = 'run-header';
    hdr.innerHTML =
      '<div class="run-header-label">Diagnosis' + (isLive ? ' in progress' : '') + '</div>' +
      '<div class="run-header-topic">' + esc(ev.topic) + '</div>' +
      '<div class="run-header-meta">Maximum ' + ev.max_rounds + ' rounds &middot; Required confidence: ' +
        Math.round(ev.confidence_threshold * 100) + '%' +
        (ev.template && ev.template !== 'default' ? ' &middot; Template: ' + esc(ev.template) : '') +
      '</div>';
    thread.insertBefore(hdr, typing);

    addRoundSep(1, maxRounds);
    if (isLive) showTyping('Primary Diagnosis');

  } else if (ev.type === 'agent_completed') {
    if (!finalizeLiveAgent(ev.content, 'final', 'Round ' + curRound)) push(buildAgentCard(ev));
    const next = NEXT_LABEL[ev.node];
    if (next && isLive) showTyping(next);

  } else if (ev.type === 'moderator_decision') {
    const d = ev.decision || {};
    HypoMap.setDecision(d);
    push(buildModCard(d, curRound));

    if (d.status === 'continue') {
      curRound = ev.round || (curRound + 1);
      addRoundSep(curRound, maxRounds);
      if (isLive) {
        setStatus('running', 'Round ' + curRound + ' of ' + maxRounds);
        showTyping('Primary Diagnosis');
      }
    }

  } else if (ev.type === 'final_result') {
    if (lastDecision) push(buildFinalCard(lastDecision));

  } else if (ev.type === 'run_finished') {
    const statsCard = buildTokenStatsCard(ev.token_totals, ev.cost_estimate);
    if (statsCard) push(statsCard);
    if (isLive) {
      setStatus('done', 'Finished');
      finishLiveView();
    }
    showPostRunActions();

  } else if (ev.type === 'run_cancelled') {
    push(buildInfoCard('Debate stopped', 'The debate was interrupted manually.'));
    if (isLive) {
      setStatus('idle', 'Stopped');
      finishLiveView();
    }
    showPostRunActions();

  } else if (ev.type === 'error') {
    push(buildErrCard(ev.message));
    if (isLive) {
      setStatus('error', 'Error');
      finishLiveView();
    }
    showPostRunActions();
  }
}

function finishLiveView() {
  finalizeLiveAgent();  // freeze (or drop, if empty) an interrupted streaming card
  btn.disabled = false;
  hideStop();
  stopBtn.disabled = false;
  removeHitlBanner();
  closeSource();
  isLive = false;
}

// ── Watch a run via SSE ────────────────────────────────────────────────
function watchRun(runId) {
  closeSource();
  viewRunId = runId;
  isLive = true;

  source = new EventSource('/api/runs/' + runId + '/events');
  source.onmessage = msg => renderEvent(JSON.parse(msg.data));
  source.onerror = () => {
    if (!isLive) return;
    hideTyping();
    push(buildErrCard('Connection to the server was lost. The debate is still running on the server: open it from the history.'));
    setStatus('error', 'Connection error');
    finishLiveView();
  };
}

// ── Form submit ────────────────────────────────────────────────────────
form.addEventListener('submit', async ev => {
  ev.preventDefault();
  closeSource();

  showDebateTab();
  clearThread();
  emptyState.classList.add('hidden');
  lastDecision = null;
  curRound = 1;
  viewRunId = null;

  setStatus('preparing', 'Preparing...');
  btn.disabled = true;
  showStop();

  try {
    const fd = new FormData(form);
    fd.set('require_approval', document.getElementById('require_approval').checked ? 'true' : 'false');
    fd.set('pause_between_rounds', document.getElementById('pause_rounds').checked ? 'true' : 'false');

    const res     = await fetch('/api/runs', { method: 'POST', body: fd });
    const payload = await res.json();
    if (!res.ok) {
      const msg = payload.detail || payload.message || 'The diagnosis could not be started';
      // Detect auth-related errors and prompt user to renew token
      if (/token.*expired|unauthorized|invalid.*token|401|403/i.test(msg)) {
        throw new Error(
          'GitHub token expired or invalid. ' +
          'Click the 🔑 Auth button in the sidebar to renew it.'
        );
      }
      throw new Error(msg);
    }

    watchRun(payload.run_id);
  } catch (err) {
    push(buildErrCard(err.message));
    setStatus('error', 'Error');
    btn.disabled = false;
    hideStop();
    viewRunId = null;
  }
});

// ── Export & resume ────────────────────────────────────────────────────
btnExport.addEventListener('click', () => {
  if (viewRunId) window.location.href = '/api/runs/' + viewRunId + '/report';
});

btnResume.addEventListener('click', () => {
  resumePanel.classList.toggle('hidden');
});

document.getElementById('resume-cancel').addEventListener('click', hideResumePanel);

function hideResumePanel() {
  resumePanel.classList.add('hidden');
  document.getElementById('resume-evidence').value = '';
  document.getElementById('resume-files').value = '';
}

document.getElementById('resume-submit').addEventListener('click', async () => {
  if (!viewRunId) return;
  const evidence = document.getElementById('resume-evidence').value.trim();
  const files    = document.getElementById('resume-files').files;
  if (!evidence && !files.length) {
    alert('Provide new evidence (text or files) to resume the debate.');
    return;
  }
  const fd = new FormData();
  fd.set('new_evidence', evidence);
  for (const f of files) fd.append('evidence_file', f);
  fd.set('require_approval', document.getElementById('require_approval').checked ? 'true' : 'false');
  fd.set('pause_between_rounds', document.getElementById('pause_rounds').checked ? 'true' : 'false');

  const parentId = viewRunId;
  try {
    const res = await fetch('/api/runs/' + parentId + '/resume', { method: 'POST', body: fd });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || 'The debate could not be resumed');

    hideResumePanel();
    showDebateTab();
    clearThread();
    emptyState.classList.add('hidden');
    lastDecision = null;
    curRound = 1;
    setStatus('preparing', 'Resuming...');
    btn.disabled = true;
    showStop();
    watchRun(payload.run_id);
  } catch (err) {
    alert(err.message);
  }
});

// ── History ────────────────────────────────────────────────────────────
async function loadHistory() {
  histPanel.innerHTML = '<p class="hist-empty">Loading history...</p>';
  try {
    const res  = await fetch('/api/runs');
    const data = await res.json();
    renderHistoryList(data.runs || []);
  } catch {
    histPanel.innerHTML = '<p class="hist-empty">Error loading the history.</p>';
  }
}

const RS_CFG = {
  running:     ['rs-running',     'In progress'],
  completed:   ['rs-completed',   'Completed'],
  cancelled:   ['rs-cancelled',   'Stopped'],
  error:       ['rs-error',       'Error'],
  interrupted: ['rs-interrupted', 'Interrupted'],
};

function statusBadge(status) {
  const [cls, lbl] = RS_CFG[status] || ['rs-cancelled', status || '?'];
  return '<span class="rs ' + cls + '">' + esc(lbl) + '</span>';
}

function fmtRunDuration(secs) {
  if (secs == null || secs < 0) return '—';
  const s = Math.round(secs);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return m + 'm' + (rs > 0 ? ' ' + rs + 's' : '');
  const h = Math.floor(m / 60), rm = m % 60;
  return h + 'h' + (rm > 0 ? ' ' + rm + 'm' : '');
}

function fmtTs(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function renderHistoryList(runs) {
  if (!runs.length) {
    histPanel.innerHTML = '<p class="hist-empty">No saved debates yet. Launch a diagnosis to see it here.</p>';
    return;
  }

  histPanel.innerHTML = '';
  const title = document.createElement('div');
  title.className = 'hist-title';
  title.textContent = 'Debate history';
  histPanel.appendChild(title);

  const table = document.createElement('table');
  table.className = 'hist-table';
  table.innerHTML = '<thead><tr>' +
    '<th>Topic</th><th>Models</th><th>Start date</th><th>Duration</th><th>Status</th><th></th>' +
    '</tr></thead>';
  const tbody = document.createElement('tbody');

  for (const r of runs) {
    const date = fmtTs(r.timestamp);
    const dur  = fmtRunDuration(r.duration_seconds);
    const totalTok = r.token_totals && r.token_totals.total ? r.token_totals.total.total_tokens : null;
    const durHtml = '<span class="hist-duration">' + esc(dur) + '</span>' +
      (totalTok ? '<br><span class="hist-tokens">~' + esc(fmtTokens(totalTok)) + ' tok</span>' : '');
    const mods = r.models
      ? [r.models.diagnostic, r.models.skeptic, r.models.moderator]
          .filter(Boolean)
          .map(m => m.replace('copilot/', '').replace('openai/', ''))
          .join(', ')
      : '—';
    const resumed = r.parent_run_id ? ' &#8635;' : '';

    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><div class="hist-topic" title="' + esc(r.topic) + '">' + esc(r.topic || '—') + resumed + '</div></td>' +
      '<td><div class="hist-models" title="' + esc(mods) + '">' + esc(mods) + '</div></td>' +
      '<td><span class="hist-date">' + esc(date) + '</span></td>' +
      '<td>' + durHtml + '</td>' +
      '<td>' + statusBadge(r.status) + '</td>' +
      '<td><div class="hist-actions">' +
        '<button class="btn-sm btn-open" data-act="open">' + (r.status === 'running' ? 'Watch live' : 'Open') + '</button>' +
        '<button class="btn-sm btn-del" data-act="del"' + (r.status === 'running' ? ' disabled' : '') + '>Delete</button>' +
      '</div></td>';

    tr.querySelector('[data-act="open"]').addEventListener('click', () => openRun(r.run_id));
    const delBtn = tr.querySelector('[data-act="del"]');
    if (!delBtn.disabled) delBtn.addEventListener('click', () => deleteRun(r.run_id, delBtn));
    tbody.appendChild(tr);
  }

  table.appendChild(tbody);
  histPanel.appendChild(table);
}

async function openRun(runId) {
  let data;
  try {
    const res = await fetch('/api/runs/' + runId);
    if (!res.ok) { alert('The debate could not be loaded.'); return; }
    data = await res.json();
  } catch (err) {
    alert('Error loading: ' + err.message);
    return;
  }

  closeSource();
  showDebateTab();
  clearThread();
  emptyState.classList.add('hidden');
  lastDecision = null;
  curRound = 1;
  viewRunId = runId;

  if (data.status === 'running') {
    // Live run: subscribe — the server replays buffered events first.
    setStatus('running', 'Connecting...');
    btn.disabled = true;
    showStop();
    watchRun(runId);
    return;
  }

  // Finished run: replay stored events.
  isLive = false;
  const banner = document.createElement('div');
  banner.className = 'replay-banner';
  banner.innerHTML = '&#9654; Replaying debate &nbsp;&middot;&nbsp; <strong>' + esc(data.topic || '') + '</strong>';
  thread.insertBefore(banner, typing);

  if (data.timestamp || data.finished_at || data.duration_seconds != null) {
    const timingParts = [];
    if (data.timestamp) timingParts.push('Start: <strong>' + esc(fmtTs(data.timestamp)) + '</strong>');
    if (data.finished_at) timingParts.push('End: <strong>' + esc(fmtTs(data.finished_at)) + '</strong>');
    if (data.duration_seconds != null) timingParts.push('Duration: <strong>' + esc(fmtRunDuration(data.duration_seconds)) + '</strong>');
    const timingBar = document.createElement('div');
    timingBar.className = 'run-timing-bar';
    timingBar.innerHTML = timingParts.map(p => '<span>' + p + '</span>').join(' &middot; ');
    thread.insertBefore(timingBar, typing);
  }

  for (const ev of (data.events || [])) {
    renderEvent(ev);
  }

  hideTyping();
  showPostRunActions();
  scrollBottom();
}

async function deleteRun(runId, btnEl) {
  if (!confirm('Delete this debate from the history?')) return;
  try {
    await fetch('/api/runs/' + runId, { method: 'DELETE' });
    const row = btnEl.closest('tr');
    if (row) row.remove();
    if (!histPanel.querySelector('tbody tr')) {
      histPanel.innerHTML = '<p class="hist-empty">No saved debates yet.</p>';
    }
  } catch (err) {
    alert('Error deleting: ' + err.message);
  }
}

// ── Model selector ─────────────────────────────────────────────────────
async function loadModels() {
  try {
    const [mRes, sRes] = await Promise.all([
      fetch('/api/models'),
      fetch('/api/settings'),
    ]);
    const { models = [] } = await mRes.json();
    const settings = sRes.ok ? await sRes.json() : {};

    populateSelect('sel-diag',    models, settings.diagnostic_model || '');
    populateSelect('sel-skeptic', models, settings.skeptic_model    || '');
    populateSelect('sel-mod',     models, settings.moderator_model  || '');

    setEffort('sel-diag-effort',    settings.diagnostic_reasoning_effort);
    setEffort('sel-skeptic-effort', settings.skeptic_reasoning_effort);
    setEffort('sel-mod-effort',     settings.moderator_reasoning_effort);

    defaultApproval = settings.tool_approval_required !== false;
    document.getElementById('require_approval').checked = defaultApproval;
    if (settings.prompt_language) {
      document.getElementById('sel-lang').value = settings.prompt_language;
    }
  } catch (err) {
    console.warn('Model list unavailable:', err.message);
    for (const id of ['sel-diag', 'sel-skeptic', 'sel-mod']) {
      const sel = document.getElementById(id);
      if (sel) {
        sel.innerHTML = '<option value="">— From .env —</option>';
        sel.disabled = false;
      }
    }
  }
}

const PROV_LABELS = {
  copilot:       'GitHub Copilot',
  github_models: 'GitHub Models',
};

function setEffort(id, value) {
  const sel = document.getElementById(id);
  if (!sel) return;
  const valid = ['none', 'low', 'medium', 'high'];
  sel.value = valid.includes(value) ? value : 'none';
}

function populateSelect(id, models, selected) {
  const sel = document.getElementById(id);
  if (!sel) return;
  sel.disabled = false;

  const groups = {};
  for (const m of models) {
    const p = m.provider || 'other';
    (groups[p] = groups[p] || []).push(m);
  }

  sel.innerHTML = '';

  // Default "from .env" option
  const dflt = document.createElement('option');
  dflt.value = '';
  dflt.textContent = '— From .env —';
  if (!selected) dflt.selected = true;
  sel.appendChild(dflt);

  for (const [prov, items] of Object.entries(groups)) {
    const grp = document.createElement('optgroup');
    grp.label = PROV_LABELS[prov] || prov;
    for (const m of items) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name || m.id;
      if (m.id === selected) opt.selected = true;
      grp.appendChild(opt);
    }
    sel.appendChild(grp);
  }
}

// ── Prompt template selector ───────────────────────────────────────────
async function loadTemplates() {
  try {
    const res = await fetch('/api/prompts');
    const { templates = [] } = await res.json();
    const sel = document.getElementById('sel-template');
    const seen = new Set();
    sel.innerHTML = '';
    for (const t of templates) {
      if (seen.has(t.name)) continue;  // one entry per template; language is a separate select
      seen.add(t.name);
      const opt = document.createElement('option');
      opt.value = t.name;
      opt.textContent = t.description || t.name;
      if (t.name === 'default') opt.textContent = 'General';
      sel.appendChild(opt);
    }
  } catch (_) { /* keep the static "General" option */ }
}

// ── Init ───────────────────────────────────────────────────────────────
loadModels();
loadTemplates();
initAuthButton();
initMobileSidebar();

// ── Auth modal & status ────────────────────────────────────────────────
let _authPollInterval = null;

function initAuthButton() {
  const head = document.querySelector('.sidebar-head');
  if (!head || document.getElementById('auth-status-btn')) return;
  const btn = document.createElement('button');
  btn.id = 'auth-status-btn';
  btn.className = 'auth-status-btn';
  btn.innerHTML = '&#128273; Auth';
  btn.addEventListener('click', showAuthModal);
  head.appendChild(btn);
}

function showAuthModal() {
  if (document.getElementById('auth-modal')) return;
  const modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.id = 'auth-modal';
  modal.innerHTML =
    '<div class="modal-card">' +
      '<div class="modal-head">' +
        '<h3>GitHub Copilot authentication</h3>' +
        '<button class="modal-close" id="auth-modal-close">&times;</button>' +
      '</div>' +
      '<div class="modal-body">' +
        '<div id="auth-current-status"></div>' +
        '<div id="auth-flow-ui" style="display:none;margin-top:1rem;">' +
          '<p>1. Open <a id="auth-verification-url" href="#" target="_blank">github.com/login/device</a></p>' +
          '<p>2. Enter the code:<br><strong id="auth-user-code"></strong></p>' +
          '<p id="auth-polling-status" style="color:#888;margin-top:0.5rem;">Waiting for authorization...</p>' +
        '</div>' +
      '</div>' +
      '<div class="modal-foot">' +
        '<button id="auth-start-btn" class="btn btn-primary">Connect Copilot</button>' +
        '<button id="auth-modal-cancel" class="btn btn-secondary">Close</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(modal);

  document.getElementById('auth-modal-close').addEventListener('click', closeAuthModal);
  document.getElementById('auth-modal-cancel').addEventListener('click', closeAuthModal);
  document.getElementById('auth-start-btn').addEventListener('click', startCopilotAuth);

  loadAuthStatus();
}

function closeAuthModal() {
  if (_authPollInterval) { clearInterval(_authPollInterval); _authPollInterval = null; }
  const m = document.getElementById('auth-modal');
  if (m) m.remove();
}

async function loadAuthStatus() {
  const div = document.getElementById('auth-current-status');
  if (!div) return;
  try {
    const resp = await fetch('/api/auth/status');
    const s = await resp.json();
    let html = '';
    if (s.copilot_configured) {
      const mins = s.copilot_session_expires_in_seconds != null
        ? Math.floor(s.copilot_session_expires_in_seconds / 60)
        : '?';
      const cls = s.copilot_session_valid && mins > 5 ? 'auth-ok' : (mins > 0 ? 'auth-warn' : 'auth-err');
      html += '<div class="' + cls + '">&#9679; Copilot: ' + (s.copilot_session_valid ? 'OK' : 'Expired') +
              ' (~' + mins + ' min)</div>';
    } else {
      html += '<div class="auth-err">&#9679; Copilot: Not configured</div>';
    }
    if (s.github_models_configured) {
      html += '<div class="auth-ok">&#9679; GitHub Models: OK</div>';
    }
    if (s.last_error) {
      html += '<div class="auth-err">Error: ' + esc(s.last_error) + '</div>';
    }
    div.innerHTML = html;
  } catch (e) {
    div.innerHTML = '<div class="auth-err">Could not retrieve auth status</div>';
  }
}

async function startCopilotAuth() {
  const btn = document.getElementById('auth-start-btn');
  const flowUI = document.getElementById('auth-flow-ui');
  const status = document.getElementById('auth-polling-status');
  btn.disabled = true;
  btn.textContent = 'Starting...'

  try {
    const resp = await fetch('/api/auth/copilot', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'action=start',
    });
    const data = await resp.json();

    if (data.status !== 'pending') {
      status.textContent = 'Error: ' + (data.error || 'unknown');
      status.className = 'auth-err';
      btn.disabled = false;
      btn.textContent = 'Retry';
      return;
    }

    document.getElementById('auth-verification-url').href = data.verification_uri;
    document.getElementById('auth-user-code').textContent = data.user_code;
    flowUI.style.display = 'block';

    const deviceCode = data.device_code;
    const intervalMs = (data.interval || 5) * 1000;

    _authPollInterval = setInterval(async () => {
      try {
        const check = await fetch('/api/auth/copilot', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: 'action=check&device_code=' + encodeURIComponent(deviceCode),
        });
        const result = await check.json();

        if (result.status === 'authorized') {
          clearInterval(_authPollInterval);
          _authPollInterval = null;
          status.textContent = '\u2713 Authorized. Reloading models...';
          status.className = 'auth-ok';
          await fetch('/api/models/refresh', {method: 'POST'});
          await loadModels();
          await loadAuthStatus();
          setTimeout(closeAuthModal, 1500);
        } else if (result.status === 'denied') {
          clearInterval(_authPollInterval);
          _authPollInterval = null;
          status.textContent = '\u2717 Denied.';
          status.className = 'auth-err';
          btn.disabled = false;
          btn.textContent = 'Retry';
        } else if (result.status === 'expired') {
          clearInterval(_authPollInterval);
          _authPollInterval = null;
          status.textContent = '\u2717 Code expired.';
          status.className = 'auth-err';
          btn.disabled = false;
          btn.textContent = 'Retry';
        } else if (result.status === 'error') {
          clearInterval(_authPollInterval);
          _authPollInterval = null;
          status.textContent = '\u2717 ' + (result.error || 'Error');
          status.className = 'auth-err';
          btn.disabled = false;
          btn.textContent = 'Retry';
        }
      } catch (e) {
        status.textContent = 'Waiting...';
      }
    }, intervalMs);

  } catch (e) {
    status.textContent = 'Network error: ' + e.message;
    status.className = 'auth-err';
    btn.disabled = false;
    btn.textContent = 'Retry';
  }
}
