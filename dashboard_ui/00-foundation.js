(function installDashboardFoundation() {
  'use strict';

  document.documentElement.dataset.design = 'physiological-horizon';
  document.documentElement.lang = 'en';
  document.title = 'WHOOP · Physiological State';

  const main = document.querySelector('main');
  if (main && !main.id) main.id = 'main-content';
  if (main && !document.querySelector('.skip-link')) {
    const skip = document.createElement('a');
    skip.className = 'skip-link';
    skip.href = '#main-content';
    skip.textContent = 'Skip to main content';
    document.body.prepend(skip);
  }

  const labels = {
    overview: 'State',
    analytics: 'Trends',
    insights: 'Factors',
    workouts: 'Activity'
  };
  const tablist = document.querySelector('[role="tablist"]');
  if (tablist) tablist.setAttribute('aria-label', 'WHOOP Dashboard sections');

  const tabs = [...document.querySelectorAll('[role="tab"][data-tab]')];
  tabs.forEach((tab, index) => {
    const key = tab.dataset.tab;
    tab.id = `tab-${key}`;
    tab.textContent = labels[key] || tab.textContent;
    tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
    const panel = document.getElementById(`panel-${key}`);
    if (panel) panel.setAttribute('aria-labelledby', tab.id);
    tab.dataset.index = String(index);
  });

  tablist?.addEventListener('keydown', event => {
    const current = event.target.closest('[role="tab"]');
    if (!current) return;
    const index = tabs.indexOf(current);
    let next = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next === null) return;
    event.preventDefault();
    tabs[next].focus();
    tabs[next].click();
  });

  const syncTabs = () => tabs.forEach(tab => {
    tab.tabIndex = tab.classList.contains('active') ? 0 : -1;
  });
  tablist?.addEventListener('click', syncTabs);
  window.addEventListener('hashchange', syncTabs);

  document.querySelectorAll('.seg').forEach(group => {
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Period or metric');
    group.querySelectorAll('button').forEach(button => {
      button.setAttribute('aria-pressed', button.classList.contains('on') ? 'true' : 'false');
    });
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('.seg button');
    if (!button) return;
    button.closest('.seg')?.querySelectorAll('button').forEach(item => {
      item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
    });
  });

  const finePointer = matchMedia('(hover:hover) and (pointer:fine)');
  if (finePointer.matches) {
    document.addEventListener('pointermove', event => {
      const card = event.target.closest('.expandable');
      if (!card) return;
      const rect = card.getBoundingClientRect();
      card.style.setProperty('--spot-x', `${event.clientX - rect.left}px`);
      card.style.setProperty('--spot-y', `${event.clientY - rect.top}px`);
    }, { passive: true });
  }

  const metricDefinitions = [
    ['.metric-hrv', 'hrv'],
    ['.metric-rhr', 'resting_hr'],
    ['.metric-sleep', 'sleep_h'],
    ['.metric-sleep-performance', 'sleep_perf'],
    ['.metric-load', 'load'],
    ['.metric-recovery', 'recovery']
  ];
  const svgNS = 'http://www.w3.org/2000/svg';
  const svgEl = (name, attrs = {}) => {
    const node = document.createElementNS(svgNS, name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  };
  const appendMetricChart = (card, metric) => {
    if (!card || card.querySelector('.metric-mini')) return;
    const values = (Array.isArray(DATA.days) ? DATA.days : [])
      .map(row => Number.isFinite(row?.[metric]) ? row[metric] : null)
      .filter(value => value !== null)
      .slice(-8);
    const plot = document.createElement('div');
    plot.className = `metric-mini${values.length < 4 ? ' is-insufficient' : ''}`;
    plot.setAttribute('aria-hidden', 'true');
    const svg = svgEl('svg', { viewBox: '0 0 160 40', preserveAspectRatio: 'none' });
    svg.append(svgEl('rect', { class: 'metric-range', x: 0, y: 15, width: 160, height: 12, rx: 3 }));
    svg.append(svgEl('line', { class: 'metric-baseline', x1: 0, y1: 20, x2: 160, y2: 20 }));
    if (values.length) {
      const min = Math.min(...values), max = Math.max(...values);
      const span = max - min || 1;
      const points = values.map((value, index) => ({
        x: values.length === 1 ? 80 : index * (160 / (values.length - 1)),
        y: 34 - ((value - min) / span) * 28
      }));
      if (values.length >= 4) {
        svg.append(svgEl('polyline', {
          class: 'metric-path',
          points: points.map(point => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ')
        }));
      }
      points.slice(0, -1).forEach(point => {
        svg.append(svgEl('circle', { class: 'metric-dot', cx: point.x, cy: point.y, r: 2.25 }));
      });
      const current = points[points.length - 1];
      svg.append(svgEl('circle', { class: 'metric-current-ring', cx: current.x, cy: current.y, r: 5 }));
      svg.append(svgEl('circle', { class: 'metric-current-core', cx: current.x, cy: current.y, r: 2 }));
    }
    plot.append(svg);
    card.append(plot);
  };
  const enhanceMetricCards = () => metricDefinitions.forEach(([selector, metric]) => {
    appendMetricChart(document.querySelector(selector), metric);
  });
  enhanceMetricCards();
  const tileRoot = document.getElementById('tiles');
  if (tileRoot) {
    let enhancementFrame = 0;
    new MutationObserver(() => {
      if (enhancementFrame) return;
      enhancementFrame = requestAnimationFrame(() => {
        enhancementFrame = 0;
        enhanceMetricCards();
      });
    }).observe(tileRoot, { childList: true });
  }
})();
