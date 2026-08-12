(function installDynamicsScreen() {
  'use strict';

  const panel = document.getElementById('panel-analytics');
  if (!panel) return;

  const days = Array.isArray(DATA?.days) ? DATA.days : [];
  const finite = value => value !== null && value !== undefined && value !== ''
    && Number.isFinite(Number(value));
  const values = (rows, key) => rows
    .map(row => row?.[key])
    .filter(finite)
    .map(Number);
  const average = list => list.length ? list.reduce((sum, value) => sum + value, 0) / list.length : null;
  const readableDate = value => {
    try { return typeof md === 'function' ? md(value) : String(value || '—'); }
    catch (_) { return String(value || '—'); }
  };
  const readableNumber = (value, digits = 0) => {
    if (!finite(value)) return '—';
    return Number(value).toLocaleString('en-US', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  };
  const zoneLabel = value => {
    if (!finite(value)) return 'zone unavailable';
    if (Number(value) >= 67) return 'high zone';
    if (Number(value) >= 34) return 'moderate zone';
    return 'low zone';
  };
  const correlationLabel = tuple => {
    const coefficient = Array.isArray(tuple) && finite(tuple[0]) ? Number(tuple[0]) : null;
    if (coefficient === null) return 'Not enough paired observations to estimate a relationship.';
    const magnitude = Math.abs(coefficient);
    const strength = magnitude >= .7 ? 'strong' : magnitude >= .4 ? 'moderate' : magnitude >= .2 ? 'weak' : 'negligible';
    const direction = coefficient > .08 ? 'positive' : coefficient < -.08 ? 'negative' : 'without a clear direction';
    const sample = Array.isArray(tuple) && finite(tuple[1])
      ? ` Based on ${readableNumber(tuple[1])} paired observations.`
      : '';
    return `${strength[0].toUpperCase()}${strength.slice(1)}, ${direction}; r = ${readableNumber(coefficient, 2)}.${sample}`;
  };

  const intro = panel.querySelector('.panel-intro');
  if (intro) {
    const title = intro.querySelector('h1');
    const copy = intro.querySelector('p');
    if (title) title.textContent = 'Trends';
    if (copy) copy.textContent = 'Recovery, sleep, and load trends focused on durable change rather than isolated fluctuations.';
  }

  // Post-merge structure is exactly 4 cards in a fixed order: Recovery hero,
  // Volume (weekly + by-sport segments), Connections (sleep chart + other-r
  // row), Load balance. Kickers are looked up by each card's own heading id
  // instead of position, so the mapping can't silently drift if a card moves.
  const cards = [...panel.querySelectorAll(':scope > .card, :scope > .cols2 > .card')];
  const kickerByHeading = {
    'Daily Recovery': 'Recovery',
    'Activity volume': 'Activity volume',
    'Factor relationships': 'Factor relationships',
    'Daily load by Recovery': 'Load balance'
  };
  cards.forEach((card, index) => {
    card.classList.add('trend-card');
    const heading = card.querySelector(':scope > h2, :scope > .card-head h2');
    if (!heading) return;
    heading.id ||= `trend-card-title-${index + 1}`;
    const kicker = document.createElement('p');
    kicker.className = 'trend-card-kicker';
    kicker.textContent = kickerByHeading[heading.textContent.trim()] || 'Trends';
    const head = card.querySelector(':scope > .card-head');
    if (head) card.insertBefore(kicker, head);
    else card.insertBefore(kicker, heading);
  });

  const directGroups = [...panel.querySelectorAll(':scope > .cols2')];
  directGroups.forEach(group => group.classList.add('trends-grid', 'trends-primary-row'));

  const summary = document.createElement('section');
  summary.className = 'trends-summary';
  summary.setAttribute('aria-label', 'Trend summary');

  const recoveryRows = days.filter(row => finite(row?.recovery));
  const latestRecovery = recoveryRows.at(-1);
  const latestWeek = Array.isArray(DATA?.weekly) ? DATA.weekly.at(-1) : null;
  const sleepCorrelation = DATA?.corr?.sleep_vs_recovery;
  const summaryItems = [
    {
      tone: 'recovery', label: 'Current recovery',
      value: latestRecovery ? readableNumber(latestRecovery.recovery) : '—', unit: latestRecovery ? '%' : '',
      note: latestRecovery ? `${zoneLabel(latestRecovery.recovery)} · ${readableDate(latestRecovery.date)}` : 'Insufficient Recovery data'
    },
    {
      tone: 'load', label: 'Latest full week',
      value: latestWeek && finite(latestWeek.load) ? readableNumber(latestWeek.load, 1) : '—', unit: latestWeek ? 'strain' : '',
      note: latestWeek ? `${readableNumber(latestWeek.count)} activities · ${readableNumber(latestWeek.dur_h, 1)} h` : 'No weekly load history'
    },
    {
      tone: 'sleep', label: 'Sleep and recovery',
      value: Array.isArray(sleepCorrelation) && finite(sleepCorrelation[0]) ? readableNumber(sleepCorrelation[0], 2) : '—', unit: 'r',
      note: correlationLabel(sleepCorrelation)
    }
  ];

  summaryItems.forEach(item => {
    const article = document.createElement('article');
    article.className = 'trend-summary-card';
    article.dataset.tone = item.tone;
    const label = document.createElement('p');
    label.className = 'trend-summary-label';
    label.textContent = item.label;
    const value = document.createElement('div');
    value.className = 'trend-summary-value';
    value.textContent = item.value;
    if (item.unit) {
      const unit = document.createElement('small');
      unit.textContent = item.unit;
      value.append(document.createTextNode(' '));
      value.append(unit);
    }
    const note = document.createElement('p');
    note.className = 'trend-summary-note';
    note.textContent = item.note;
    article.append(label, value, note);
    summary.append(article);
  });
  intro?.insertAdjacentElement('afterend', summary);

  const chartSpecs = {
    c_recovery: {
      title: 'Daily Recovery',
      summary() {
        const windowRows = (typeof recoveryRange !== 'undefined' && recoveryRange !== 'all') ? days.slice(-Number(recoveryRange)) : days;
        const series = windowRows.filter(row => finite(row?.recovery));
        if (!series.length) return 'No Recovery measurements in the selected period.';
        const first = Number(series[0].recovery), last = Number(series.at(-1).recovery);
        const base = average(values(days, 'recovery'));
        const direction = last - first > 3 ? 'above the start of the period' : last - first < -3 ? 'below the start of the period' : 'near the start-of-period level';
        return `Latest value ${readableNumber(last)}%, ${zoneLabel(last)}; ${direction}. Window baseline: ${readableNumber(base)}%. ${series.length} measurements.`;
      }
    },
    c_weekly: {
      // Merged card: same host renders either the weekly bars or the by-sport
      // breakdown depending on weeklyMetric, so the summary branches too.
      title: 'Activity volume',
      summary() {
        if (weeklyMetric === 'sports') {
          const sports = Array.isArray(DATA?.sports) ? DATA.sports : [];
          if (!sports.length) return 'No activities are available for comparison.';
          const top = [...sports].filter(row => finite(row?.total_strain)).sort((a, b) => Number(b.total_strain) - Number(a.total_strain))[0];
          return top ? `Highest total load — ${top.sport}: ${readableNumber(top.total_strain, 1)} strain across ${readableNumber(top.n)} activities.` : 'Insufficient data to rank activities.';
        }
        const series = Array.isArray(DATA?.weekly) ? DATA.weekly : [];
        if (!series.length) return 'Weekly activity history is not available yet.';
        const metric = typeof weeklyMetric === 'string' ? weeklyMetric : 'load';
        const units = { load: 'strain', dur_h: 'h', count: 'activities' };
        const last = series.at(-1)?.[metric];
        return `Latest week: ${readableNumber(last, metric === 'count' ? 0 : 1)} ${units[metric] || ''}. ${series.length} weeks compared.`;
      }
    },
    c_load: {
      title: 'Daily load with Recovery context',
      summary() {
        const active = days.filter(row => Number(row?.load) > 0);
        if (!active.length) return 'No days with recorded load.';
        const peak = active.reduce((best, row) => Number(row.load) > Number(best.load) ? row : best);
        const risk = active.filter(row => Number(row.recovery) < 34 && Number(row.load) >= 10).length;
        return `Peak ${readableNumber(peak.load, 1)} strain — ${readableDate(peak.date)}. High-load days with low Recovery: ${risk}.`;
      }
    },
    c_sleep: {
      title: 'Sleep duration and Recovery',
      summary: () => correlationLabel(DATA?.corr?.sleep_vs_recovery)
    }
  };

  const ensureSummary = (host, spec) => {
    const card = host.closest('.trend-card');
    if (!card) return null;
    const heading = card.querySelector(':scope > h2, :scope > .card-head h2');
    const id = `${host.id}-summary`;
    let node = card.querySelector(`#${id}`);
    if (!node) {
      node = document.createElement('p');
      node.className = 'chart-summary';
      node.id = id;
      node.setAttribute('aria-live', 'polite');
      const desc = host === card.querySelector('.chart, .small-mult') ? card.querySelector(':scope > .desc') : null;
      if (desc) desc.insertAdjacentElement('afterend', node);
      else host.insertAdjacentElement('beforebegin', node);
    }
    const text = typeof spec.summary === 'function' ? spec.summary() : String(spec.summary || '');
    if (node.textContent !== text) node.textContent = text;
    host.classList.add('trend-chart-viewport');
    host.tabIndex = 0;
    host.setAttribute('role', 'img');
    host.setAttribute('aria-label', spec.title);
    host.setAttribute('aria-describedby', id);
    if (heading) host.setAttribute('aria-labelledby', heading.id);
    requestAnimationFrame(() => {
      host.classList.toggle('has-horizontal-overflow', host.scrollWidth > host.clientWidth + 1);
    });
    return node;
  };

  const enhanceCharts = () => Object.entries(chartSpecs).forEach(([id, spec]) => {
    const host = document.getElementById(id);
    if (!host) return;
    ensureSummary(host, spec);
  });
  enhanceCharts();
  addEventListener('resize', enhanceCharts, { passive: true });

  const chartObserver = new MutationObserver(() => enhanceCharts());
  chartObserver.observe(panel, { childList: true, subtree: true });

  panel.querySelectorAll('.seg').forEach(group => {
    const chart = group.dataset.chart;
    group.setAttribute('aria-label', chart === 'recovery' ? 'Recovery chart period' : 'Weekly volume metric');
    const controlledId = chart === 'recovery' ? 'c_recovery' : 'c_weekly';
    group.querySelectorAll('button').forEach(button => button.setAttribute('aria-controls', controlledId));
  });
})();
