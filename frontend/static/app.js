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

  renderListingData(data.listing_data);
  renderDecision(data.recommendation);
  renderRiskMap(data.lat, data.lon, data.address, data.nearby_risks || []);
  renderRadar(data.scores);
  renderScores(data.scores, data.score_evidence);
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

// ── Property listing card ─────────────────────────────────

function renderListingData(listing) {
  const card = document.getElementById('listingCard');
  if (!listing || listing.error || (!listing.price && !listing.beds && !listing.sqft)) {
    card.classList.add('hidden');
    return;
  }

  const photoHtml = (listing.photos && listing.photos.length)
    ? `<img src="${listing.photos[0]}" alt="Property photo" loading="lazy">`
    : `<div class="listing-photo-placeholder">🏠</div>`;

  const statusBadge = listing.status
    ? `<span class="listing-status-badge listing-status-${listing.status.toLowerCase().replace(/\s+/g,'-')}">${listing.status}</span>`
    : '';

  const priceHtml = listing.price
    ? `<div class="listing-price">$${listing.price.toLocaleString()} ${statusBadge}</div>`
    : statusBadge ? `<div class="listing-price">${statusBadge}</div>` : '';

  // Primary specs row
  const specs = [];
  if (listing.beds)          specs.push(`<span class="listing-spec"><span class="listing-spec-icon">🛏</span>${listing.beds} bed${listing.beds !== 1 ? 's' : ''}</span>`);
  if (listing.baths)         specs.push(`<span class="listing-spec"><span class="listing-spec-icon">🚿</span>${listing.baths} bath${listing.baths !== 1 ? 's' : ''}</span>`);
  if (listing.sqft)          specs.push(`<span class="listing-spec"><span class="listing-spec-icon">📐</span>${listing.sqft.toLocaleString()} sqft</span>`);
  if (listing.year_built)    specs.push(`<span class="listing-spec"><span class="listing-spec-icon">🏗</span>Built ${listing.year_built}</span>`);
  if (listing.lot_size_sqft) specs.push(`<span class="listing-spec"><span class="listing-spec-icon">🌳</span>${listing.lot_size_sqft.toLocaleString()} sqft lot</span>`);
  if (listing.garage_spaces) specs.push(`<span class="listing-spec"><span class="listing-spec-icon">🚗</span>${listing.garage_spaces}-car garage</span>`);

  // Financial details row (HOA, taxes, DOM)
  const fin = [];
  if (listing.hoa_fee_monthly != null) fin.push(`<span class="listing-fin-item"><strong>HOA</strong> $${listing.hoa_fee_monthly.toLocaleString()}/mo</span>`);
  if (listing.tax_annual != null)      fin.push(`<span class="listing-fin-item"><strong>Tax</strong> $${Math.round(listing.tax_annual).toLocaleString()}/yr</span>`);
  if (listing.days_on_market != null)  fin.push(`<span class="listing-fin-item"><strong>DOM</strong> ${listing.days_on_market} days</span>`);
  if (listing.property_type)           fin.push(`<span class="listing-fin-item"><strong>Type</strong> ${listing.property_type}</span>`);
  if (listing.heating_cooling)         fin.push(`<span class="listing-fin-item"><strong>HVAC</strong> ${listing.heating_cooling}</span>`);

  const finRow = fin.length ? `<div class="listing-fin-row">${fin.join('')}</div>` : '';

  // Description (truncate at 200 chars)
  const descHtml = listing.description
    ? `<div class="listing-desc">${listing.description.substring(0, 220)}${listing.description.length > 220 ? '…' : ''}</div>`
    : '';

  const linkHtml = listing.listing_url
    ? `<a class="listing-link" href="${listing.listing_url}" target="_blank" rel="noopener">View on ${listing.source || 'listing site'} →</a>`
    : '';

  card.innerHTML = `
    <div class="listing-inner">
      <div class="listing-photo-strip">${photoHtml}</div>
      <div class="listing-details">
        <div class="listing-source-badge">🏠 Property Listing · ${listing.source || 'Unknown source'}</div>
        ${priceHtml}
        <div class="listing-specs">${specs.join('')}</div>
        ${finRow}
        ${descHtml}
        ${linkHtml}
      </div>
    </div>`;
  card.classList.remove('hidden');
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

// ── Property Risk Map (street map + risk markers) ─────────

const _SEV_COLOR = {
  critical: '#7c3aed',
  high:     '#dc2626',
  medium:   '#ea580c',
  low:      '#16a34a',
};
const _CAT_ICON = {
  traffic:       '🚗',
  infrastructure:'🏭',
  flood:         '💧',
  environmental: '☣️',
  epa:           '☣️',
  noise:         '🔊',
};

function _offsetLatLon(lat, lon, distM, bearingDeg) {
  const R = 6371000;
  const b = bearingDeg * Math.PI / 180;
  const dLat = (distM * Math.cos(b)) / R * (180 / Math.PI);
  const dLon = (distM * Math.sin(b)) / (R * Math.cos(lat * Math.PI / 180)) * (180 / Math.PI);
  return [lat + dLat, lon + dLon];
}

function renderRiskMap(lat, lon, address, risks) {
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }

  leafletMap = L.map('riskMap', { zoomControl: true, scrollWheelZoom: false })
    .setView([lat, lon], 14);

  // OpenStreetMap street tiles
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }).addTo(leafletMap);

  // Distance rings
  L.circle([lat, lon], {
    radius: 300, color: '#ea580c', weight: 1.5, opacity: 0.8,
    fillColor: '#ea580c', fillOpacity: 0.04, dashArray: '5 5',
  }).addTo(leafletMap).bindPopup('<b>300 m radius</b>');

  L.circle([lat, lon], {
    radius: 1000, color: '#f59e0b', weight: 1.5, opacity: 0.6,
    fillColor: '#f59e0b', fillOpacity: 0.02, dashArray: '5 5',
  }).addTo(leafletMap).bindPopup('<b>1 km radius</b>');

  // Property marker
  const homeIcon = L.divIcon({
    html: '<div style="background:#1e3a5f;border:2px solid #fff;border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-size:16px;box-shadow:0 2px 8px rgba(0,0,0,.45)">🏠</div>',
    iconSize: [34, 34], iconAnchor: [17, 17], className: '',
  });
  L.marker([lat, lon], { icon: homeIcon, zIndexOffset: 1000 })
    .addTo(leafletMap)
    .bindPopup(`<strong>${address}</strong><br><span style="font-size:0.8em;color:#6b7280">${lat.toFixed(5)}, ${lon.toFixed(5)}</span>`);

  // Risk markers — golden-angle distribution for risks without a known bearing
  const GOLDEN = 137.508;
  let bIdx = 0;
  risks.forEach(risk => {
    const bearing = (risk.bearing_deg != null) ? risk.bearing_deg : (bIdx++ * GOLDEN) % 360;
    const dist    = risk.distance_m || 600;
    const [rLat, rLon] = _offsetLatLon(lat, lon, dist, bearing);
    const color   = _SEV_COLOR[risk.severity] || '#6b7280';
    const emoji   = _CAT_ICON[(risk.category || '').toLowerCase()] || '⚠️';

    const rIcon = L.divIcon({
      html: `<div style="background:${color};border:2px solid #fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:13px;box-shadow:0 1px 6px rgba(0,0,0,.4)">${emoji}</div>`,
      iconSize: [28, 28], iconAnchor: [14, 14], className: '',
    });

    L.marker([rLat, rLon], { icon: rIcon })
      .addTo(leafletMap)
      .bindPopup(`
        <strong>${risk.name}</strong><br>
        <span style="font-size:0.82em;color:#6b7280">Distance: ${risk.distance_label}</span><br>
        <span style="font-size:0.82em;font-weight:700;color:${color}">${(risk.severity || '').toUpperCase()}</span>
      `);
  });

  // ── Right panel: risk list ───────────────────────────────
  const panel = document.getElementById('riskPanelList');
  if (!risks || risks.length === 0) {
    panel.innerHTML = '<div class="risk-panel-empty">No significant risks detected nearby.</div>';
  } else {
    panel.innerHTML = risks.map(r => {
      const color = _SEV_COLOR[r.severity] || '#6b7280';
      const emoji = _CAT_ICON[(r.category || '').toLowerCase()] || '⚠️';
      return `<div class="risk-panel-item">
        <span class="risk-panel-icon">${emoji}</span>
        <div class="risk-panel-info">
          <div class="risk-panel-name" title="${r.name}">${r.name}</div>
          <div class="risk-panel-dist">${r.distance_label}</div>
        </div>
        <span class="risk-sev-badge" style="background:${color}20;color:${color};border-color:${color}55">${r.severity}</span>
      </div>`;
    }).join('');
  }

  // External map links
  const enc = encodeURIComponent(address);
  document.getElementById('riskMapLinks').innerHTML = [
    { label: '🗺 Google Maps', url: `https://www.google.com/maps/search/${enc}` },
    { label: '🏠 Zillow',      url: `https://www.zillow.com/homes/${enc}_rb/` },
    { label: '🔵 Redfin',      url: `https://www.redfin.com/stingray/do/location-autocomplete?location=${enc}` },
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
function showResults() {
  document.getElementById('resultsPanel').classList.remove('hidden');
  // Leaflet can't measure its container while the panel is hidden.
  // invalidateSize() forces a full tile reload once it becomes visible.
  setTimeout(() => { if (leafletMap) leafletMap.invalidateSize(); }, 60);
}
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
