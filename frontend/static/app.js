let currentMode = 'buyer';

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.mode-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.mode-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      currentMode = pill.dataset.mode;
    });
  });

  document.getElementById('analyzeBtn').addEventListener('click', runAnalysis);
  document.getElementById('addressInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') runAnalysis();
  });
});

async function runAnalysis() {
  const address = document.getElementById('addressInput').value.trim();
  if (!address) {
    showError('Please enter a property address.');
    return;
  }

  hideError();
  hideResults();
  showLoading();
  disableBtn(true);

  let stepIdx = 0;
  const stepInterval = setInterval(() => {
    if (stepIdx > 0) {
      const prev = document.getElementById(`step${stepIdx}`);
      if (prev) { prev.classList.remove('active'); prev.classList.add('done'); }
    }
    stepIdx++;
    if (stepIdx <= 4) {
      const cur = document.getElementById(`step${stepIdx}`);
      if (cur) cur.classList.add('active');
    } else {
      clearInterval(stepInterval);
    }
  }, 1800);

  try {
    const resp = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ address, mode: currentMode, risk_tolerance: 'medium' }),
    });

    clearInterval(stepInterval);
    [1, 2, 3, 4].forEach(i => {
      const el = document.getElementById(`step${i}`);
      if (el) { el.classList.remove('active'); el.classList.add('done'); }
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: 'Unknown error' }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }

    const data = await resp.json();
    hideLoading();
    renderResults(data);
  } catch (err) {
    clearInterval(stepInterval);
    hideLoading();
    showError(err.message || 'Analysis failed. Please try again.');
  } finally {
    disableBtn(false);
    resetSteps();
  }
}

function renderResults(data) {
  document.getElementById('resultAddress').textContent = data.address;
  document.getElementById('resultCoords').textContent =
    `${data.lat.toFixed(5)}, ${data.lon.toFixed(5)}`;
  document.getElementById('resultMode').textContent = data.mode.toUpperCase();
  document.getElementById('confidenceValue').textContent = `${data.overall_confidence}%`;

  renderScores(data.scores, data.mode);

  const bsec = document.getElementById('breakdownSection');
  if (data.score_breakdown) {
    bsec.style.display = '';
    renderBreakdown(data.score_breakdown);
  } else {
    bsec.style.display = 'none';
  }

  document.getElementById('narrativeText').textContent = data.narrative;

  const adviceCard = document.getElementById('adviceCard');
  adviceCard.className = `advice-card${data.mode === 'investor' ? ' investor-mode' : ''}`;
  document.getElementById('adviceTitle').textContent =
    data.mode === 'investor' ? 'Investor Advice' : 'Buyer Advice';
  document.getElementById('adviceIcon').textContent =
    data.mode === 'investor' ? '📈' : '💡';
  document.getElementById('adviceText').textContent = data.mode_advice;

  renderRisks(data.risks);
  document.getElementById('sourcesList').textContent =
    (data.data_sources || []).join(', ') || 'FEMA · EPA · OSM · Census · USGS';
  showResults();
}

function renderScores(scores, mode) {
  const grid = document.getElementById('scoresGrid');
  grid.innerHTML = '';

  const defs = [
    { key: 'livability',              label: 'Livability',       invert: false },
    { key: 'environmental_exposure',  label: 'Env. Exposure',    invert: true  },
    { key: 'infrastructure_risk',     label: 'Infra. Risk',      invert: true  },
    { key: 'neighborhood_stability',  label: 'Nbhd. Stability',  invert: false },
    { key: 'hidden_risk',             label: 'Hidden Risk',      invert: true  },
  ];

  defs.forEach(def => {
    const val = scores[def.key] || 0;
    const colorClass = getScoreColor(val, def.invert);
    const card = document.createElement('div');
    card.className = 'score-card';
    card.innerHTML = `
      <div class="score-label">${def.label}</div>
      <div class="score-value score-${colorClass}" id="sv-${def.key}">0</div>
      <div class="score-bar-bg">
        <div class="score-bar bar-${colorClass}" id="sb-${def.key}"></div>
      </div>`;
    grid.appendChild(card);
    animateScore(def.key, val, colorClass);
  });
}

function getScoreColor(val, invert) {
  const effective = invert ? (100 - val) : val;
  if (effective >= 70) return 'green';
  if (effective >= 40) return 'amber';
  return 'red';
}

function animateScore(key, target, colorClass) {
  const el  = document.getElementById(`sv-${key}`);
  const bar = document.getElementById(`sb-${key}`);
  if (!el || !bar) return;
  const start = performance.now();
  const duration = 800;
  function frame(now) {
    const elapsed = now - start;
    const progress = Math.min(elapsed / duration, 1);
    const ease = 1 - Math.pow(1 - progress, 3);
    const current = Math.round(target * ease);
    el.textContent = current;
    bar.style.width = `${current}%`;
    if (progress < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// ── Breakdown ─────────────────────────────────────────────

function toggleBreakdown() {
  const body    = document.getElementById('breakdownBody');
  const chevron = document.getElementById('breakdownChevron');
  const open    = !body.classList.contains('hidden');
  body.classList.toggle('hidden', open);
  chevron.classList.toggle('open', !open);
}

function renderBreakdown(bd) {
  const body = document.getElementById('breakdownBody');
  const items = [
    { label: 'Env Raw',      sublabel: 'fetcher',  key: 'env_raw'        },
    { label: 'Env Agent',    sublabel: 'LLM',      key: 'env_agent'      },
    { label: 'Infra Raw',    sublabel: 'fetcher',  key: 'infra_raw'      },
    { label: 'Infra Agent',  sublabel: 'LLM',      key: 'infra_agent'    },
    { label: 'Nbhd Raw',     sublabel: 'fetcher',  key: 'nbhd_raw'       },
    { label: 'Nbhd Agent',   sublabel: 'LLM',      key: 'nbhd_agent'     },
    { label: 'Elevation',    sublabel: 'USGS',     key: 'elevation_score'},
  ];
  body.innerHTML = items.map(it => `
    <div class="bd-item">
      <div class="bd-label">${it.label}</div>
      <div class="bd-value">${bd[it.key] ?? '—'}</div>
      <div class="bd-sublabel">${it.sublabel}</div>
    </div>`).join('');
}

// ── Risks ─────────────────────────────────────────────────

function renderRisks(risks) {
  const list = document.getElementById('risksList');
  const countEl = document.getElementById('riskCount');
  list.innerHTML = '';

  if (!risks || risks.length === 0) {
    countEl.textContent = '0';
    list.innerHTML = '<p style="font-size:.875rem;color:var(--muted);padding:.5rem 0">No significant risks identified.</p>';
    return;
  }

  countEl.textContent = risks.length;

  const order = { critical: 0, high: 1, medium: 2, low: 3 };
  const sorted = [...risks].sort((a, b) => (order[a.severity] ?? 99) - (order[b.severity] ?? 99));

  sorted.forEach(risk => {
    const card = document.createElement('div');
    card.className = `risk-card severity-${risk.severity}`;
    const evidence = (risk.evidence || []).map(e => `<li>${e}</li>`).join('');
    card.innerHTML = `
      <div class="risk-header" onclick="toggleRisk(this)">
        <span class="risk-title">${formatCategory(risk.category)}</span>
        <div class="risk-right">
          <span class="severity-badge badge-${risk.severity}">${risk.severity}</span>
          <span class="expand-icon">▼</span>
        </div>
      </div>
      <div class="risk-body">
        <p class="risk-description">${risk.description}</p>
        ${evidence ? `<ul class="risk-evidence">${evidence}</ul>` : ''}
        <div class="risk-meta">
          Confidence: ${risk.confidence}%${risk.timeline ? ` · Timeline: ${risk.timeline}` : ''}${risk.confidence < 40 ? ' · ⚠ low confidence' : ''}
        </div>
      </div>`;
    list.appendChild(card);
  });
}

function toggleRisk(header) {
  header.closest('.risk-card').classList.toggle('expanded');
}

function formatCategory(cat) {
  return cat.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

// ── Helpers ───────────────────────────────────────────────

function showLoading()  { document.getElementById('loadingPanel').classList.remove('hidden'); }
function hideLoading()  { document.getElementById('loadingPanel').classList.add('hidden'); }
function showResults()  { document.getElementById('resultsPanel').classList.remove('hidden'); }
function hideResults()  { document.getElementById('resultsPanel').classList.add('hidden'); }
function showError(msg) {
  document.getElementById('errorMsg').textContent = msg;
  document.getElementById('errorBanner').classList.remove('hidden');
}
function hideError()    { document.getElementById('errorBanner').classList.add('hidden'); }
function disableBtn(v)  { document.getElementById('analyzeBtn').disabled = v; }
function resetSteps() {
  [1, 2, 3, 4].forEach(i => {
    const el = document.getElementById(`step${i}`);
    if (el) el.classList.remove('active', 'done');
  });
}
