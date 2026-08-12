(function installVitalityCore() {
  'use strict';

  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const rows = () => Array.isArray(DATA?.days) ? DATA.days : [];
  const latest = () => [...rows()].reverse().find(row => finite(row?.recovery)) || [...rows()].reverse().find(Boolean) || null;
  const mean = values => values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const fmt = (value, decimals = 0) => finite(value)
    ? value.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals})
    : '—';
  const escapeHTML = value => String(value ?? '').replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
  const clamp = (value, min = 0, max = 100) => Math.max(min, Math.min(max, value));
  const metricRows = (key, through) => rows().filter(row => (!through || row.date <= through) && finite(row?.[key]));
  const series = (key, count, through) => metricRows(key, through).slice(-count).map(row => row[key]);
  const observations = (key, count, through) => metricRows(key, through).slice(-count);
  const baseline = (key, count, through) => mean(metricRows(key, through).slice(-(count + 1), -1).map(row => row[key]));

  const dateLabel = value => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || '');
    if (!match) return 'Latest data';
    const date = new Date(Date.UTC(+match[1], +match[2] - 1, +match[3]));
    return new Intl.DateTimeFormat('en-US', {weekday:'long', day:'numeric', month:'long'}).format(date);
  };

  const recoveryState = value => !finite(value)
    ? {accent:'#a8b0aa', rgb:'168 176 170', label:'Awaiting data', title:'Daily state', tone:'neutral'}
    : value >= 67
      ? {accent:'#c6ff00', rgb:'198 255 0', label:'High readiness', title:'Daily state', tone:'good'}
      : value >= 34
        ? {accent:'#ffc24b', rgb:'255 194 75', label:'Moderate readiness', title:'Daily state', tone:'warn'}
        : {accent:'#ff5c5c', rgb:'255 92 92', label:'Recovery priority', title:'Daily state', tone:'critical'};

  function previousDay(reference) {
    if (!reference?.date) return null;
    const date = new Date(`${reference.date}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() - 1);
    return rows().find(row => row.date === date.toISOString().slice(0, 10)) || null;
  }

  function signed(value, suffix = '', decimals = 0, invert = false) {
    if (!finite(value)) return {text:'Building baseline', tone:'neutral'};
    const rounded = Number(value.toFixed(decimals));
    if (Math.abs(rounded) < (decimals ? .05 : .5)) return {text:'At baseline', tone:'neutral'};
    const positive = rounded > 0;
    return {
      text:`${positive ? '+' : '−'}${fmt(Math.abs(rounded), decimals)}${suffix} vs baseline`,
      tone:(positive !== invert) ? 'good' : 'warn'
    };
  }

  function microLine(values, label) {
    const valid = values.filter(finite);
    if (!valid.length) return `<span class="v6-microline is-empty" role="img" aria-label="${escapeHTML(label)}: insufficient data"></span>`;
    const W = 240, H = 52;
    const min = Math.min(...valid), max = Math.max(...valid), spread = max - min || 1;
    const points = valid.map((value, index) => ({
      x: valid.length === 1 ? W / 2 : index * (W / (valid.length - 1)),
      y: 42 - ((value - min) / spread) * 31
    }));
    const line = points.length > 1
      ? `<polyline points="${points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')}"/>`
      : '';
    const dots = points.map((point, index) => `<circle class="${index === points.length - 1 ? 'is-current' : ''}" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="${index === points.length - 1 ? 3.8 : 2.2}"/>`).join('');
    return `<span class="v6-microline" role="img" aria-label="${escapeHTML(label)}: ${valid.length} observations"><svg viewBox="0 0 ${W} ${H}" aria-hidden="true"><line x1="0" y1="35" x2="${W}" y2="35" class="v6-microline-base"/>${line}${dots}</svg></span>`;
  }

  function signalRow({metric, label, value, unit, detail, tone, values}) {
    const disabled = !finite(value);
    return `<button type="button" class="v6-signal-row" data-tone="${tone}" ${disabled ? 'disabled' : `data-metric="${metric}" aria-haspopup="dialog"`} aria-label="${escapeHTML(`${label}: ${fmt(value, metric === 'hrv' ? 1 : 0)} ${unit}. ${detail}`)}">
      <span class="v6-signal-copy"><span>${escapeHTML(label)}</span><small>${escapeHTML(detail)}</small></span>
      <span class="v6-signal-plot">${microLine(values, label)}</span>
      <strong>${fmt(value, metric === 'hrv' ? 1 : 0)}<small>${escapeHTML(unit)}</small></strong>
      <svg class="v6-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
    </button>`;
  }

  function coreObject(recovery, state, delta) {
    const pct = finite(recovery) ? clamp(recovery) : 0;
    const deltaText = finite(delta) ? (delta === 0 ? '0' : `${delta > 0 ? '+' : '−'}${fmt(Math.abs(delta))}`) : '—';
    const deltaAria = !finite(delta)
      ? 'Previous-day comparison unavailable.'
      : delta === 0
        ? 'No change from the previous day.'
        : `${fmt(Math.abs(delta))} points ${delta > 0 ? 'above' : 'below'} the previous day.`;
    const ringOffset = 100 - pct;
    const ringAngle = pct * 3.6;
    return `<button type="button" class="v6-core-button v6-ring-button" data-metric="recovery" aria-haspopup="dialog" aria-label="Recovery ${fmt(recovery)} percent. ${escapeHTML(state.label)}. ${escapeHTML(deltaAria)} Open details">
      <span class="v6-recovery-ring" style="--ring-offset:${ringOffset.toFixed(2)};--ring-angle:${ringAngle.toFixed(2)}deg" aria-hidden="true">
        <svg class="v6-ring-svg" viewBox="0 0 300 300">
          <defs><linearGradient id="v6-ring-gradient" x1="45" y1="36" x2="256" y2="268" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="var(--vital-accent)" stop-opacity="1"/><stop offset=".55" stop-color="var(--vital-accent)" stop-opacity=".94"/><stop offset="1" stop-color="var(--vital-accent)" stop-opacity=".82"/></linearGradient></defs>
          <circle class="v6-ring-bed" cx="150" cy="150" r="118" pathLength="100"/>
          <circle class="v6-ring-aura" cx="150" cy="150" r="118" pathLength="100"/>
          <circle class="v6-ring-progress" cx="150" cy="150" r="118" pathLength="100"/>
        </svg>
        <span class="v6-ring-marker"></span>
      </span>
      <span class="v6-core-reading"><small>Recovery index</small><span class="v6-core-value"><strong>${fmt(recovery)}</strong><i>%</i></span><span class="v6-core-delta">${escapeHTML(deltaText)}</span></span>
    </button>`;
  }

  function sleepPanel(reference) {
    const sleep = DATA?.latest_sleep;
    const hours = finite(sleep?.hours) ? sleep.hours : reference?.sleep_h;
    const performance = finite(sleep?.performance) ? sleep.performance : reference?.sleep_perf;
    const efficiency = finite(sleep?.efficiency) ? sleep.efficiency : null;
    const stages = sleep?.stages_ms || {};
    const stageRows = [
      ['deep', 'Deep', '#b388ff'],
      ['rem', 'REM', '#00e5ff'],
      ['light', 'Core', '#55aeb7'],
      ['awake', 'Awake', '#6d7470']
    ];
    const total = Math.max(1, stageRows.reduce((sum, [key]) => sum + (finite(stages[key]) ? stages[key] : 0), 0));
    const stageTrack = stageRows.map(([key, label, color]) => {
      const ms = finite(stages[key]) ? stages[key] : 0;
      const share = Math.round(ms / total * 100);
      return `<span style="--stage:${color};--share:${share}%" title="${label}: ${share}%"><i></i><small>${label}</small><b>${fmt(ms / 3600000, 1)} h</b></span>`;
    }).join('');
    return `<button type="button" class="v6-panel v6-sleep" data-metric="sleep" aria-haspopup="dialog" aria-label="Sleep ${fmt(hours,1)} hours, performance ${fmt(performance)} percent. Open details">
      <span class="v6-panel-glint" aria-hidden="true"></span>
      <span class="v6-panel-head"><span><small>Last night</small><strong>Sleep architecture</strong></span><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a6 6 0 1 0 9 9 9 9 0 1 1-9-9"/></svg></span>
      <span class="v6-sleep-reading"><strong>${fmt(hours,1)}<small>h</small></strong><span><b>${fmt(performance)}%</b> performance</span><span><b>${fmt(efficiency)}%</b> efficiency</span></span>
      <span class="v6-stage-track">${stageTrack}</span>
    </button>`;
  }

  function protocolPanel(reference, state, hrvDelta, rhrDelta) {
    const recovery = reference?.recovery;
    const sleep = reference?.sleep_perf;
    const action = !finite(recovery)
      ? 'Wait for another night of data before adjusting training load.'
      : recovery >= 67
        ? 'High-intensity training is available today.'
        : recovery >= 34
          ? 'Keep training load moderate today.'
          : 'Prioritize walking, mobility, and an early night.';
    const context = finite(sleep) && sleep >= 85
      ? 'Sleep supports the current reserve.'
      : 'Sleep quality is today’s main constraint.';
    return `<article class="v6-panel v6-protocol" data-tone="${state.tone}">
      <span class="v6-panel-glint" aria-hidden="true"></span>
      <div class="v6-panel-head"><span><small>Today</small><strong>Training guidance</strong></span><span class="v6-status-dot">${escapeHTML(state.label)}</span></div>
      <p>${escapeHTML(action)}</p>
      <div class="v6-protocol-note"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z"/><path d="M12 8v4l2.5 2.5"/></svg><span><strong>${escapeHTML(context)}</strong><small>HRV ${escapeHTML(hrvDelta.text.toLowerCase())}; RHR ${escapeHTML(rhrDelta.text.toLowerCase())}.</small></span></div>
    </article>`;
  }

  function trajectoryRow({metric, key, label, unit, accent, reference, decimals = 0, domain, threshold, directional = true, summary = 'current'}) {
    const data = observations(key, 7, reference?.date);
    const values = data.map(row => row[key]).filter(finite);
    const current = values[values.length - 1];
    const personalBase = baseline(key, key === 'hrv' ? 28 : 14, reference?.date);
    const source = [...values, personalBase].filter(finite);
    const rawMin = source.length ? Math.min(...source) : 0;
    const rawMax = source.length ? Math.max(...source) : 100;
    const minimumPadding = key === 'workout_count' ? .35 : key === 'load' ? 1.5 : key === 'duration_min' ? 10 : key === 'hrv' ? 2 : 5;
    const padding = Math.max((rawMax - rawMin) * .24, minimumPadding);
    const low = domain ? domain[0] : Math.max(0, Math.floor(rawMin - padding));
    const high = domain ? domain[1] : Math.ceil(rawMax + padding);
    const spread = Math.max(1, high - low);
    const W = 620, H = 104, left = 8, right = 8, top = 12, bottom = 24;
    const plotW = W - left - right;
    const plotH = H - top - bottom;
    const x = index => data.length <= 1 ? W / 2 : left + index * plotW / (data.length - 1);
    const y = value => top + (high - value) / spread * plotH;
    const points = data.map((row, index) => ({value:row[key], x:x(index), y:y(row[key]), date:row.date}));
    const polyline = points.length > 1 ? `<polyline class="v6-trace-line" points="${points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ')}"/>` : '';
    const baselineLine = finite(personalBase) ? `<line class="v6-trace-baseline" x1="${left}" y1="${y(personalBase).toFixed(1)}" x2="${W - right}" y2="${y(personalBase).toFixed(1)}"/><text class="v6-trace-baseline-label" x="${W - right}" y="${Math.max(9, y(personalBase) - 5).toFixed(1)}" text-anchor="end">baseline ${fmt(personalBase, decimals)}</text>` : '';
    const thresholdLine = finite(threshold) && threshold > low && threshold < high ? `<line class="v6-trace-threshold" x1="${left}" y1="${y(threshold).toFixed(1)}" x2="${W - right}" y2="${y(threshold).toFixed(1)}"/><text class="v6-trace-threshold-label" x="${left}" y="${Math.max(9, y(threshold) - 5).toFixed(1)}">threshold ${fmt(threshold)}</text>` : '';
    const pointMarkup = points.map((point, index) => `<circle class="v6-trace-point${index === points.length - 1 ? ' is-current' : ''}" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="${index === points.length - 1 ? 4.2 : 2.4}"/>`).join('');
    const dayFormatter = new Intl.DateTimeFormat('en-US', {weekday:'short'});
    const days = points.map(point => {
      const date = new Date(`${point.date}T00:00:00Z`);
      return `<text class="v6-trace-day" x="${point.x.toFixed(1)}" y="${H - 4}" text-anchor="middle">${escapeHTML(dayFormatter.format(date).replace('.', ''))}</text>`;
    }).join('');
    const range = values.length ? `${fmt(Math.min(...values), decimals)}–${fmt(Math.max(...values), decimals)} ${unit}` : 'no data';
    const history = metricRows(key, reference?.date);
    const previousWindow = history.slice(-14, -7).map(row => row[key]).filter(finite);
    const summaryValue = summary === 'sum' ? values.reduce((sumValue, value) => sumValue + value, 0) : current;
    const comparisonValue = summary === 'sum'
      ? (previousWindow.length ? previousWindow.reduce((sumValue, value) => sumValue + value, 0) : null)
      : personalBase;
    const deltaValue = summaryValue - comparisonValue;
    const deltaUnit = unit === '%'
      ? ' pp'
      : unit === 'sessions' && Math.abs(deltaValue) === 1
        ? ' session'
        : ` ${unit}`;
    const difference = finite(summaryValue) && finite(comparisonValue) ? signed(deltaValue, deltaUnit, decimals) : {text:'Building baseline', tone:'neutral'};
    if (summary === 'sum') difference.text = difference.text.replace('vs baseline', 'vs prior week');
    if (!directional) difference.tone = 'neutral';
    const summaryLabel = summary === 'sum' ? 'over seven days' : 'now';
    const aria = `${label}: ${summaryLabel} ${fmt(summaryValue, decimals)} ${unit}; daily range ${range}; ${difference.text}. Open details`;
    return `<button type="button" class="v6-trajectory-row" style="--trace-accent:${accent}" data-tone="${difference.tone}" data-metric="${metric}" aria-haspopup="dialog" aria-label="${escapeHTML(aria)}">
      <span class="v6-trajectory-meta"><strong>${escapeHTML(label)}</strong><small>range · ${escapeHTML(range)}</small><em>${escapeHTML(difference.text)}</em></span>
      <span class="v6-trace-chart" role="img" aria-label="${escapeHTML(aria)}"><svg viewBox="0 0 ${W} ${H}" aria-hidden="true">${thresholdLine}${baselineLine}${polyline}${pointMarkup}${days}</svg></span>
      <span class="v6-trajectory-current"><strong>${fmt(summaryValue, decimals)}</strong><small>${escapeHTML(unit)}</small><em>${summary === 'sum' ? '7 days' : 'now'}</em></span>
      <svg class="v6-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
    </button>`;
  }

  function registerLoadContext() {
    if (typeof registerExpand !== 'function') return;
    const dates = rows().map(row => row.date);
    const specs = [
      {metric:'load', key:'load', title:'Daily strain', unit:'strain', color:'#ff4d00', dec:1, note:'Total strain from all WHOOP activities during the day.'},
      {metric:'duration', key:'duration_min', title:'Active time', unit:'min', color:'#ff9b65', dec:0, note:'Total duration of training sessions during the day.'},
      {metric:'sessions', key:'workout_count', title:'Training sessions', unit:'sessions', color:'#b8c7bf', dec:0, note:'Number of individual WHOOP activities during the day.'}
    ];
    specs.forEach(spec => registerExpand({metric:spec.metric, title:spec.title, unit:spec.unit, color:spec.color, values:rows().map(row => row[spec.key]), dates, dec:spec.dec, period:'day', note:spec.note}));
  }

  function registerOverviewMetrics() {
    if (typeof registerExpand !== 'function') return;
    const dates = rows().map(row => row.date);
    [
      {metric:'recovery', key:'recovery', title:'Recovery', unit:'%', color:'#c6ff00', dec:0, zones:RECOVERY_ZONES},
      {metric:'hrv', key:'hrv', title:'HRV', unit:'ms', color:'#c6ff00', dec:1},
      {metric:'rhr', key:'resting_hr', title:'Resting HR', unit:'bpm', color:'#dfe5e0', dec:0},
      {metric:'sleep_perf', key:'sleep_perf', title:'Sleep Performance', unit:'%', color:'#00e5ff', dec:0},
      {metric:'sleep', key:'sleep_h', title:'Sleep', unit:'h', color:'#00e5ff', dec:1}
    ].forEach(spec => registerExpand({
      metric:spec.metric, title:spec.title, unit:spec.unit, color:spec.color,
      values:rows().map(row => row[spec.key]), dates, dec:spec.dec,
      zones:spec.zones, period:'day'
    }));
  }

  function trajectoryPanel(reference) {
    return `<section class="v6-panel v6-trajectory" aria-labelledby="trajectory-title">
      <span class="v6-panel-glint" aria-hidden="true"></span>
      <div class="v6-panel-head"><span><small>7 days · system input</small><strong id="trajectory-title">Training load context</strong></span><span class="v6-legend"><i></i>value <b></b>personal baseline</span></div>
      ${trajectoryRow({metric:'load', key:'load', label:'Daily strain', unit:'strain', reference, accent:'#ff4d00', decimals:1, directional:false, summary:'sum'})}
      ${trajectoryRow({metric:'duration', key:'duration_min', label:'Active time', unit:'min', reference, accent:'#ff9b65', directional:false, summary:'sum'})}
      ${trajectoryRow({metric:'sessions', key:'workout_count', label:'Sessions', unit:'sessions', reference, accent:'#b8c7bf', directional:false, summary:'sum'})}
    </section>`;
  }

  function renderVitalityCore() {
    const panel = document.getElementById('panel-overview');
    if (!panel) return;
    const reference = latest();
    // The product view replaces the legacy Overview DOM. Register every
    // metric rendered by this replacement itself instead of depending on a
    // previous legacy render to have populated EXPAND_SPECS.
    registerOverviewMetrics();
    registerLoadContext();
    const state = recoveryState(reference?.recovery);
    const previous = previousDay(reference);
    const recoveryDelta = finite(reference?.recovery) && finite(previous?.recovery) ? reference.recovery - previous.recovery : null;
    const hrvBase = baseline('hrv', 28, reference?.date);
    const rhrBase = baseline('resting_hr', 28, reference?.date);
    const hrvDelta = signed(finite(reference?.hrv) && finite(hrvBase) ? (reference.hrv / hrvBase - 1) * 100 : null, '%');
    const rhrDelta = signed(finite(reference?.resting_hr) && finite(rhrBase) ? reference.resting_hr - rhrBase : null, '', 0, true);
    const sleepDelta = signed(finite(reference?.sleep_perf) ? reference.sleep_perf - baseline('sleep_perf', 14, reference?.date) : null, '%');
    const signals = [
      signalRow({metric:'hrv', label:'HRV', value:reference?.hrv, unit:'ms', detail:hrvDelta.text, tone:hrvDelta.tone, values:series('hrv',7,reference?.date)}),
      signalRow({metric:'rhr', label:'RHR', value:reference?.resting_hr, unit:'bpm', detail:rhrDelta.text, tone:rhrDelta.tone, values:series('resting_hr',7,reference?.date)}),
      signalRow({metric:'sleep_perf', label:'Sleep', value:reference?.sleep_perf, unit:'%', detail:sleepDelta.text, tone:sleepDelta.tone, values:series('sleep_perf',7,reference?.date)})
    ].join('');

    panel.className = 'panel state-screen vitality-screen';
    panel.innerHTML = `<main class="vitality-product" style="--vital-accent:${state.accent};--vital-rgb:${state.rgb}">
      <header class="v6-page-head"><div><p>${escapeHTML(dateLabel(reference?.date))}</p><h1>${escapeHTML(state.title)}</h1></div><span><i></i>WHOOP synced</span></header>
      <section class="v6-stage" aria-labelledby="v6-stage-title">
        <span class="v6-stage-ambient" aria-hidden="true"></span>
        <div class="v6-stage-copy">
          <h2 id="v6-stage-title">${escapeHTML(state.label)}</h2>
        </div>
        <div class="v6-core-wrap">${coreObject(reference?.recovery, state, recoveryDelta)}</div>
        <aside class="v6-signal-stack" aria-label="Key signals"><div class="v6-signal-title"><span>System signals</span><small>vs personal baseline</small></div>${signals}</aside>
      </section>
      <section class="v6-lower-grid" aria-label="State details">${sleepPanel(reference)}${protocolPanel(reference, state, hrvDelta, rhrDelta)}</section>
      ${trajectoryPanel(reference)}
    </main>`;
  }

  renderVitalityCore();
  if (typeof PANEL_RENDER !== 'undefined') PANEL_RENDER.overview = [renderVitalityCore];
})();
