(function installProductionState() {
  'use strict';

  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const rows = () => Array.isArray(DATA?.days) ? DATA.days : [];
  const observed = (key, through) => rows().filter(row => (!through || row.date <= through) && finite(row?.[key]));
  const latest = () => [...rows()].reverse().find(row => finite(row?.recovery)) || [...rows()].reverse().find(Boolean) || null;
  const mean = values => values.length ? values.reduce((sum,value) => sum + value, 0) / values.length : null;
  const number = (value, decimals = 0) => finite(value) ? value.toLocaleString('en-US',{minimumFractionDigits:decimals,maximumFractionDigits:decimals}) : '—';
  const signed = (value, suffix = '', decimals = 0) => {
    if (!finite(value)) return 'Building baseline';
    const rounded = Number(value.toFixed(decimals));
    if (Math.abs(rounded) < (decimals ? .05 : .5)) return 'At baseline';
    return `${rounded > 0 ? '+' : '−'}${number(Math.abs(rounded),decimals)}${suffix} vs baseline`;
  };
  const escapeHTML = value => String(value ?? '').replace(/[&<>"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[char]));
  const dateLabel = value => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || '');
    if (!match) return 'Date unavailable';
    const d = new Date(Date.UTC(+match[1],+match[2]-1,+match[3]));
    return new Intl.DateTimeFormat('en-US',{weekday:'long',day:'numeric',month:'long'}).format(d);
  };
  const recoveryState = value => !finite(value)
    ? {label:'Insufficient data',accent:'#a1aaa3',headline:'More physiological data needed'}
    : value >= 67
      ? {label:'High readiness',accent:'#d5f365',headline:'Ready for high training load'}
      : value >= 34
        ? {label:'Moderate readiness',accent:'#ffc24b',headline:'Keep training intensity moderate'}
        : {label:'Recovery priority',accent:'#ff6b6b',headline:'Prioritize Recovery today'};
  const baseline = (key, count, referenceDate) => mean(observed(key, referenceDate).slice(-(count + 1),-1).map(row => row[key]));
  const series = (key, count, referenceDate) => observed(key, referenceDate).slice(-count).map(row => row[key]);
  const previousDate = value => {
    if (!value) return null;
    const d = new Date(`${value}T00:00:00Z`); d.setUTCDate(d.getUTCDate()-1); return d.toISOString().slice(0,10);
  };
  const previousDay = reference => rows().find(row => row.date === previousDate(reference?.date)) || null;

  function spark(values, label) {
    // viewBox aspect (300×64 ≈ 4.7:1) roughly matches the signal-card plot area,
    // and the SVG scales uniformly (no preserveAspectRatio="none"), so dots and
    // the current-ring stay perfect circles instead of stretching into ovals.
    const W = 300, H = 64;
    const valid = values.filter(finite);
    if (!valid.length) return `<span class="state-mini-plot" role="img" aria-label="${escapeHTML(label)}: insufficient data"><svg viewBox="0 0 ${W} ${H}"><line class="state-chart-base" x1="0" y1="30" x2="${W}" y2="30" stroke-dasharray="4 7"/></svg></span>`;
    const min = Math.min(...valid), max = Math.max(...valid), spread = max-min || 1;
    const points = valid.map((value,index) => ({x:valid.length===1?W/2:index*(W/(valid.length-1)),y:51-((value-min)/spread)*36}));
    const polyline = points.length > 1 ? `<polyline class="state-chart-line" points="${points.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}"/>` : '';
    const dots = points.slice(0,-1).map(p=>`<circle class="state-chart-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.6"/>`).join('');
    const current = points[points.length-1];
    return `<span class="state-mini-plot" role="img" aria-label="${escapeHTML(label)}: ${valid.length} observations"><svg viewBox="0 0 ${W} ${H}"><rect class="state-chart-band" x="0" y="24" width="${W}" height="14" rx="4"/><line class="state-chart-base" x1="0" y1="31" x2="${W}" y2="31"/>${polyline}${dots}<circle class="state-chart-ring" cx="${current.x.toFixed(1)}" cy="${current.y.toFixed(1)}" r="6.5"/><circle class="state-chart-current" cx="${current.x.toFixed(1)}" cy="${current.y.toFixed(1)}" r="2.6"/></svg></span>`;
  }

  // Направление «куда лучше» — Tabler trending-up/down (точная геометрия, MIT)
  // вместо текстовых стрелок ↗/↘: одинаковый вес штриха с графиками карточек.
  const DIRECTION_ICONS = {
    up: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17l6 -6l4 4l8 -8"/><path d="M14 7l7 0l0 7"/></svg>',
    down: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7l6 6l4 -4l8 8"/><path d="M21 10l0 7l-7 0"/></svg>'
  };

  // Компактная метрика (структура V5 «плотная панель»): число и спарклайн
  // в одной строке, дельта под ними — никакой пустой середины. Две такие
  // карточки (HRV/RHR) занимают столько же высоты, сколько кольцо.
  function metricCard({label,value,unit,delta,accent='aqua',metric,values=[],decimals=0,direction='up'}) {
    const missing = !finite(value);
    return `<button type="button" class="state-card state-compact-card${missing?' is-missing':''}" data-accent="${accent}"${metric&&!missing?` data-metric="${metric}" aria-haspopup="dialog"`:''} aria-label="${escapeHTML(`${label}: ${missing?'insufficient data':`${number(value,decimals)} ${unit}`}. ${delta}`)}"${missing?' disabled':''}><span class="state-card-sheen" aria-hidden="true"></span><span class="state-card-head"><span class="state-card-label">${escapeHTML(label)}</span><span class="state-card-direction" aria-hidden="true">${DIRECTION_ICONS[direction]||DIRECTION_ICONS.up}</span></span><span class="state-compact-row"><span class="state-card-reading"><span class="state-card-value">${number(value,decimals)}</span>${unit?`<span class="state-card-unit">${escapeHTML(unit)}</span>`:''}</span>${spark(values,label)}</span><span class="state-card-delta">${escapeHTML(delta)}</span></button>`;
  }

  // Стеклянное кольцо-датчик — hero-инструмент V3 (заменяет силуэт по решению
  // 17.07). Кольцо — стеклянный жёлоб; заполненная дуга — «жидкость» зонного
  // цвета с вертикальным градиентом и бликом у вершины; конец дуги отмечен
  // ring+core маркером — тем же, что маркер текущего значения на графиках.
  function ringHero(current, recDelta, recommendationData) {
    const state = recoveryState(current);
    const has = finite(current);
    const pct = has ? Math.max(0, Math.min(100, current)) : 0;
    const R = 86, SW = 15, CX = 110, CY = 110;
    const C = 2 * Math.PI * R;
    const filled = C * pct / 100;
    const angle = (-90 + 360 * pct / 100) * Math.PI / 180;
    const mx = CX + R * Math.cos(angle), my = CY + R * Math.sin(angle);
    const deltaTxt = finite(recDelta)
      ? `${recDelta > 0 ? '+' : '−'}${number(Math.abs(recDelta))} vs yesterday`
      : 'Comparison unavailable';
    const tag = has ? 'button' : 'div';
    const attrs = has ? 'type="button" data-metric="recovery" aria-haspopup="dialog"' : 'role="img"';
    const svg = `<svg viewBox="0 0 220 220" aria-hidden="true">
      <defs>
        <linearGradient id="stateRingFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style="stop-color:color-mix(in srgb, var(--fig-accent) 84%, #ffffff)"/>
          <stop offset="1" style="stop-color:color-mix(in srgb, var(--fig-accent) 82%, #0a140b)"/>
        </linearGradient>
      </defs>
      <circle class="ring-track" cx="${CX}" cy="${CY}" r="${R}" stroke-width="${SW}"/>
      <circle class="ring-edge" cx="${CX}" cy="${CY}" r="${R + SW/2 + .5}"/>
      <circle class="ring-edge" cx="${CX}" cy="${CY}" r="${R - SW/2 - .5}"/>
      ${has ? `<circle class="ring-fill" cx="${CX}" cy="${CY}" r="${R}" stroke-width="${SW - 2.5}"
        stroke-dasharray="${filled.toFixed(1)} ${(C - filled).toFixed(1)}"
        transform="rotate(-90 ${CX} ${CY})" style="--ring-reveal:${filled.toFixed(1)}"/>
      <circle class="ring-marker-ring" cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="7"/>
      <circle class="ring-marker-core" cx="${mx.toFixed(1)}" cy="${my.toFixed(1)}" r="2.8"/>` : ''}
    </svg>`;
    const center = `<span class="state-ring-center"><span class="state-ring-value">${number(current)}</span><span class="state-ring-unit">%</span><span class="state-ring-metric">Recovery</span></span>`;
    return `<${tag} class="state-hero-card state-ring-panel${has ? '' : ' is-missing'}" ${attrs} style="--fig-accent:${state.accent}" aria-label="Recovery ${has ? number(current) : 'unavailable'} percent, ${state.label}${has ? '. Open details' : ''}">
      <span class="state-card-sheen" aria-hidden="true"></span>
      <span class="state-ring-wrap">${svg}${center}</span>
      <span class="state-ring-reading">
        <span class="state-ring-status">${escapeHTML(state.label)}</span>
        <span class="state-ring-delta${has ? '' : ' is-missing'}">${escapeHTML(deltaTxt)}</span>
      </span>
      <span class="state-recommendation"><span>Today’s recommendation</span><strong>${escapeHTML(recommendationData.action)}</strong></span>
    </${tag}>`;
  }

  // Сон-компакт (V5): часы + performance крупно, под ними список стадий с
  // барами — влезает в hero-ряд рядом с кольцом. Факты (эффективность,
  // пробуждения, дыхание) и вывод живут в detail-sheet по клику.
  function sleepPanel() {
    const sleep = DATA?.latest_sleep;
    const C = {deep:'#7b69a4', rem:'#48a596', light:'#3f8286', awake:'#3b403c'};
    const NAMES = {deep:'Deep', rem:'REM', light:'Light', awake:'Awake'};
    if (!sleep) return `<article class="state-card state-sleep-panel is-missing" data-accent="aqua"><span class="state-card-sheen" aria-hidden="true"></span><span class="state-card-label">Sleep · last night</span><span class="state-card-reading"><span class="state-card-value">No data</span></span><p class="state-card-context">WHOOP has not provided last night’s sleep architecture yet.</p></article>`;
    const stages = sleep.stages_ms || {};
    const total = Math.max(1, Object.values(stages).filter(finite).reduce((a,b) => a + b, 0));
    const row = key => {
      const ms = finite(stages[key]) ? stages[key] : 0;
      const share = Math.round(ms / total * 100);
      return `<div class="state-stage-row" style="--stage-color:${C[key]}">
        <span class="state-stage-name"><i aria-hidden="true"></i>${NAMES[key]}</span>
        <span class="state-stage-time">${number(ms / 3600000, 1)} h · ${share}%</span>
        <span class="state-stage-track" role="img" aria-label="${NAMES[key]}: ${share}% of the night"><i style="width:${share}%"></i></span>
      </div>`;
    };
    return `<button type="button" class="state-card state-sleep-panel" data-accent="aqua" data-metric="sleep" aria-haspopup="dialog" aria-label="Sleep: ${number(sleep.hours,1)} hours, Sleep Performance ${finite(sleep.performance)?number(sleep.performance):'—'}%. Open details">
      <span class="state-card-sheen" aria-hidden="true"></span>
      <span class="state-card-head"><span class="state-card-label">Sleep · last night</span><span class="state-card-direction" aria-hidden="true">${DIRECTION_ICONS.up}</span></span>
      <span class="state-card-reading"><span class="state-card-value">${number(sleep.hours,1)}</span><span class="state-card-unit">h</span><span class="state-card-perf">${finite(sleep.performance) ? `${number(sleep.performance)}%` : ''}</span></span>
      <span class="state-stage-list">${row('deep')}${row('rem')}${row('light')}${row('awake')}</span>
    </button>`;
  }

  // Большой недельный график — теперь во всю ширину (V5): бары — дневная
  // нагрузка, тонкая линия — recovery. Подписи дней — HTML-строкой под SVG
  // (в растягиваемом preserveAspectRatio:none SVG-текст искажается и на
  // мобильном ужимается до нечитаемого; flex-ячейки держат типографику).
  function weekCard(reference) {
    const days = rows().filter(r => r.date <= (reference?.date || '9999') && (finite(r.load) || finite(r.recovery))).slice(-14);
    if (days.length < 2) return `<article class="state-card state-week-panel is-missing" data-accent="orange"><span class="state-card-sheen" aria-hidden="true"></span><span class="state-card-label">Recent load and Recovery</span><span class="state-card-reading"><span class="state-card-value">Insufficient data</span></span><p class="state-card-context">The chart will appear after several days of measurements.</p></article>`;
    const W = 1280, H = 210, L = 8, Rp = 8, T = 12, B = 8;
    const plotW = W - L - Rp, plotH = H - T - B;
    const step = plotW / days.length;
    const maxLoad = Math.max(10, ...days.map(d => finite(d.load) ? d.load : 0));
    const barW = Math.min(26, step * .42);
    const bars = days.map((d, i) => {
      if (!finite(d.load) || d.load <= 0) return '';
      const h = Math.max(2, d.load / maxLoad * plotH);
      const x = L + i * step + (step - barW) / 2;
      const isLast = i === days.length - 1;
      return `<rect class="week-bar${isLast ? ' is-current' : ''}" x="${x.toFixed(1)}" y="${(T + plotH - h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="${Math.min(9, barW / 2).toFixed(1)}"/>`;
    }).join('');
    const recPts = days.map((d, i) => finite(d.recovery) ? {x: L + i * step + step / 2, y: T + (1 - d.recovery / 100) * plotH} : null).filter(Boolean);
    const line = recPts.length > 1 ? `<polyline class="week-line" points="${recPts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}"/>` : '';
    const dots = recPts.slice(0, -1).map(p => `<circle class="week-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.4"/>`).join('');
    const cur = recPts[recPts.length - 1];
    const marker = cur ? `<circle class="week-ring" cx="${cur.x.toFixed(1)}" cy="${cur.y.toFixed(1)}" r="6.5"/><circle class="week-current" cx="${cur.x.toFixed(1)}" cy="${cur.y.toFixed(1)}" r="2.6"/>` : '';
    const dayFmt = new Intl.DateTimeFormat('en-US', {weekday: 'short'});
    const dayCells = days.map((d, i) => {
      const isLast = i === days.length - 1;
      // на узких экранах каждый второй день скрывается через CSS (nth-child),
      // последний подписан всегда
      const text = dayFmt.format(new Date(`${d.date}T00:00:00Z`));
      return `<i class="week-day${isLast ? ' is-current' : ''}${(days.length - 1 - i) % 2 !== 0 ? ' is-odd' : ''}">${text}</i>`;
    }).join('');
    const week = days.slice(-7);
    const weekLoad = week.reduce((sum, d) => sum + (finite(d.load) ? d.load : 0), 0);
    const weekRec = mean(week.filter(d => finite(d.recovery)).map(d => d.recovery));
    return `<button type="button" class="state-card state-week-panel" data-accent="orange" data-metric="load" aria-haspopup="dialog" aria-label="Last 7 days: total load ${number(weekLoad,1)} strain, average Recovery ${finite(weekRec)?number(weekRec):'—'}%. Open details">
      <span class="state-card-sheen" aria-hidden="true"></span>
      <span class="state-card-head"><span class="state-card-label">Recent load and Recovery</span><span class="state-card-direction" aria-hidden="true">${DIRECTION_ICONS.up}</span></span>
      <span class="state-card-reading"><span class="state-card-value">${number(weekLoad,1)}</span><span class="state-card-unit">strain · 7 days</span><span class="state-card-perf">Recovery ${finite(weekRec)?number(weekRec):'—'}%</span></span>
      <span class="state-week-plot" role="img" aria-label="Load and Recovery across ${days.length} days"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}${line}${dots}${marker}</svg></span>
      <span class="week-days" aria-hidden="true">${dayCells}</span>
    </button>`;
  }

  function recommendation(reference, currentSeven, previousSeven) {
    const sleep=reference?.sleep_h, hrv=reference?.hrv, hrvBase=baseline('hrv',28,reference?.date), recovery=reference?.recovery;
    if (!finite(recovery)) return {level:'is-warning',title:'New data needed',copy:'Sync WHOOP to generate today’s recommendation.',action:'Decision deferred'};
    if (recovery<34) return {level:'is-critical',title:'Reduce intensity',copy:'Recovery is in the red zone. Prioritize sleep, nutrition, and low-intensity movement.',action:'Recovery day'};
    if (finite(sleep)&&sleep<6.5) return {level:'is-warning',title:'Account for sleep debt',copy:'Readiness is not critical, but a short night increases the cost of high intensity.',action:'Moderate load'};
    if (finite(hrvBase)&&finite(hrv)&&hrv<hrvBase*.9) return {level:'is-warning',title:'Monitor HRV',copy:'HRV is well below your personal baseline. Start gently and reassess how you feel.',action:'Adaptive start'};
    const trend=finite(currentSeven)&&finite(previousSeven)&&currentSeven>previousSeven;
    return {level:'',title:'No constraints detected',copy:trend?'The 7-day context is improving and the key signals agree.':'The key signals agree and do not require a plan adjustment.',action:recovery>=67?'High-intensity training available':'Moderate training available'};
  }

  function renderState() {
    const panel=document.getElementById('panel-overview');
    if (!panel) return;
    const reference=latest();
    const rec=reference?.recovery, state=recoveryState(rec), prior=previousDay(reference);
    const hrv=finite(reference?.hrv)?reference.hrv:null, rhr=finite(reference?.resting_hr)?reference.resting_hr:null;
    const hrvSeries=series('hrv',7,reference?.date), rhrSeries=series('resting_hr',7,reference?.date);
    const currentSeven=mean(observed('recovery',reference?.date).slice(-7).map(r=>r.recovery));
    const previousSeven=mean(observed('recovery',reference?.date).slice(-14,-7).map(r=>r.recovery));
    const recDelta=finite(rec)&&finite(prior?.recovery)?rec-prior.recovery:null;
    const recommendationData=recommendation(reference,currentSeven,previousSeven);
    const hrvDelta=signed(finite(hrv)&&finite(baseline('hrv',28,reference?.date))?(hrv/baseline('hrv',28,reference?.date)-1)*100:null,'%',0);
    const rhrDelta=signed(finite(rhr)?rhr-baseline('resting_hr',28,reference?.date):null,'',0);
    const hrvCard=metricCard({label:'HRV',value:hrv,unit:'ms',delta:hrvDelta,accent:'aqua',metric:'hrv',values:hrvSeries});
    const rhrCard=metricCard({label:'RHR',value:rhr,unit:'bpm',delta:rhrDelta,accent:'neutral',metric:'rhr',values:rhrSeries,direction:'down'});
    const attentionStrip=recommendationData.level
      ? `<article class="state-attention-strip state-attention ${recommendationData.level}"><span class="state-card-label">Attention</span><strong>${escapeHTML(recommendationData.title)}</strong><p>${escapeHTML(recommendationData.copy)}</p></article>`
      : '';
    panel.className='panel state-screen';
    panel.innerHTML=`<div class="state-product">
      <header class="state-head"><p class="state-date">${escapeHTML(dateLabel(reference?.date))}</p><h1 class="state-title" id="state-title">${state.headline}</h1></header>
      <section class="state-hero-grid" aria-labelledby="state-title">
        ${ringHero(rec,recDelta,recommendationData)}
        <div class="state-compact-stack">${hrvCard}${rhrCard}</div>
        ${sleepPanel()}
      </section>
      ${attentionStrip}
      <section class="state-week-section" aria-label="Recent load and Recovery">
        ${weekCard(reference)}
      </section>
    </div>`;
  }

  // Environment host: mounting point + always-on gate for whatever paints the
  // app background (97-shader-bg.js draws into it). Kept minimal here — no
  // canvas/drawing of its own — since the WebGL "mesh drift" shader (17.07)
  // replaced the earlier 2D star field and owns its own render loop.
  function installEnvironment() {
    if (document.querySelector('.state-environment')) return;
    const host=document.createElement('div'); host.className='state-environment'; host.setAttribute('aria-hidden','true');
    document.body.prepend(host);
    const sync=()=>{
      // Phase 6 promotes the environment from an Overview decoration to a
      // dashboard foundation, so it's active on every tab, not just State.
      const active=Boolean(document.querySelector('.shell'));
      document.body.classList.toggle('state-active',active);
    };
    addEventListener('hashchange',sync);
    document.getElementById('tabs')?.addEventListener('click',()=>requestAnimationFrame(sync));
    document.addEventListener('visibilitychange',sync);
    window.syncStateEnvironment=sync;
    sync();
  }

  renderState();
  installEnvironment();
  if (typeof PANEL_RENDER !== 'undefined') PANEL_RENDER.overview=[renderState];
})();
