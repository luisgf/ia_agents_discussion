// Copyright (C) 2025 Luis González Fernández
// SPDX-License-Identifier: GPL-3.0-or-later

// ── Hypothesis map (radial view) ───────────────────────────────────────
// Consumes hypothesis_update SSE events (live or replayed) and renders an
// incremental Cytoscape graph: topic at the center, hypotheses on concentric
// rings by creation round. Exposed as window.HypoMap; wired from app.js.
(function () {
  'use strict';

  const STATE_LABELS = { active: 'Active', confirmed: 'Confirmed', rejected: 'Refuted' };
  const STATE_ICONS  = { active: '●', confirmed: '✓', rejected: '✗' };
  const TOPIC_ID     = '__topic__';
  const PLAY_STEP_MS = 1100;
  const LEADER_MIN_SCORE = 0.3;

  // ── State ────────────────────────────────────────────────────────────
  const hyps     = new Map();   // id → hypothesis (last snapshot wins)
  let topicText  = '';
  let maxRound   = 1;
  let leaderText = '';          // moderator's leading_hypothesis (free text)
  let leaderId   = null;        // matched hypothesis id, if any
  let roundFilter = 'all';
  let cy         = null;
  let dirty      = false;       // updates arrived while the tab was hidden
  let firstLayout = true;
  let isPlaying  = false;
  let playTimer  = null;

  // ── DOM ──────────────────────────────────────────────────────────────
  const panel       = document.getElementById('map-panel');
  const cyBox       = document.getElementById('map-cy');
  const emptyBox    = document.getElementById('map-empty');
  const fitBtn      = document.getElementById('map-fit');
  const tooltip     = document.getElementById('map-tooltip');
  const roundFilterBox = document.getElementById('map-round-filter');
  const playBtn     = document.getElementById('map-play');
  const leaderStrip = document.getElementById('map-leader-strip');
  const detailEmpty = document.getElementById('map-panel-empty');
  const detailBox   = document.getElementById('map-panel-detail');

  const esc = s => String(s || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const truncate = (s, n) => {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
  };

  const nodeLabel = h => h.id + '\n' + truncate(h.text, 42);

  const isVisible = () => panel && panel.classList.contains('active');

  // ── Cytoscape setup ──────────────────────────────────────────────────
  function cyStyle() {
    return [
      {
        selector: 'node[type="hypothesis"]',
        style: {
          'label': 'data(label)',
          'shape': 'round-rectangle',
          'width': 118, 'height': 50,
          'background-color': '#fff',
          'border-width': 2,
          'font-size': 9,
          'text-valign': 'center',
          'text-halign': 'center',
          'text-wrap': 'wrap',
          'text-max-width': 106,
          'overlay-padding': 6,
          'transition-property': 'opacity, border-color, background-color',
          'transition-duration': '0.25s',
        },
      },
      {
        selector: 'node[type="hypothesis"][state="active"]',
        style: { 'border-color': '#2563eb', 'color': '#2563eb', 'background-color': '#eff6ff' },
      },
      {
        selector: 'node[type="hypothesis"][state="confirmed"]',
        style: { 'border-color': '#15803d', 'color': '#15803d', 'background-color': '#f0fdf4' },
      },
      {
        selector: 'node[type="hypothesis"][state="rejected"]',
        style: {
          'border-color': '#dc2626', 'color': '#dc2626', 'background-color': '#fef2f2',
          'border-style': 'dashed',
        },
      },
      {
        selector: 'node[type="topic"]',
        style: {
          'label': 'data(label)',
          'shape': 'round-rectangle',
          'width': 140, 'height': 54,
          'background-color': '#1a1a1a',
          'color': '#fff',
          'border-width': 0,
          'font-size': 10,
          'text-valign': 'center',
          'text-halign': 'center',
          'text-wrap': 'wrap',
          'text-max-width': 126,
        },
      },
      {
        selector: 'edge',
        style: {
          'width': 1,
          'curve-style': 'bezier',
          'line-color': '#d4d4d4',
          'line-style': 'dotted',
          'target-arrow-shape': 'triangle',
          'target-arrow-color': '#d4d4d4',
          'label': 'data(label)',
          'font-size': 9,
          'color': '#999',
          'text-rotation': 'autorotate',
        },
      },
      {
        selector: 'node:selected',
        style: { 'border-width': 3.5, 'overlay-color': '#2563eb', 'overlay-opacity': 0.08 },
      },
      {
        selector: '.leader',
        style: { 'border-width': 4, 'overlay-color': '#7c3aed', 'overlay-opacity': 0.14 },
      },
      {
        selector: '.dimmed',
        style: { 'opacity': 0.15 },
      },
      {
        selector: '.pulsing',
        style: { 'border-width': 5, 'overlay-color': '#2563eb', 'overlay-opacity': 0.18 },
      },
    ];
  }

  function ensureCy() {
    if (cy || !cyBox || typeof cytoscape === 'undefined') return cy;
    cy = cytoscape({
      container: cyBox,
      elements: [],
      style: cyStyle(),
      layout: { name: 'preset' },
      userZoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
      minZoom: 0.25,
      maxZoom: 3,
    });

    cy.on('tap', 'node[type="hypothesis"]', evt => {
      const hyp = hyps.get(evt.target.id());
      if (hyp) showDetail(hyp);
      cy.$('node').deselect();
      evt.target.select();
    });
    cy.on('tap', evt => { if (evt.target === cy) closeDetail(); });

    cy.on('mouseover', 'node[type="hypothesis"]', evt => {
      const hyp = hyps.get(evt.target.id());
      if (!hyp || !tooltip) return;
      tooltip.innerHTML = '<strong>' + esc(hyp.id) + '</strong> · ' + esc(hyp.text);
      tooltip.classList.remove('hidden');
      positionTooltip(evt.target);
    });
    cy.on('mouseout', 'node[type="hypothesis"]', hideTooltip);
    cy.on('pan zoom', hideTooltip);

    if (fitBtn) fitBtn.addEventListener('click', () => cy && cy.fit(undefined, 40));
    return cy;
  }

  function positionTooltip(node) {
    const pos = node.renderedPosition();
    const box = cyBox.getBoundingClientRect();
    tooltip.style.left = Math.min(pos.x + 14, box.width - 290) + 'px';
    tooltip.style.top  = Math.max(pos.y - 10, 8) + 'px';
  }

  function hideTooltip() {
    if (tooltip) tooltip.classList.add('hidden');
  }

  function runLayout(fit) {
    if (!cy || cy.nodes().length === 0) return;
    cy.layout({
      name: 'concentric',
      animate: true,
      animationDuration: 350,
      // Topic at the center; ring = creation round (older rounds closer in).
      concentric: n => n.data('type') === 'topic' ? maxRound + 2 : maxRound - (n.data('round') || 1) + 1,
      levelWidth: () => 1,
      minNodeSpacing: 30,
      avoidOverlap: true,
      fit,
      padding: 40,
    }).run();
  }

  // ── Incremental render ───────────────────────────────────────────────
  function sync() {
    if (!ensureCy()) return;
    dirty = false;

    if (cy.$id(TOPIC_ID).length === 0) {
      cy.add({ data: { id: TOPIC_ID, type: 'topic', round: 0, label: truncate(topicText || 'PROBLEM', 90) } });
    } else {
      cy.$id(TOPIC_ID).data('label', truncate(topicText || 'PROBLEM', 90));
    }

    for (const h of hyps.values()) {
      const node = cy.$id(h.id);
      if (node.length) {
        node.data({ state: h.state, label: nodeLabel(h), round: h.round });
      } else {
        cy.add([
          { data: { id: h.id, type: 'hypothesis', state: h.state, round: h.round, label: nodeLabel(h) } },
          { data: { id: 'e_' + h.id, source: TOPIC_ID, target: h.id, label: 'R' + h.round } },
        ]);
      }
    }

    const hasData = hyps.size > 0;
    if (emptyBox) emptyBox.classList.toggle('hidden', hasData);
    if (fitBtn) fitBtn.classList.toggle('hidden', !hasData);

    rebuildRoundButtons();
    applyFilter();
    applyLeader();
    refreshDetail();
    runLayout(firstLayout);
    if (hasData) firstLayout = false;
  }

  // ── Round filter ─────────────────────────────────────────────────────
  function rebuildRoundButtons() {
    if (!roundFilterBox) return;
    roundFilterBox.querySelectorAll('.map-round-btn').forEach(b => b.remove());
    const values = ['all'];
    for (let r = 1; r <= maxRound; r++) values.push(String(r));
    for (const v of values) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'map-round-btn' + (v === roundFilter ? ' active' : '');
      b.dataset.round = v;
      b.textContent = v === 'all' ? 'All' : v;
      b.addEventListener('click', () => setRoundFilter(v));
      roundFilterBox.appendChild(b);
    }
  }

  function setRoundFilter(v) {
    roundFilter = v;
    stopPlay();
    if (roundFilterBox) {
      roundFilterBox.querySelectorAll('.map-round-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.round === v));
    }
    applyFilter();
  }

  function applyFilter() {
    if (!cy) return;
    const limit = roundFilter === 'all' ? Infinity : Number(roundFilter);
    cy.$('node[type="hypothesis"]').forEach(n => {
      n.toggleClass('dimmed', (n.data('round') || 1) > limit);
    });
    cy.edges().forEach(e => {
      e.toggleClass('dimmed', e.target().hasClass('dimmed'));
    });
  }

  // ── Leading hypothesis (moderator) ──────────────────────────────────
  function tokenize(s) {
    return new Set(String(s || '')
      .toLowerCase()
      .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .replace(/[^a-z0-9\s]/g, ' ')
      .split(/\s+/)
      .filter(w => w.length > 3));
  }

  function jaccard(a, b) {
    if (!a.size || !b.size) return 0;
    let inter = 0;
    for (const w of a) if (b.has(w)) inter++;
    return inter / (a.size + b.size - inter);
  }

  function applyLeader() {
    leaderId = null;
    if (leaderText) {
      const target = tokenize(leaderText);
      let best = null;
      let bestScore = 0;
      for (const h of hyps.values()) {
        const score = jaccard(target, tokenize(h.text));
        if (score > bestScore) { bestScore = score; best = h; }
      }
      if (best && bestScore >= LEADER_MIN_SCORE) leaderId = best.id;
    }
    if (cy) {
      cy.$('node').removeClass('leader');
      if (leaderId) cy.$id(leaderId).addClass('leader');
    }
    if (leaderStrip) {
      if (leaderText) {
        leaderStrip.innerHTML = '<strong>Leader per moderator</strong>' +
          (leaderId ? esc(leaderId) + ' · ' : '') + esc(leaderText);
        leaderStrip.classList.remove('hidden');
      } else {
        leaderStrip.classList.add('hidden');
      }
    }
  }

  // ── Detail panel ─────────────────────────────────────────────────────
  let detailId = null;

  function showDetail(hyp) {
    detailId = hyp.id;
    if (!detailBox || !detailEmpty) return;
    detailEmpty.classList.add('hidden');
    detailBox.classList.remove('hidden');

    document.getElementById('map-panel-id').textContent = hyp.id;
    document.getElementById('map-panel-text').textContent = hyp.text;
    document.getElementById('map-panel-proposer').textContent = String(hyp.proposer || '').replace('_agent', '');
    document.getElementById('map-panel-round').textContent = 'Round ' + hyp.round +
      (hyp.probability != null ? ' · P=' + Number(hyp.probability).toFixed(2) : '');

    const badge = document.getElementById('map-panel-state');
    badge.textContent = (STATE_ICONS[hyp.state] || '') + ' ' + (STATE_LABELS[hyp.state] || hyp.state);
    badge.className = 'map-state-badge ' + hyp.state;

    const evSect = document.getElementById('map-panel-evidence-sect');
    const evList = document.getElementById('map-panel-evidence');
    const evidence = hyp.supporting_evidence || [];
    evSect.classList.toggle('hidden', evidence.length === 0);
    evList.innerHTML = evidence.map(e => '<div class="map-evidence-item">' + esc(e) + '</div>').join('');

    const rejSect = document.getElementById('map-panel-rejection-sect');
    rejSect.classList.toggle('hidden', !hyp.rejected_reason);
    if (hyp.rejected_reason) {
      document.getElementById('map-panel-rejection').textContent = hyp.rejected_reason;
    }

    const trSect = document.getElementById('map-panel-transitions-sect');
    const trList = document.getElementById('map-panel-transitions');
    const transitions = hyp.transitions || [];
    trSect.classList.toggle('hidden', transitions.length === 0);
    trList.innerHTML = transitions.map(t => {
      const arrow = t.from
        ? (STATE_ICONS[t.from] || t.from) + ' → ' + (STATE_ICONS[t.to] || t.to)
        : '→ ' + (STATE_ICONS[t.to] || t.to);
      return '<div class="map-evidence-item"><strong>R' + esc(String(t.round)) + '</strong> ' + arrow +
        (t.note ? ' · ' + esc(t.note) : '') +
        ' <em style="color:var(--muted)">(' + esc(String(t.agent || '').replace('_agent', '')) + ')</em></div>';
    }).join('');
  }

  function refreshDetail() {
    if (detailId && hyps.has(detailId)) showDetail(hyps.get(detailId));
  }

  function closeDetail() {
    detailId = null;
    if (detailEmpty) detailEmpty.classList.remove('hidden');
    if (detailBox) detailBox.classList.add('hidden');
    if (cy) cy.$('node').deselect();
  }

  // ── Playback: replays creations AND state transitions ────────────────
  function buildTimeline() {
    const steps = [];
    for (const h of hyps.values()) {
      for (const t of h.transitions || []) {
        steps.push({ hypId: h.id, round: t.round, from: t.from || null, to: t.to });
      }
      // Hypothesis persisted without transitions (defensive): treat as creation.
      if (!(h.transitions || []).length) steps.push({ hypId: h.id, round: h.round, from: null, to: h.state });
    }
    steps.sort((a, b) => a.round - b.round || (a.from === null ? 0 : 1) - (b.from === null ? 0 : 1));
    return steps;
  }

  function togglePlay() {
    if (isPlaying) { stopPlay(); return; }
    startPlay();
  }

  function startPlay() {
    if (!cy || hyps.size === 0) return;
    const steps = buildTimeline();
    if (!steps.length) return;
    isPlaying = true;
    playBtn.innerHTML = '&#9632; Stop';
    playBtn.classList.add('playing');
    setRoundFilterVisual('all');
    closeDetail();
    hideTooltip();

    // Hide everything except the topic, then replay step by step.
    cy.$('node[type="hypothesis"], edge').addClass('dimmed');
    let i = 0;
    const tick = () => {
      if (!isPlaying) return;
      if (i >= steps.length) { stopPlay(); return; }
      const step = steps[i++];
      const node = cy.$id(step.hypId);
      if (node.length) {
        if (step.from === null) {
          node.removeClass('dimmed');
          cy.$id('e_' + step.hypId).removeClass('dimmed');
        }
        node.data('state', step.to);
        node.flashClass('pulsing', 600);
      }
      playTimer = setTimeout(tick, PLAY_STEP_MS);
    };
    tick();
  }

  function stopPlay() {
    if (!isPlaying) return;
    isPlaying = false;
    if (playTimer) { clearTimeout(playTimer); playTimer = null; }
    playBtn.innerHTML = '&#9654; Replay';
    playBtn.classList.remove('playing');
    if (cy) {
      cy.$('.dimmed').removeClass('dimmed');
      // Restore final states overwritten during playback.
      for (const h of hyps.values()) {
        const node = cy.$id(h.id);
        if (node.length) node.data('state', h.state);
      }
      applyFilter();
    }
  }

  function setRoundFilterVisual(v) {
    roundFilter = v;
    if (roundFilterBox) {
      roundFilterBox.querySelectorAll('.map-round-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.round === v));
    }
  }

  if (playBtn) playBtn.addEventListener('click', togglePlay);

  // ── Public API ───────────────────────────────────────────────────────
  window.HypoMap = {
    setTopic(topic) {
      topicText = String(topic || '');
      if (cy && cy.$id(TOPIC_ID).length) cy.$id(TOPIC_ID).data('label', truncate(topicText || 'PROBLEM', 90));
    },

    update(ev) {
      stopPlay();
      for (const h of (ev && ev.hypotheses) || []) {
        if (!h || !h.id) continue;
        hyps.set(h.id, h);
        maxRound = Math.max(maxRound, h.round || 1);
        for (const t of h.transitions || []) maxRound = Math.max(maxRound, t.round || 1);
      }
      if (ev && ev.round) maxRound = Math.max(maxRound, ev.round);
      if (isVisible()) sync(); else dirty = true;
    },

    setDecision(d) {
      leaderText = String((d && d.leading_hypothesis) || '');
      if (isVisible()) applyLeader(); else dirty = true;
    },

    reset() {
      stopPlay();
      hyps.clear();
      topicText = '';
      maxRound = 1;
      leaderText = '';
      leaderId = null;
      roundFilter = 'all';
      firstLayout = true;
      dirty = false;
      closeDetail();
      hideTooltip();
      if (cy) cy.elements().remove();
      if (leaderStrip) leaderStrip.classList.add('hidden');
      if (emptyBox) emptyBox.classList.remove('hidden');
      if (fitBtn) fitBtn.classList.add('hidden');
      rebuildRoundButtons();
    },

    show() {
      if (!ensureCy()) return;
      cy.resize();
      if (dirty || cy.nodes().length === 0) sync();
      else if (firstLayout) runLayout(true);
    },
  };
})();
