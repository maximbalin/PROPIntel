let currentMode = 'buyer';
let radarChartInstance = null;
let leafletMap = null;

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
  if (!address) { showError('Please enter a property address.'); return; }

  hideError(); hideResults(); showLoading(); disableBtn(true);

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
    } else { clearInterval(stepInterval); }
  }, 1800);

  try {
    const resp = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ address, mode: currentMode, risk_tolerance: 'medium' }),
    });
    clearInterval(stepInterval);
    [1,2,3,4].forEach(i => {
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

  renderDecision(data.recommendation);
  renderSatelliteMap(data.lat, data.lon, data.address);
  renderRadar(data.scores);
  renderScores(data.scores, data.score_evidence);
  renderNearbyRisks(data.nearby_risks || []);
  renderHiddenCosts(data.hidden_costs || []);
  renderPriceContext(data.price_context);

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
  document.getElementById('adviceIcon').textContent = data.mode === 'investor' ? '📈' : '💡';
  document.getElementById('adviceText').textContent = data.mode_advice;

  renderRisks(data.risks);
  document.getElementById('sourcesList').textContent =
    (data.data_sources || []).join(', ') || 'FEMA · EPA · OSM · Census · USGS';
  showResults();
}

// ── Decision banner ───────────────────────────────────────

function renderDecision(rec) {
  const banner = document.getElementById('decisionBanner');
  if (!rec) { banner.classList.add('hidden'); return; }

  banner.className = `decision-banner verdict-${rec.verdict}`;
  document.getElementById('decisionVerdict').textContent = rec.verdict;
  document.getElementById('decisionScore').textContent = rec.score;
  document.getElementById('decisionSummary').textContent = rec.summary;

  const factors = document.getElementById('decisionFactors');
  factors.innerHTML = (rec.key_factors || [])
    .map(f => `<li>${f}</li>`).join('');
}

// ── Satellite map ─────────────────────────────────────────

function renderSatelliteMap(lat, lon, address) {
  // Destroy previous instance
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }

  leafletMap = L.map('satelliteMap', { zoomControl: true, scrollWheelZoom: false })
    .setView([lat, lon], 15);

  // Esri World Imagery — free satellite tiles, no API key required
  L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { attribution: 'Tiles © Esri — Source: Esri, Maxar, GeoEye, Earthstar Geographics, CNES/Airbus DS, USDA, USGS, AeroGRID, IGN', maxZoom: 19 }
  ).addTo(leafletMap);

  // Property marker
  const icon = L.divIcon({
    html: '<div style="background:#1e3a5f;color:#fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.4)">📍</div>',
    iconSize: [28, 28], iconAnchor: [14, 14], className: '',
  });
  L.marker([lat, lon], { icon })
    .addTo(leafletMap)
    .bindPopup(`<strong>${address}</strong><br>${lat.toFixed(5)}, ${lon.toFixed(5)}`);

  // 0.5 mile radius circle (804.67m)
  L.circle([lat, lon], {
    radius: 804.67,
    color: '#f59e0b', weight: 2, opacity: 0.8,
    fillColor: '#f59e0b', fillOpacity: 0.06,
  }).addTo(leafletMap);

  // External listing links
  const enc = encodeURIComponent(address);
  const linksEl = document.getElementById('mapLinks');
  linksEl.innerHTML = [
    { label: '🏠 Realtor.com', url: `https://www.realtor.com/realestateandhomes-search/${enc}` },
    { label: '🟢 Zillow',      url: `https://www.zillow.com/homes/${enc}_rb/` },
    { label: '🔵 Redfin',      url: `https://www.redfin.com/stingray/do/location-autocomplete?location=${enc}` },
    { label: '🗺 Google Maps', url: `https://www.google.com/maps/search/${enc}` },
  ].map(l => `<a class="map-link-btn" href="${l.url}" target="_blank" rel="noopener">${l.label}</a>`).join('');
}

// ── Radar chart ───────────────────────────────────────────

function renderRadar(scores) {
  if (radarChartInstance) { radarChartInstance.destroy(); radarChartInstance = null; }
  const ctx = document.getElementById('radarChart').getContext('2d');

  // All axes: higher = better for display (invert risk scores)
  const data = [
    scores.livability,
    100 - scores.environmental_exposure,
    100 - scores.infrastructure_risk,
    scores.neighborhood_stability,
    100 - scores.hidden_risk,
  ];

  radarChartInstance = new Chart(ctx, {
    type: 'radar',
    data: {
      labels: ['Livability', 'Env. Safety', 'Infra. Safety', 'Nbhd. Stability', 'Low Hidden Risk'],
      datasets: [{
        data,
        backgroundColor: 'rgba(245,158,11,0.12)',
        borderColor: 'rgba(217,119,6,0.8)',
        borderWidth: 2,
        pointBackgroundColor: 'rgba(217,119,6,0.9)',
        pointRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      scales: {
        r: {
          min: 0, max: 100, ticks: { display: false, stepSize: 25 },
          grid: { color: 'rgba(0,0,0,0.06)' },
          pointLabels: { font: { size: 10, family: 'Inter' }, color: '#6b7280' },
          angleLines: { color: 'rgba(0,0,0,0.06)' },
        },
      },
      plugins: { legend: { display: false } },
    },
  });
}

// ── Score cards with evidence ─────────────────────────────

function renderScores(scores, evidence) {
  const grid = document.getElementById('scoresGrid');
  grid.innerHTML = '';

  const defs = [
    { key: 'livability',             label: 'Livability',       invert: false },
    { key: 'environmental_exposure', label: 'Env. Exposure',    invert: true  },
    { key: 'infrastructure_risk',    label: 'Infra. Risk',      invert: true  },
    { key: 'neighborhood_stability', label: 'Nbhd. Stability',  invert: false },
    { key: 'hidden_risk',            label: 'Hidden Risk',      invert: true  },
  ];

  defs.forEach(def => {
    const val = scores[def.key] || 0;
    const colorClass = getScoreColor(val, def.invert);
    const bullets = (evidence && evidence[def.key]) || [];
    const bulletHtml = bullets.length
      ? `<ul class="score-evidence-list">${bullets.map(b => `<li>${b}</li>`).join('')}</ul>`
      : '';

    const card = document.createElement('div');
    card.className = 'score-card';
    card.innerHTML = `
      <div class="score-left">
        <div class="score-label">${def.label}</div>
        <div class="score-value score-${colorClass}" id="sv-${def.key}">0</div>
        <div class="score-bar-bg"><div class="score-bar bar-${colorClass}" id="sb-${def.key}"></div></div>
      </div>
      <div class="score-right">${bulletHtml}</div>`;
    grid.appendChild(card);
    animateScore(def.key, val);
  });
}

function getScoreColor(val, invert) {
  const eff = invert ? (100 - val) : val;
  if (eff >= 70) return 'green';
  if (eff >= 40) return 'amber';
  return 'red';
}

function animateScore(key, target) {
  const el  = document.getElementById(`sv-${key}`);
  const bar = document.getElementById(`sb-${key}`);
  if (!el || !bar) return;
  const start = performance.now();
  function frame(now) {
    const progress = Math.min((now - start) / 800, 1);
    const ease = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(target * ease);
    bar.style.width = `${target * ease}%`;
    if (progress < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// ── Nearby risks ring ─────────────────────────────────────

const SEV_COLOR = { critical: '#7c3aed', high: '#dc2626', medium: '#ea580c', low: '#f59e0b' };

function renderNearbyRisks(risks) {
  const card = document.getElementById('nearbyCard');
  const legend = document.getElementById('ringLegend');
  if (!risks || risks.length === 0) { card.style.display = 'none'; return; }
  card.style.display = '';

  drawRingCanvas(risks);

  legend.innerHTML = risks.map(r => `
    <div class="ring-legend-item">
      <span class="ring-dot ring-dot-${r.severity}"></span>
      <span>
        <span class="ring-item-name">${r.name}</span>
        <span class="ring-item-dist"> — ${r.distance_label}</span>
      </span>
    </div>`).join('');
}

function drawRingCanvas(risks) {
  const canvas = document.getElementById('ringCanvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2;
  ctx.clearRect(0, 0, W, H);

  // 3 rings: 300m, 1km, 3mi radius in canvas px
  const rings = [
    { label: '300m',  r: 55,  color: 'rgba(220,38,38,.08)' },
    { label: '1km',   r: 100, color: 'rgba(234,88,12,.06)' },
    { label: '3 mi',  r: 140, color: 'rgba(245,158,11,.05)' },
  ];

  rings.forEach(ring => {
    ctx.beginPath();
    ctx.arc(cx, cy, ring.r, 0, Math.PI * 2);
    ctx.fillStyle = ring.color;
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.08)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = '#9ca3af';
    ctx.font = '10px Inter';
    ctx.textAlign = 'left';
    ctx.fillText(ring.label, cx + ring.r + 4, cy + 4);
  });

  // Property dot at center
  ctx.beginPath();
  ctx.arc(cx, cy, 7, 0, Math.PI * 2);
  ctx.fillStyle = '#1e3a5f';
  ctx.fill();
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 9px Inter';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('P', cx, cy);

  // Place risk dots around rings
  const maxDistM = 5000;
  const maxR = 140;

  // Group risks without a bearing — spread them evenly by angle
  const noBearing = risks.filter(r => r.bearing_deg == null);
  const hasBearing = risks.filter(r => r.bearing_deg != null);
  const angleStep = noBearing.length > 0 ? (360 / noBearing.length) : 0;

  [...hasBearing, ...noBearing].forEach((risk, i) => {
    const distM = risk.distance_m ?? 800;
    const ratio = Math.min(distM / maxDistM, 1);
    const rPx = 18 + ratio * (maxR - 18);

    let angleDeg;
    if (risk.bearing_deg != null) {
      angleDeg = risk.bearing_deg - 90; // chart.js convention: 0=right
    } else {
      angleDeg = i * angleStep - 90;
    }
    const rad = angleDeg * Math.PI / 180;
    const x = cx + rPx * Math.cos(rad);
    const y = cy + rPx * Math.sin(rad);

    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fillStyle = SEV_COLOR[risk.severity] || '#9ca3af';
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });

  ctx.textBaseline = 'alphabetic';
}

// ── Hidden costs ──────────────────────────────────────────

function renderHiddenCosts(costs) {
  const card = document.getElementById('hiddenCostsCard');
  if (!costs || costs.length === 0) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const body = document.getElementById('hiddenCostsBody');
  body.innerHTML = costs.map(c => {
    const amt = (c.annual_low && c.annual_high)
      ? `$${c.annual_low.toLocaleString()}–$${c.annual_high.toLocaleString()}`
      : (c.annual_low ? `~$${c.annual_low.toLocaleString()}` : '—');
    return `<tr>
      <td class="hc-name">${c.name}</td>
      <td><span class="hc-cat">${c.category}</span></td>
      <td class="hc-amount">${amt}</td>
      <td><span class="hc-likelihood lik-${c.likelihood}">${c.likelihood}</span></td>
      <td class="hc-basis">${c.basis}</td>
    </tr>`;
  }).join('');

  // Total confirmed + likely range
  const confirmed = costs.filter(c => c.likelihood === 'confirmed');
  const likely    = costs.filter(c => c.likelihood === 'likely');
  const toRange   = arr => arr.reduce((s, c) => [s[0] + (c.annual_low || 0), s[1] + (c.annual_high || c.annual_low || 0)], [0, 0]);
  const [cLow, cHigh] = toRange(confirmed);
  const [lLow, lHigh] = toRange(likely);
  const totalLow  = cLow + lLow;
  const totalHigh = cHigh + lHigh;

  const totalEl = document.getElementById('hiddenCostsTotal');
  if (totalLow > 0) {
    totalEl.innerHTML = `Estimated annual hidden cost burden: <strong>$${totalLow.toLocaleString()}–$${totalHigh.toLocaleString()}/yr</strong>
      <span style="color:var(--muted);font-size:.75rem"> (confirmed + likely items only)</span>`;
  } else {
    totalEl.textContent = '';
  }
}

// ── Price context ─────────────────────────────────────────

function renderPriceContext(pc) {
  const card = document.getElementById('priceCard');
  if (!pc) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  const medEl = document.getElementById('priceMedian');
  if (pc.area_median_value && pc.area_median_value > 0) {
    medEl.innerHTML = `
      <div class="price-stat-label">Area Median Home Value</div>
      <div class="price-stat-value">$${pc.area_median_value.toLocaleString()}</div>
      <div class="price-stat-sub">US Census ACS 2022 — census tract</div>`;
  } else {
    medEl.innerHTML = `
      <div class="price-stat-label">Area Median Home Value</div>
      <div class="price-stat-value" style="font-size:1rem;color:var(--muted)">Not available</div>`;
  }

  const pct = pc.estimated_impact_pct || 0;
  const impEl = document.getElementById('priceImpact');
  let cls, sign, label;
  if (pct > 0)      { cls = 'impact-positive'; sign = '+'; label = 'Est. premium vs area'; }
  else if (pct < 0) { cls = 'impact-negative'; sign = '';  label = 'Est. discount vs area'; }
  else              { cls = 'impact-neutral';  sign = '';  label = 'Neutral vs area median'; }
  impEl.className = `price-impact-badge ${cls}`;
  impEl.textContent = pct !== 0 ? `${sign}${pct}%  ${label}` : label;

  const driversEl = document.getElementById('priceDrivers');
  driversEl.innerHTML = (pc.impact_drivers || [])
    .map(d => `<li>${d}</li>`).join('');
}

// ── Score breakdown ───────────────────────────────────────

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
    { label: 'Env Raw',     sublabel: 'fetcher',  key: 'env_raw'         },
    { label: 'Env Agent',   sublabel: 'LLM',      key: 'env_agent'       },
    { label: 'Infra Raw',   sublabel: 'fetcher',  key: 'infra_raw'       },
    { label: 'Infra Agent', sublabel: 'LLM',      key: 'infra_agent'     },
    { label: 'Nbhd Raw',    sublabel: 'fetcher',  key: 'nbhd_raw'        },
    { label: 'Nbhd Agent',  sublabel: 'LLM',      key: 'nbhd_agent'      },
    { label: 'Elevation',   sublabel: 'USGS',     key: 'elevation_score' },
  ];
  body.innerHTML = items.map(it => `
    <div class="bd-item">
      <div class="bd-label">${it.label}</div>
      <div class="bd-value">${bd[it.key] ?? '—'}</div>
      <div class="bd-sublabel">${it.sublabel}</div>
    </div>`).join('');
}

// ── Risk factors ──────────────────────────────────────────

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
  [...risks].sort((a, b) => (order[a.severity] ?? 99) - (order[b.severity] ?? 99))
    .forEach(risk => {
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

function toggleRisk(header) { header.closest('.risk-card').classList.toggle('expanded'); }
function formatCategory(cat) { return cat.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }

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
  [1,2,3,4].forEach(i => {
    const el = document.getElementById(`step${i}`);
    if (el) el.classList.remove('active', 'done');
  });
}
