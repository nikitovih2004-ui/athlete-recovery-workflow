(function installDashboardPolish() {
  'use strict';

  const finite = value => Number.isFinite(value);
  const validDays = metric => (Array.isArray(DATA.days) ? DATA.days : [])
    .filter(row => finite(row?.[metric]));
  const pairedDays = (a, b) => (Array.isArray(DATA.days) ? DATA.days : [])
    .filter(row => finite(row?.[a]) && finite(row?.[b]));

  const emptyChart = (id, copy) => {
    const target = document.getElementById(id);
    if (!target) return;
    target.replaceChildren();
    const state = document.createElement('div');
    state.className = 'chart-empty';
    const title = document.createElement('strong');
    title.textContent = 'Insufficient data';
    const description = document.createElement('span');
    description.textContent = copy || 'The chart will appear after several comparable measurements are available.';
    state.append(title, description);
    target.append(state);
  };

  const readiness = {
    chartRecovery: { id: 'c_recovery', ready: () => validDays('recovery').length >= 2 },
    chartSports: { id: 'c_sports', ready: () => Array.isArray(DATA.sports) && DATA.sports.length > 0 },
    chartScatter: { id: 'c_scatter', ready: () => pairedDays('recovery', 'load').length >= 2 },
    chartSleep: {
      id: 'c_sleep',
      ready: () => {
        const pairs = pairedDays('sleep_h', 'recovery');
        return pairs.length >= 2 && new Set(pairs.map(row => row.sleep_h)).size >= 2;
      }
    },
    chartProgress: {
      id: 'c_progress',
      ready: () => Object.values(DATA.sport_series || {}).some(series => Array.isArray(series) && series.length >= 2)
    },
    heatmapCalendar: { id: 'c_heatmap', ready: () => validDays('recovery').length > 0 },
    chartDow: { id: 'c_dow', ready: () => validDays('recovery').length > 0 }
  };

  ['analytics', 'insights'].forEach(panelName => {
    if (!PANEL_RENDER?.[panelName]) return;
    PANEL_RENDER[panelName] = PANEL_RENDER[panelName].map(renderer => {
      const rule = readiness[renderer.name];
      if (!rule) return renderer;
      return function guardedDashboardRenderer() {
        if (!rule.ready()) {
          emptyChart(rule.id);
          return;
        }
        try {
          renderer();
        } catch (error) {
          console.warn(`Dashboard renderer ${renderer.name} used an insufficient-data fallback.`);
          emptyChart(rule.id);
        }
      };
    });
  });

  // Guards are installed after the legacy initial route render. Re-render an
  // already-open data panel once so its first frame also receives the guards.
  if (['analytics', 'insights'].includes(activeTab)) {
    panelDirty[activeTab] = true;
    renderPanel(activeTab);
  }

  const metricSeries = {
    hrv: 'hrv', rhr: 'resting_hr', sleep: 'sleep_h', sleep_perf: 'sleep_perf',
    load: 'load', duration: 'duration_min', sessions: 'workout_count', recovery: 'recovery'
  };
  const syncInsufficientCards = () => {
    document.querySelectorAll('[data-metric]').forEach(card => {
      const metric = card.dataset.metric;
      const dataKey = metricSeries[metric];
      if (!dataKey || validDays(dataKey).length) return;
      card.classList.add('is-insufficient-card');
      card.removeAttribute('data-metric');
      card.removeAttribute('role');
      card.removeAttribute('tabindex');
      card.removeAttribute('aria-label');
      const status = card.querySelector('.u, .trend');
      if (status) status.textContent = 'Insufficient data';
    });
  };
  syncInsufficientCards();

  const chartLabels = {
    c_recovery: ['Daily Recovery', 'Recovery time series with readiness zones and a trend line.'],
    c_weekly: ['Weekly load', 'Comparison of weekly strain, duration, or activity count.'],
    c_sports: ['Load by activity', 'Total strain by activity type.'],
    c_load: ['Daily load and Recovery', 'Daily load bars colored by Recovery zone.'],
    c_scatter: ['Recovery and load', 'Each point compares same-day Recovery and load.'],
    c_sleep: ['Sleep and Recovery', 'Each point compares sleep duration with next-morning Recovery.'],
    c_progress: ['Leading activity trends', 'Compact strain time series for the most frequent activities.'],
    c_heatmap: ['Recovery calendar', 'Calendar map of daily Recovery zones.'],
    c_dow: ['Weekly rhythm', 'Average Recovery and load by weekday.']
  };
  const syncChartSemantics = () => Object.entries(chartLabels).forEach(([id, [label, summary]]) => {
    const chart = document.getElementById(id);
    if (!chart) return;
    const described = chart.getAttribute('aria-describedby');
    if (!chart.hasAttribute('role')) chart.setAttribute('role', 'group');
    if (!chart.hasAttribute('aria-label') && !chart.hasAttribute('aria-labelledby')) {
      chart.setAttribute('aria-label', label);
    }
    if (!described && !chart.querySelector('.chart-summary')) {
      const text = document.createElement('p');
      text.className = 'chart-summary sr-only';
      text.textContent = summary;
      chart.prepend(text);
    }
    chart.querySelectorAll('svg').forEach(svg => {
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
    });
  });
  syncChartSemantics();

  const table = document.getElementById('t_records');
  if (table) {
    if (!table.querySelector('caption')) {
      const caption = document.createElement('caption');
      caption.textContent = 'All WHOOP activities in the selected period';
      table.prepend(caption);
    }
    table.querySelectorAll('thead th').forEach(header => header.setAttribute('scope', 'col'));
  }

  const shell = document.querySelector('.shell');
  const skip = document.querySelector('.skip-link');
  const syncModalIsolation = () => {
    const open = Boolean(document.querySelector('.sheet-backdrop'));
    if (shell) {
      if (open) {
        shell.setAttribute('inert', '');
        shell.setAttribute('aria-hidden', 'true');
      } else {
        shell.removeAttribute('inert');
        shell.removeAttribute('aria-hidden');
      }
    }
    if (skip) skip.toggleAttribute('inert', open);
    window.syncStateEnvironment?.();
  };
  window.syncDashboardModalIsolation = syncModalIsolation;
  syncModalIsolation();

  let polishFrame = 0;
  const schedulePolish = () => {
    if (polishFrame) return;
    polishFrame = requestAnimationFrame(() => {
      polishFrame = 0;
      syncInsufficientCards();
      syncChartSemantics();
      syncModalIsolation();
    });
  };
  const main = document.querySelector('main');
  if (main) new MutationObserver(schedulePolish).observe(main, { childList: true, subtree: true });
  new MutationObserver(syncModalIsolation).observe(document.body, { childList: true });
})();
