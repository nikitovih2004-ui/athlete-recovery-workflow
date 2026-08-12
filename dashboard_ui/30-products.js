(function installProductScreens() {
  'use strict';

  const factorsPanel = document.getElementById('panel-insights');
  const activityPanel = document.getElementById('panel-workouts');
  if (!factorsPanel || !activityPanel || typeof DATA !== 'object') return;

  const setPanelIntro = (panel, title, copy) => {
    const intro = panel.querySelector('.panel-intro');
    if (!intro) return;
    const heading = intro.querySelector('h1');
    const description = intro.querySelector('p');
    if (heading) heading.textContent = title;
    if (description) description.textContent = copy;
  };
  setPanelIntro(
    factorsPanel,
    'Factors',
    'Recovery patterns, weekly rhythm, and observed relationships between sleep, load, and readiness.'
  );
  setPanelIntro(
    activityPanel,
    'Activity',
    'WHOOP activities, strength logs, cardio, and supplements in one verifiable history.'
  );

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // Exact Tabler Icons outline geometry (MIT) — replaces the previous hand-drawn
  // approximations. Keys keep their local semantic names; stroke width stays 1.7.
  const svgNS = 'http://www.w3.org/2000/svg';
  const paths = {
    calendar: ['M4 7a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2v-12z', 'M16 3v4', 'M8 3v4', 'M4 11h16', 'M11 15h1', 'M12 15v3'],
    pulse: ['M3 12h4l3 8l4 -16l3 8h4'],
    activity: ['M3 17l6 -6l4 4l8 -8', 'M14 7l7 0l0 7'],
    capsule: ['M4.5 12.5l8 -8a4.94 4.94 0 0 1 7 7l-8 8a4.94 4.94 0 0 1 -7 -7', 'M8.5 8.5l7 7'],
    strength: ['M2 12h1', 'M6 8h-2a1 1 0 0 0 -1 1v6a1 1 0 0 0 1 1h2', 'M6 7v10a1 1 0 0 0 1 1h1a1 1 0 0 0 1 -1v-10a1 1 0 0 0 -1 -1h-1a1 1 0 0 0 -1 1z', 'M9 12h6', 'M15 7v10a1 1 0 0 0 1 1h1a1 1 0 0 0 1 -1v-10a1 1 0 0 0 -1 -1h-1a1 1 0 0 0 -1 1z', 'M18 8h2a1 1 0 0 1 1 1v6a1 1 0 0 1 -1 1h-2', 'M22 12h-1'],
    cardio: ['M13 4m-1 0a1 1 0 1 0 2 0a1 1 0 1 0 -2 0', 'M4 17l5 1l.75 -1.5', 'M15 21l0 -4l-4 -3l1 -6', 'M7 12l0 -3l5 -1l3 3l3 1'],
    record: ['M8 21l8 0', 'M12 17l0 4', 'M7 4l10 0', 'M17 4v8a5 5 0 0 1 -10 0v-8', 'M5 9m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0', 'M19 9m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0'],
    empty: ['M4 4m0 2a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2z', 'M4 13h3l3 3h4l3 -3h3']
  };

  const icon = name => {
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.7');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    (paths[name] || paths.empty).forEach(d => {
      const path = document.createElementNS(svgNS, 'path');
      path.setAttribute('d', d);
      svg.appendChild(path);
    });
    return svg;
  };

  const count = value => Array.isArray(value) ? value.length : 0;
  const numeric = value => value !== null && value !== undefined && value !== ''
    && Number.isFinite(Number(value)) ? Number(value) : null;
  const format = (value, digits = 0) => value === null
    ? '—'
    : value.toLocaleString('en-US', { maximumFractionDigits: digits });
  const pluralize = (value, singular, plural = `${singular}s`) => `${format(value)} ${Number(value) === 1 ? singular : plural}`;

  function addKicker(card, text) {
    const heading = card?.querySelector(':scope > h2, :scope > .card-head h2');
    if (!heading) return;
    const head = heading.closest('.card-head');
    const anchor = head || heading;
    if (anchor.previousElementSibling?.classList.contains('product-card-kicker')) return;
    anchor.before(make('span', 'product-card-kicker', text));
  }

  function statStrip(panel, items) {
    if (panel.querySelector(':scope > .product-stat-strip')) return;
    const strip = make('div', 'product-stat-strip');
    strip.setAttribute('aria-label', 'Section highlights');
    strip.setAttribute('role', 'list');
    items.forEach(item => {
      const card = make('div', 'product-stat');
      card.dataset.tone = item.tone || 'neutral';
      card.setAttribute('role', 'listitem');
      const value = make('strong', 'product-stat__value', item.value);
      if (item.unit) value.append(document.createTextNode(' '), make('small', '', item.unit));
      card.append(
        make('span', 'product-stat__label', item.label),
        value,
        make('span', 'product-stat__note', item.note)
      );
      strip.appendChild(card);
    });
    panel.querySelector('.panel-intro')?.after(strip);
  }

  function sectionHead(title, copy) {
    const head = make('div', 'product-section-head');
    const text = make('div', 'product-section-head__copy');
    text.append(make('h2', '', title), make('p', '', copy));
    head.appendChild(text);
    return head;
  }

  function structureFactors() {
    if (factorsPanel.dataset.productStructured === 'true') return;
    factorsPanel.dataset.productStructured = 'true';

    const days = Array.isArray(DATA.days) ? DATA.days : [];
    const knownRecovery = days.map(day => numeric(day.recovery)).filter(value => value !== null);
    const avgRecovery = numeric(DATA.summary?.avg_recovery)
      ?? (knownRecovery.length ? knownRecovery.reduce((sum, value) => sum + value, 0) / knownRecovery.length : null);
    statStrip(factorsPanel, [
      { label: 'Observation window', value: format(days.length), note: 'days in the current dataset', tone: 'neutral' },
      { label: 'Average Recovery', value: avgRecovery === null ? '—' : format(avgRecovery), unit: avgRecovery === null ? '' : '%', note: `${knownRecovery.length} measurements`, tone: 'recovery' },
      { label: 'Load on low Recovery', value: format(count(DATA.red_hard_days)), note: 'days need attention', tone: 'risk' },
      { label: 'High readiness without load', value: format(count(DATA.undertrained)), note: 'potential training windows', tone: 'recovery' }
    ]);

    const calendarCard = document.getElementById('hm_card');
    const rhythmCard = document.getElementById('c_dow')?.closest('.card');
    if (calendarCard && rhythmCard && !factorsPanel.querySelector('.factor-primary-grid')) {
      const head = sectionHead('Recovery patterns', 'Daily context first, followed by the stable weekly rhythm.');
      const grid = make('div', 'factor-primary-grid');
      calendarCard.before(head);
      head.after(grid);
      grid.append(calendarCard, rhythmCard);
      calendarCard.classList.add('product-card--calendar');
      rhythmCard.classList.add('product-card--rhythm');
      addKicker(calendarCard, 'Recovery map');
      addKicker(rhythmCard, 'Weekly rhythm');
    }

    const insightGrid = document.getElementById('insights');
    if (insightGrid && !insightGrid.previousElementSibling?.classList.contains('product-section-head')) {
      insightGrid.before(sectionHead('Signals and relationships', 'Observed relationships in the data, without causal claims.'));
    }

    // Секция «Дни для решения» удалена: её списки живут внутри evidence-карточек
    // (fillInsights), отдельного блока с дублями больше нет.

    const narrative = document.getElementById('ls_card');
    if (narrative) {
      narrative.classList.add('product-card--report');
      addKicker(narrative, 'Lifestyle context');
    }
  }

  function addSupplementEvidence() {
    const insightGrid = document.getElementById('insights');
    if (!insightGrid || factorsPanel.querySelector('.factor-supplement-evidence')) return;
    const entries = Array.isArray(DATA.supplements_log) ? DATA.supplements_log : [];
    const observed = entries.filter(entry => entry?.taken !== null && entry?.taken !== undefined);
    const taken = observed.filter(entry => Number(entry.taken) === 1);
    const skipped = observed.filter(entry => Number(entry.taken) === 0);
    const unknown = entries.length - observed.length;
    const head = sectionHead(
      'Daily factors',
      'This is an observation log, not proof of causation: intake status is shown separately from physiological outcomes.'
    );
    const card = make('article', 'card factor-supplement-evidence');
    const cardHead = make('div', 'card-head');
    const title = make('h2', '', 'Supplements · log coverage');
    const source = make('span', 'factor-source-tag', 'manual log');
    cardHead.append(title, source);
    if (!entries.length) {
      // Пустой журнал не заслуживает сетки из трёх нулей: одна строка-заглушка.
      card.classList.add('is-empty');
      card.append(cardHead, make('p', 'desc', 'The log is empty. Entries will appear here and in Activity after the first Telegram check-in.'));
    } else {
      const copy = make('p', 'desc', `${format(entries.length)} events are logged in the current dataset. Unknown status does not count as skipped.`);
      const stats = make('div', 'factor-evidence-stats');
      [
        ['Taken', format(taken.length), 'intake confirmed'],
        ['Skipped', format(skipped.length), 'explicitly marked'],
        ['Unknown', format(unknown), 'not counted as skipped']
      ].forEach(([label, value, note]) => {
        const stat = make('div', 'factor-evidence-stat');
        stat.append(make('span', '', label), make('strong', '', value), make('small', '', note));
        stats.appendChild(stat);
      });
      card.append(cardHead, copy, stats);
    }
    // Порядок вкладки: паттерны → evidence-карточки → строка добавок → отчёт,
    // поэтому «Ежедневные факторы» встают ПОСЛЕ сетки инсайтов.
    insightGrid.after(head, card);
  }

  function structureActivity() {
    if (activityPanel.dataset.productStructured === 'true') return;
    activityPanel.dataset.productStructured = 'true';

    // Stat-strip больше не считает ручные журналы (три из четырёх плиток были
    // нулями, пока журналы пусты) — вместо этого показываем то, что не бывает
    // нулевым, пока есть хоть один WHOOP-workout: тренировки, покрытие
    // последних 14 дней, средняя недельная нагрузка, самый частый вид.
    const days = Array.isArray(DATA.days) ? DATA.days : [];
    const recent14 = days.slice(-14);
    const activeDays14 = recent14.filter(day => (numeric(day?.load) || 0) > 0).length;
    const weekly = Array.isArray(DATA.weekly) ? DATA.weekly : [];
    const avgWeeklyLoad = weekly.length
      ? weekly.reduce((sum, week) => sum + (numeric(week?.load) || 0), 0) / weekly.length
      : null;
    const sports = Array.isArray(DATA.sports) ? DATA.sports : [];
    const topSport = sports.length ? [...sports].sort((a, b) => (numeric(b.n) || 0) - (numeric(a.n) || 0))[0] : null;
    statStrip(activityPanel, [
      { label: 'WHOOP activities', value: format(numeric(DATA.summary?.n_workouts) ?? 0), note: 'in the source period', tone: 'load' },
      { label: 'Active days', value: format(activeDays14), unit: 'of 14', note: 'last two weeks', tone: 'neutral' },
      { label: 'Avg load/week', value: avgWeeklyLoad === null ? '—' : format(avgWeeklyLoad, 1), unit: avgWeeklyLoad === null ? '' : 'strain', note: `${weekly.length} ${weekly.length === 1 ? 'week' : 'weeks'} in the dataset`, tone: 'neutral' },
      { label: 'Most frequent activity', value: topSport ? topSport.sport : '—', note: topSport ? pluralize(topSport.n, 'activity', 'activities') : 'no data', tone: 'recovery' }
    ]);

    const manual = document.getElementById('manual_workouts_container')?.closest('.card');
    const cardio = document.getElementById('cardio_exercises_container')?.closest('.card');
    const supplements = document.getElementById('supplements_log_container')?.closest('.card');
    if (manual && !activityPanel.querySelector('.activity-overview-grid')) {
      const days = Array.isArray(DATA.days) ? DATA.days : [];
      const recent = days.slice(-14);
      const loads = recent.map(day => numeric(day?.load) || 0);
      const maxLoad = Math.max(...loads, 1);
      const overviewHead = sectionHead(
        'Load and sources',
        'WHOOP-recorded load and coverage first, followed by the manual logs that complete the history.'
      );
      overviewHead.classList.add('activity-overview-head');
      const grid = make('div', 'activity-overview-grid');
      const loadCard = make('article', 'card activity-load-card');
      const loadTitle = make('h2', '', 'WHOOP load · last 14 days');
      const loadCopy = make('p', 'desc', recent.length
        ? `${recent.length} calendar days · ${format(recent.filter(day => (numeric(day?.load) || 0) > 0).length)} active days`
        : 'Load history will appear after the first WHOOP sync.');
      const bars = make('div', 'activity-load-bars');
      bars.setAttribute('role', 'img');
      bars.setAttribute('aria-label', recent.length
        ? `WHOOP load across ${recent.length} days. Peak ${format(maxLoad, 1)} strain.`
        : 'Insufficient WHOOP load data.');
      recent.forEach(day => {
        const bar = make('span', '');
        const load = numeric(day?.load) || 0;
        bar.style.setProperty('--activity-load-height', `${Math.max(5, Math.round(load / maxLoad * 100))}%`);
        bar.title = `${day.date}: ${format(load, 1)} strain`;
        bars.appendChild(bar);
      });
      loadCard.append(loadTitle, loadCopy, bars);

      const sourceCard = make('article', 'card activity-source-card');
      sourceCard.append(
        make('h2', '', 'Data sources'),
        make('p', 'desc', 'Each source remains distinct; similarly named fields are not blended into a false sense of precision.')
      );
      const sourceRows = make('div', 'activity-source-rows');
      [
        ['WHOOP', pluralize(numeric(DATA.summary?.n_workouts) ?? 0, 'activity', 'activities'), 'Automatic strain, duration, heart rate, and records'],
        ['Strength log', pluralize(count(DATA.manual_workouts), 'entry', 'entries'), 'Manual exercises, weights, reps, and volume'],
        ['Cardio and supplements', pluralize(count(DATA.cardio_exercises) + count(DATA.supplements_log), 'entry', 'entries'), 'Manual sessions and daily-factor events']
      ].forEach(([source, value, note]) => {
        const row = make('div', 'activity-source-row');
        const label = make('span', 'activity-source-tag', source);
        const content = make('div', '');
        content.append(make('strong', '', value), make('small', '', note));
        row.append(label, content);
        sourceRows.appendChild(row);
      });
      sourceCard.appendChild(sourceRows);
      manual.before(overviewHead, grid);
      grid.append(loadCard, sourceCard);

      // Прогрессия (принята из Динамики) встаёт сразу после «Нагрузка и
      // источники», перед журналами — карточка уже размечена в HTML,
      // здесь только позиционируем её.
      const progressCard = document.getElementById('progress_card');
      if (progressCard) {
        addKicker(progressCard, 'Progression');
        grid.after(progressCard);
      }
    }

    // Три журнала: если ВСЕ пусты, полноразмерная сетка из трёх «пустых»
    // карточек (~300px каждая) — чистый балласт. Схлопываем в одну строку
    // со счётчиками; как только в любом журнале появится хотя бы одна
    // запись, возвращаемся к обычной сетке полных карточек (та же ветка,
    // что уже решает show/hide по данным в emptyState/decorateDetails).
    const logCounts = {
      manual: count(DATA.manual_workouts),
      cardio: count(DATA.cardio_exercises),
      supplements: count(DATA.supplements_log)
    };
    const allLogsEmpty = !logCounts.manual && !logCounts.cardio && !logCounts.supplements;
    if (manual && cardio && supplements && allLogsEmpty && !activityPanel.querySelector('.activity-logs-summary')) {
      const head = sectionHead('Activity log', 'Dated manual entries complete the WHOOP history where automatic data is unavailable.');
      const row = make('article', 'card activity-logs-summary');
      row.append(make('h2', '', 'Logs'));
      row.append(make('p', 'desc', 'No manual entries yet. Log strength, cardio, or a supplement in Telegram and its card will appear here.'));
      const counters = make('div', 'activity-logs-counters');
      [['Strength', logCounts.manual], ['Cardio', logCounts.cardio], ['Supplements', logCounts.supplements]].forEach(([label, value]) => {
        const counter = make('div', 'activity-logs-counter');
        counter.append(make('strong', '', format(value)), make('span', '', label));
        counters.appendChild(counter);
      });
      row.appendChild(counters);
      manual.before(head, row);
      // Контейнеры остаются в DOM (renderXWorkouts продолжают писать в них),
      // просто выведены из потока — так при появлении данных на следующей
      // синхронизации достаточно перезагрузить страницу, а не чинить разметку.
      [manual, cardio, supplements].forEach(card => card.classList.add('is-hidden-log'));
    } else if (manual && cardio && supplements && !allLogsEmpty && !activityPanel.querySelector('.activity-log-grid')) {
      const head = sectionHead('Activity log', 'Dated manual entries complete the WHOOP history where automatic data is unavailable.');
      const grid = make('div', 'activity-log-grid');
      manual.before(head);
      head.after(grid);
      [manual, cardio, supplements].forEach(card => {
        card.classList.add('activity-log-card');
        grid.appendChild(card);
      });
      addKicker(manual, 'Strength log');
      addKicker(cardio, 'Cardio log');
      addKicker(supplements, 'Supplement log');
      manual.querySelector(':scope > h2').textContent = 'Strength training';
      cardio.querySelector(':scope > h2').textContent = 'Cardio';
      supplements.querySelector(':scope > h2').textContent = 'Supplements';
    }

    // «Полная таблица» (t_records) удалена — те же 6 полей уже были в
    // ранжированном списке плюс дата, которая теперь раскрывается по клику
    // на строке (см. rankRecords). Одна карточка вместо дублирующей пары.
    const recordsList = document.getElementById('rank_records')?.closest('.card');
    if (recordsList && !recordsList.dataset.productKickered) {
      recordsList.dataset.productKickered = 'true';
      const head = sectionHead('History and records', 'Ranked by peak strain; select a row to reveal the record date.');
      recordsList.before(head);
      addKicker(recordsList, 'Personal records');
    }
  }

  function chartSummary(container, id, text) {
    if (!container || container.querySelector(`#${id}`)) return;
    const summary = make('p', 'product-chart-summary', text);
    summary.id = id;
    container.appendChild(summary);
    container.setAttribute('role', 'group');
    container.setAttribute('aria-describedby', id);
    container.querySelectorAll(':scope > svg').forEach(svg => svg.setAttribute('aria-hidden', 'true'));
  }

  function enhanceFactors() {
    const days = Array.isArray(DATA.days) ? DATA.days : [];
    const known = days.filter(day => numeric(day.recovery) !== null);
    const avg = known.length
      ? known.reduce((sum, day) => sum + Number(day.recovery), 0) / known.length
      : null;
    chartSummary(
      document.getElementById('c_heatmap'),
      'heatmap_access_summary',
      known.length
        ? `The calendar contains ${known.length} Recovery measurements across ${days.length} days; average ${format(avg)}%. Exact dates can be compared with the signals below.`
        : 'There are not enough measurements for the Recovery calendar yet.'
    );
    chartSummary(
      document.getElementById('c_dow'),
      'dow_access_summary',
      known.length
        ? `The weekly chart aggregates ${known.length} Recovery measurements by weekday. Exact values are available in labels and card details.`
        : 'There are not enough measurements to calculate the weekly rhythm yet.'
    );
  }

  function stripLeadingEmoji(text) {
    return text
      .replace(/^📅\s*/u, '')
      .replace(/^💊\s*/u, '')
      .replace(/^🏃(?:‍♂️)?\s*/u, '')
      .trimStart();
  }

  function decorateDetails(container, iconName) {
    container.querySelectorAll('details').forEach(details => {
      if (details.dataset.productEnhanced === 'true') return;
      details.dataset.productEnhanced = 'true';
      details.classList.add('activity-details');
      const summary = details.querySelector(':scope > summary');
      const main = summary?.firstElementChild;
      const total = summary?.lastElementChild;
      if (summary && main) {
        const label = stripLeadingEmoji(main.textContent || '');
        main.textContent = '';
        main.classList.add('activity-summary-main');
        main.append(icon(iconName), document.createTextNode(label));
        summary.setAttribute('aria-label', `${label}. ${total?.textContent || ''}`.trim());
      }
      total?.classList.add('activity-summary-total');
    });
  }

  // Компактная однострочная заглушка вместо центрированного блока на 178px:
  // это состояние встречается только когда СОСЕДНИЕ журналы уже развернулись
  // (все-три-пустых случай схлопнут отдельно, см. structureActivity), так что
  // экономия высоты здесь и держит суммарную "цену" пустых состояний низкой.
  function emptyState(container, label, note) {
    if (!container) return;
    if (container.querySelector('details')) {
      delete container.dataset.productEmpty;
      return;
    }
    if (container.querySelector('.product-empty-line')) return;
    const existing = container.querySelector('.ls-empty, p');
    if (!existing) return;
    const message = existing.textContent.trim();
    container.dataset.productEmpty = 'true';
    container.textContent = '';
    container.appendChild(make('p', 'product-empty-line', message || label));
  }

  function enhanceRecords() {
    // recordsList уже несёт every field the removed "Полная таблица" (t_records)
    // showed, plus the click-to-reveal date (см. rankRecords в dashboard_template.html);
    // its refined presentation lives in .records-list-card below.
    document.getElementById('rank_records')?.closest('.card')?.classList.add('records-list-card');
    const rank = document.getElementById('rank_records');
    if (rank?.querySelector('.rank-item')) delete rank.dataset.productEmpty;
    if (rank && !rank.children.length) {
      rank.dataset.productEmpty = 'true';
      const item = make('li', 'product-empty');
      const body = make('div', '');
      const iconWrap = make('span', 'product-empty__icon');
      iconWrap.appendChild(icon('record'));
      body.append(iconWrap, make('p', 'product-empty__label', 'No personal records yet.'), make('p', 'product-empty__note', 'They will appear after the first completed WHOOP activity.'));
      item.appendChild(body);
      rank.appendChild(item);
    }
  }

  function enhanceActivity() {
    const manual = document.getElementById('manual_workouts_container');
    const cardio = document.getElementById('cardio_exercises_container');
    const supplements = document.getElementById('supplements_log_container');
    decorateDetails(manual, 'strength');
    decorateDetails(cardio, 'cardio');
    decorateDetails(supplements, 'capsule');
    emptyState(manual, 'No strength entries yet.', 'A new entry will appear after a workout is saved through Telegram.');
    emptyState(cardio, 'No cardio logged yet.', 'Add a session through Telegram or import a screenshot.');
    emptyState(supplements, 'No supplements logged yet.', 'The log distinguishes taken, explicitly skipped, and missing data.');
    enhanceRecords();
  }

  structureFactors();
  addSupplementEvidence();
  structureActivity();
  enhanceFactors();
  enhanceActivity();

  const factorsObserver = new MutationObserver(() => enhanceFactors());
  factorsObserver.observe(factorsPanel, { childList: true, subtree: true });
  const activityObserver = new MutationObserver(() => enhanceActivity());
  activityObserver.observe(activityPanel, { childList: true, subtree: true });
})();
