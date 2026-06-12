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
  moderator_agent:            { cls: 'a-mod',       ico: ICO.mod },
};

const NEXT_LABEL = {
  diagnostic_agent:          'Revisor Escéptico',
  skeptic_agent:             'Contrarréplica',
  diagnostic_rebuttal_agent: 'Moderador',
};

const STATUS_CFG = {
  continue:               { lbl: 'Continuar',     cls: 'db-continue' },
  final_diagnosis:        { lbl: 'Diagnóstico',   cls: 'db-final_diagnosis' },
  needs_more_data:        { lbl: 'Faltan datos',  cls: 'db-needs_more_data' },
  propose_fix:            { lbl: 'Fix listo',     cls: 'db-propose_fix' },
  structured_uncertainty: { lbl: 'Incertidumbre', cls: 'db-structured_uncertainty' },
};

const RISK_CLS = { critical:'rc', high:'rh', medium:'rm', low:'rl' };

const APPROVAL_LBL = {
  auto:     ['ap-auto',     'auto'],
  approved: ['ap-approved', 'aprobada'],
  rejected: ['ap-rejected', 'rechazada'],
  timeout:  ['ap-timeout',  'timeout'],
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
const tabHist     = document.getElementById('tab-hist');
const btnExport   = document.getElementById('btn-export');
const btnResume   = document.getElementById('btn-resume');
const resumePanel = document.getElementById('resume-panel');

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

function showTyping(label, suffix = ' analizando...') {
  typingLbl.textContent = label + suffix;
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
}

function addRoundSep(round, total) {
  const d = document.createElement('div');
  d.className = 'round-sep';
  d.innerHTML = '<span>Ronda ' + round + ' <em>de ' + total + '</em></span>';
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
tabHist.addEventListener('click', showHistoryTab);

function showDebateTab() {
  activeTab = 'debate';
  tabDebate.classList.add('active');
  tabHist.classList.remove('active');
  thread.style.display = 'flex';
  histPanel.classList.remove('active');
}

function showHistoryTab() {
  activeTab = 'history';
  tabHist.classList.add('active');
  tabDebate.classList.remove('active');
  thread.style.display = 'none';
  histPanel.classList.add('active');
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
             '<span class="agent-sub">Ronda ' + curRound + '</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body">' + md(event.content) + '</div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

// Static card with the model's chain of thought, used when replaying a
// stored run (live runs render it inside the streaming card instead).
function buildThinkingCard(event) {
  const cfg = AGENT_CFG[event.agent_node] || { cls: 'a-diag', ico: ICO.diag };
  const el = document.createElement('article');
  el.className = 'acard acard--reasoning ' + cfg.cls;
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + cfg.ico + '</div>' +
        '<div><span class="agent-nm">' + esc(event.agent_role) + '</span>' +
             '<span class="agent-sub agent-sub--reasoning">Razonamiento del modelo</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body">' +
      '<details class="think-box"><summary>Razonamiento del modelo</summary>' +
      '<div class="think-body">' + md(event.content) + '</div></details>' +
    '</div>';
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
             '<span class="agent-sub agent-sub--reasoning">Razonamiento</span></div>' +
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
// Collapsible block holding the model's chain of thought (thinking tokens).
// `open` while streaming so the operator sees it live; collapsed once frozen.
function thinkHTML(live, open) {
  if (!live.thinkBuffer.trim()) return '';
  return '<details class="think-box"' + (open ? ' open' : '') + '>' +
    '<summary>Razonamiento del modelo</summary>' +
    '<div class="think-body">' + md(live.thinkBuffer) + '</div></details>';
}

function ensureLiveAgent(node, role) {
  // Reutilizar tarjeta del mismo nodo (reactivar si está congelada por reasoning)
  if (liveAgent && liveAgent.node === node) {
    if (!liveAgent.el.classList.contains('acard--streaming')) {
      liveAgent.el.classList.add('acard--streaming');
      liveAgent.el.classList.remove('acard--reasoning');
      const sub = liveAgent.el.querySelector('.agent-sub');
      sub.classList.remove('agent-sub--reasoning');
      sub.classList.add('agent-sub--live');
      sub.textContent = 'Razonando en tiempo real…';
    } else if (liveAgent.thinkBuffer.trim()) {
      // Mid-stream restart (tool call without visible text): keep the
      // thinking already shown by folding it into the frozen HTML.
      liveAgent.frozenHTML += thinkHTML(liveAgent, false);
      liveAgent.body.innerHTML = liveAgent.frozenHTML;
    }
    // Reset buffers for a new LLM streaming iteration (same agent, new round of ReAct)
    liveAgent.buffer = '';
    liveAgent.thinkBuffer = '';
    if (liveAgent.timer) { clearTimeout(liveAgent.timer); liveAgent.timer = null; }
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
             '<span class="agent-sub agent-sub--live">Razonando en tiempo real…</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body"></div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  liveAgent = { el, body: el.querySelector('.card-body'), buffer: '', thinkBuffer: '', node, role, timer: null, frozenHTML: '' };
  hideTyping();
  push(el);
  return liveAgent;
}

// Throttle markdown re-rendering: deltas arrive far faster than 8 fps.
function renderLive(live) {
  if (live.timer) return;
  live.timer = setTimeout(() => {
    live.timer = null;
    // Preserve frozen content (e.g., prior reasoning) while appending fresh deltas
    live.body.innerHTML = live.frozenHTML + thinkHTML(live, true) + md(live.buffer);
    scrollBottom();
  }, 120);
}

function appendLiveDelta(ev) {
  const live = ensureLiveAgent(ev.agent_node, ev.agent_role);
  live.buffer += ev.delta || '';
  renderLive(live);
}

function appendLiveThinking(ev) {
  const live = ensureLiveAgent(ev.agent_node, ev.agent_role);
  live.thinkBuffer += ev.delta || '';
  renderLive(live);
}

// Freeze the streaming card with its definitive content.
// kind: 'reasoning' | 'final'. Returns false when there is no live card
// (e.g. replaying a stored run), so the caller builds a static card instead.
function finalizeLiveAgent(content, kind, subtitle) {
  if (!liveAgent) return false;
  const live = liveAgent;
  // Liberar referencia SOLO si el freeze es definitivo ('final')
  // o si es llamado sin kind (cambio de agente en ensureLiveAgent).
  // Cuando kind === 'reasoning', dejamos liveAgent activo para que
  // ensureLiveAgent pueda reutilizar la tarjeta en la siguiente
  // iteración LLM dentro del mismo ciclo ReAct.
  if (!kind || kind === 'final') {
    liveAgent = null;
  }
  if (live.timer) clearTimeout(live.timer);
  const text = content != null ? content : live.buffer;
  if (!String(text).trim() && !live.thinkBuffer.trim() && !live.frozenHTML) {
    live.el.remove();
    return true;
  }
  live.el.classList.remove('acard--streaming');
  if (kind === 'reasoning') {
    live.el.classList.add('acard--reasoning');
    // Accumulate so subsequent ReAct iterations append instead of overwriting
    live.body.innerHTML = live.frozenHTML + thinkHTML(live, false) + md(text);
    live.frozenHTML = live.body.innerHTML;
    live.buffer = '';
    live.thinkBuffer = '';
  } else {
    // Definitive freeze: show the final answer (plus its thinking, collapsed)
    live.body.innerHTML = thinkHTML(live, false) + md(text);
  }
  const sub = live.el.querySelector('.agent-sub');
  sub.classList.remove('agent-sub--live');
  if (kind === 'reasoning') {
    sub.classList.add('agent-sub--reasoning');
    sub.textContent = 'Razonamiento';
  } else {
    sub.textContent = subtitle || ('Ronda ' + curRound);
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
        '<div><span class="agent-nm">Operador</span>' +
             '<span class="agent-sub">Comentario entre rondas</span></div>' +
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
    body += '<div><div class="msect-title">Hipótesis principal</div>' +
            '<div class="msect-body">' + esc(decision.leading_hypothesis) + '</div></div>';
  }

  if (decision.next_step) {
    body += '<div><div class="msect-title">Siguiente paso</div>' +
            '<div class="msect-body">' + esc(decision.next_step) + '</div></div>';
  }

  const evRow = (decision.evidence && decision.evidence.length) || (decision.missing_evidence && decision.missing_evidence.length);
  if (evRow) {
    body += '<div class="mod-row2">';
    if (decision.evidence && decision.evidence.length) {
      body += '<div><div class="msect-title">Evidencias (' + decision.evidence.length + ')</div>' +
              rlist(decision.evidence) + '</div>';
    }
    if (decision.missing_evidence && decision.missing_evidence.length) {
      body += '<div><div class="msect-title">Evidencia faltante</div>' +
              rlist(decision.missing_evidence) + '</div>';
    }
    body += '</div>';
  }

  if (decision.recommended_fix) {
    body += '<div class="fix-box"><div class="msect-title">Fix recomendado</div>' +
            '<div class="msect-body">' + esc(decision.recommended_fix) + '</div></div>';
  }

  const rejRow = (decision.rejected_hypotheses && decision.rejected_hypotheses.length) || (decision.validation && decision.validation.length);
  if (rejRow) {
    body += '<div class="mod-row2">';
    if (decision.rejected_hypotheses && decision.rejected_hypotheses.length) {
      body += '<div><div class="msect-title">Hipótesis rechazadas</div>' +
              rlist(decision.rejected_hypotheses) + '</div>';
    }
    if (decision.validation && decision.validation.length) {
      body += '<div><div class="msect-title">Pasos de validación</div>' +
              rlist(decision.validation) + '</div>';
    }
    body += '</div>';
  }

  if (decision.stop_reason) {
    body += '<div><div class="msect-title">Motivo de cierre</div>' +
            '<div class="msect-body it">' + esc(decision.stop_reason) + '</div></div>';
  }

  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.mod + '</div>' +
        '<div><span class="agent-nm">Moderador</span>' +
             '<span class="agent-sub">Ronda ' + round + ' · Decisión</span></div>' +
      '</div>' +
      '<div style="display:flex;align-items:center;gap:7px">' +
        '<span class="rbadge ' + rk + '">' + esc(decision.risk_level || '?') + '</span>' +
        '<span class="dbadge ' + st.cls + '">' + st.lbl + '</span>' +
        CHEVRON +
      '</div>' +
    '</div>' +
    '<div class="conf-row">' +
      '<span class="conf-lbl">Confianza</span>' +
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
            '<div><div class="msect-title">Fix recomendado</div>' +
            '<div class="msect-body">' + esc(decision.recommended_fix) + '</div></div>' +
            '</div>';
  }

  const el = document.createElement('article');
  el.className = 'acard a-final';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.final + '</div>' +
        '<div><span class="agent-nm">Diagnóstico completo</span>' +
             '<span class="agent-sub">Debate cerrado · ' + rnds + ' ronda' + (rnds > 1 ? 's' : '') + '</span></div>' +
      '</div>' +
      '<span class="dbadge ' + st.cls + '">' + st.lbl + '</span>' +
    '</div>' +
    '<div class="final-stats">' +
      '<div class="fstat"><span class="fstat-lbl">Confianza</span><span class="fstat-val">' + pct + '%</span></div>' +
      '<div class="fstat"><span class="fstat-lbl">Rondas</span><span class="fstat-val">' + rnds + '</span></div>' +
      '<div class="fstat"><span class="fstat-lbl">Riesgo</span><span class="fstat-val"><span class="rbadge ' + rk + '">' + esc(decision.risk_level || '?') + '</span></span></div>' +
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
        '<div><span class="agent-nm">Error</span><span class="agent-sub">Fallo durante el debate</span></div>' +
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
      '<div class="tc-section-lbl">Argumentos</div>' +
      '<pre class="tc-pre">' + esc(argsStr) + '</pre>' +
      '<div class="tc-section-lbl" style="margin-top:8px">Resultado' + (isErr ? ' (error)' : '') + '</div>' +
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
      '<span class="tc-status">ejecutando&hellip;</span>' +
      '<span class="tc-agent">' + esc(ev.agent_role || ev.agent_node) + '</span>' +
      '<span class="tc-arrow">&#9654;</span>' +
    '</div>' +
    '<div class="tc-body">' +
      '<div class="tc-section-lbl">Argumentos</div>' +
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
      '<span class="tc-group-title">Herramientas</span>' +
      '<span class="tc-group-count">0 llamadas</span>' +
      '<span class="tc-group-errors hidden">0 errores</span>' +
      '<span class="tc-group-running hidden">ejecutando&hellip;</span>' +
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

  const label = group.count === 1 ? '1 llamada' : group.count + ' llamadas';
  group.countEl.textContent = label;

  if (group.errors > 0) {
    group.el.classList.add('has-errors');
    group.errEl.classList.remove('hidden');
    group.errEl.textContent = group.errors === 1 ? '1 error' : group.errors + ' errores';
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
    group.agentEl.textContent = 'Varios agentes';
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
      '<span class="appr-name">&#9888; Aprobación requerida: ' + esc(ev.tool_name) + '</span>' +
      '<span class="appr-agent">' + esc(ev.agent_role || '') + '</span>' +
    '</div>' +
    '<div class="appr-body"><pre class="tc-pre">' +
      esc(JSON.stringify(ev.args || {}, null, 2)) +
    '</pre></div>' +
    '<div class="appr-actions">' +
      '<button class="btn-sm btn-approve" type="button">Aprobar</button>' +
      '<button class="btn-sm btn-reject" type="button">Rechazar</button>' +
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
    st.textContent = '✓ Aprobada por el operador — ejecutando...';
  } else {
    st.classList.add('no');
    st.textContent = ev.resolution === 'timeout'
      ? '✕ Sin respuesta (timeout) — no ejecutada'
      : '✕ Rechazada por el operador — no ejecutada';
  }
}

// ── HITL banner ────────────────────────────────────────────────────────
function showHitlBanner(round) {
  removeHitlBanner();
  const div = document.createElement('div');
  div.className = 'hitl-banner';
  div.innerHTML =
    '<div class="hitl-title">El debate está en pausa: añade contexto antes de la ronda ' + esc(String(round)) + '</div>' +
    '<textarea placeholder="Comentario, dato nuevo o instrucción para los agentes (opcional)..."></textarea>' +
    '<div class="hitl-actions">' +
      '<button class="btn-sm btn-open" type="button" data-act="send">Enviar comentario</button>' +
      '<button class="btn-sm btn-del" type="button" data-act="skip">Continuar sin comentario</button>' +
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

  if (ev.type === 'agent_reasoning_delta') {
    appendLiveThinking(ev);
    return;
  }

  if (ev.type === 'agent_thinking') {
    // Live run: the streaming card already shows it via deltas.
    // Stored replay (no live card): build a static collapsed card.
    if (!(liveAgent && liveAgent.node === ev.agent_node)) push(buildThinkingCard(ev));
    return;
  }

  if (ev.type === 'summary_started') {
    if (isLive) showTyping('Comprimiendo historial', '...');
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
      // Update the "ejecutando…" card in place, keeping its expanded state.
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

  closeToolGroup();

  if (ev.type === 'tool_approval_request') {
    push(buildApprovalCard(ev));
    if (isLive) setStatus('waiting', 'Esperando aprobación');
    return;
  }

  if (ev.type === 'tool_approval_resolved') {
    markApprovalResolved(ev);
    if (isLive) setStatus('running', 'Ronda ' + curRound + ' de ' + maxRounds);
    return;
  }

  if (ev.type === 'awaiting_user_input') {
    if (isLive) {
      hideTyping();
      showHitlBanner(ev.round);
      setStatus('waiting', 'Esperando comentario');
    }
    return;
  }

  if (ev.type === 'user_comment') {
    removeHitlBanner();
    if (ev.content) push(buildUserCard(ev.content));
    if (isLive) {
      setStatus('running', 'Ronda ' + curRound + ' de ' + maxRounds);
      showTyping('Diagnóstico Principal');
    }
    return;
  }

  if (ev.type === 'run_resumed') {
    const banner = document.createElement('div');
    banner.className = 'resumed-banner';
    banner.innerHTML = '&#8635; Debate reanudado con nueva evidencia' +
      (ev.parent_topic ? ' &nbsp;&middot;&nbsp; <strong>' + esc(ev.parent_topic) + '</strong>' : '');
    thread.insertBefore(banner, typing);
    return;
  }

  if (ev.type === 'reasoning_effort_ignored') {
    push(buildInfoCard(
      'Nivel de thinking ignorado',
      'El modelo «' + (ev.model || '?') + '» del agente ' + (ev.agent_role || '?') +
      ' no admite nivel de thinking; se ignoró el valor «' + (ev.requested_effort || '?') + '».'
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
      '<div class="info-title">&#9888; ' + esc(ev.agent_role || ev.agent_node) + ' omitido</div>' +
      '<p class="info-body">' + esc(ev.rationale || 'Decisión del moderador') + '</p>';
    push(card);
    return;
  }

  if (ev.type === 'history_compressed') {
    const card = document.createElement('div');
    card.className = 'info-card';
    card.innerHTML =
      '<div class="info-title">&#128209; Historial comprimido (ronda ' + esc(String(ev.round)) + ')</div>' +
      '<p class="info-body">Las rondas anteriores se resumen para reducir tokens.</p>';
    push(card);
    return;
  }

  hideTyping();

  if (ev.type === 'run_started') {
    maxRounds = ev.max_rounds;
    curRound  = 1;
    if (isLive) setStatus('running', 'Ronda 1 de ' + maxRounds);

    const hdr = document.createElement('div');
    hdr.className = 'run-header';
    hdr.innerHTML =
      '<div class="run-header-label">Diagnóstico' + (isLive ? ' en curso' : '') + '</div>' +
      '<div class="run-header-topic">' + esc(ev.topic) + '</div>' +
      '<div class="run-header-meta">Máximo ' + ev.max_rounds + ' rondas &middot; Confianza requerida: ' +
        Math.round(ev.confidence_threshold * 100) + '%' +
        (ev.template && ev.template !== 'default' ? ' &middot; Plantilla: ' + esc(ev.template) : '') +
      '</div>';
    thread.insertBefore(hdr, typing);

    addRoundSep(1, maxRounds);
    if (isLive) showTyping('Diagnóstico Principal');

  } else if (ev.type === 'agent_reasoning') {
    // Live run: the streaming card already holds this text — freeze it.
    // Stored replay (no live card): build the static reasoning card.
    if (!finalizeLiveAgent(ev.content, 'reasoning')) push(buildReasoningCard(ev));

  } else if (ev.type === 'agent_completed') {
    if (!finalizeLiveAgent(ev.content, 'final', 'Ronda ' + curRound)) push(buildAgentCard(ev));
    const next = NEXT_LABEL[ev.node];
    if (next && isLive) showTyping(next);

  } else if (ev.type === 'moderator_decision') {
    // Discard the moderator's live card (raw JSON from the fallback path is
    // not useful); if it streamed thinking, the collapsed block survives.
    if (liveAgent && liveAgent.node === 'moderator_agent') {
      finalizeLiveAgent('', 'final', 'Ronda ' + curRound + ' · Razonamiento');
    }
    const d = ev.decision || {};
    push(buildModCard(d, curRound));

    if (d.status === 'continue') {
      curRound = ev.round || (curRound + 1);
      addRoundSep(curRound, maxRounds);
      if (isLive) {
        setStatus('running', 'Ronda ' + curRound + ' de ' + maxRounds);
        showTyping('Diagnóstico Principal');
      }
    }

  } else if (ev.type === 'final_result') {
    if (lastDecision) push(buildFinalCard(lastDecision));

  } else if (ev.type === 'run_finished') {
    if (isLive) {
      setStatus('done', 'Finalizado');
      finishLiveView();
    }
    showPostRunActions();

  } else if (ev.type === 'run_cancelled') {
    push(buildInfoCard('Debate detenido', 'El debate fue interrumpido manualmente.'));
    if (isLive) {
      setStatus('idle', 'Detenido');
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
    push(buildErrCard('Se perdió la conexión con el servidor. El debate sigue en el servidor: ábrelo desde el historial.'));
    setStatus('error', 'Error de conexión');
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

  setStatus('preparing', 'Preparando...');
  btn.disabled = true;
  showStop();

  try {
    const fd = new FormData(form);
    fd.set('require_approval', document.getElementById('require_approval').checked ? 'true' : 'false');
    fd.set('pause_between_rounds', document.getElementById('pause_rounds').checked ? 'true' : 'false');

    const res     = await fetch('/api/runs', { method: 'POST', body: fd });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || 'No se pudo iniciar el diagnóstico');

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
    alert('Aporta nueva evidencia (texto o archivos) para reanudar el debate.');
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
    if (!res.ok) throw new Error(payload.detail || 'No se pudo reanudar el debate');

    hideResumePanel();
    showDebateTab();
    clearThread();
    emptyState.classList.add('hidden');
    lastDecision = null;
    curRound = 1;
    setStatus('preparing', 'Reanudando...');
    btn.disabled = true;
    showStop();
    watchRun(payload.run_id);
  } catch (err) {
    alert(err.message);
  }
});

// ── History ────────────────────────────────────────────────────────────
async function loadHistory() {
  histPanel.innerHTML = '<p class="hist-empty">Cargando historial...</p>';
  try {
    const res  = await fetch('/api/runs');
    const data = await res.json();
    renderHistoryList(data.runs || []);
  } catch {
    histPanel.innerHTML = '<p class="hist-empty">Error al cargar el historial.</p>';
  }
}

const RS_CFG = {
  running:     ['rs-running',     'En curso'],
  completed:   ['rs-completed',   'Completado'],
  cancelled:   ['rs-cancelled',   'Detenido'],
  error:       ['rs-error',       'Error'],
  interrupted: ['rs-interrupted', 'Interrumpido'],
};

function statusBadge(status) {
  const [cls, lbl] = RS_CFG[status] || ['rs-cancelled', status || '?'];
  return '<span class="rs ' + cls + '">' + esc(lbl) + '</span>';
}

function renderHistoryList(runs) {
  if (!runs.length) {
    histPanel.innerHTML = '<p class="hist-empty">No hay debates guardados todavía. Lanza un diagnóstico para verlo aquí.</p>';
    return;
  }

  histPanel.innerHTML = '';
  const title = document.createElement('div');
  title.className = 'hist-title';
  title.textContent = 'Historial de debates';
  histPanel.appendChild(title);

  const table = document.createElement('table');
  table.className = 'hist-table';
  table.innerHTML = '<thead><tr>' +
    '<th>Tema</th><th>Modelos</th><th>Fecha</th><th>Estado</th><th></th>' +
    '</tr></thead>';
  const tbody = document.createElement('tbody');

  for (const r of runs) {
    const date = r.timestamp
      ? new Date(r.timestamp).toLocaleString('es', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
      : '—';
    const mods = r.models
      ? [r.models.diagnostic, r.models.skeptic]
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
      '<td>' + statusBadge(r.status) + '</td>' +
      '<td><div class="hist-actions">' +
        '<button class="btn-sm btn-open" data-act="open">' + (r.status === 'running' ? 'Ver en vivo' : 'Abrir') + '</button>' +
        '<button class="btn-sm btn-del" data-act="del"' + (r.status === 'running' ? ' disabled' : '') + '>Eliminar</button>' +
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
    if (!res.ok) { alert('No se pudo cargar el debate.'); return; }
    data = await res.json();
  } catch (err) {
    alert('Error al cargar: ' + err.message);
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
    setStatus('running', 'Conectando...');
    btn.disabled = true;
    showStop();
    watchRun(runId);
    return;
  }

  // Finished run: replay stored events.
  isLive = false;
  const banner = document.createElement('div');
  banner.className = 'replay-banner';
  banner.innerHTML = '&#9654; Reproduciendo debate &nbsp;&middot;&nbsp; <strong>' + esc(data.topic || '') + '</strong>';
  thread.insertBefore(banner, typing);

  for (const ev of (data.events || [])) {
    renderEvent(ev);
  }

  hideTyping();
  showPostRunActions();
  scrollBottom();
}

async function deleteRun(runId, btnEl) {
  if (!confirm('¿Eliminar este debate del historial?')) return;
  try {
    await fetch('/api/runs/' + runId, { method: 'DELETE' });
    const row = btnEl.closest('tr');
    if (row) row.remove();
    if (!histPanel.querySelector('tbody tr')) {
      histPanel.innerHTML = '<p class="hist-empty">No hay debates guardados todavía.</p>';
    }
  } catch (err) {
    alert('Error al eliminar: ' + err.message);
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
        sel.innerHTML = '<option value="">— Desde .env —</option>';
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
  dflt.textContent = '— Desde .env —';
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
