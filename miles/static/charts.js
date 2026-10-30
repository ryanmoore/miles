// Shared chart/formatting helpers for races.html: fmt (time/pace/name
// formatting, from design-lab.html), gradientColor/recentColor (build-line
// highlight palettes, from index.html), staggerEndLabels (endLabel
// de-collision, from design-lab.html), and sparklineSVG (dense-table
// sparkline, generalized from lab/variant-c.js's fixed 12-week version).
// Plain global script — load after nav.js.

const fmt = {
  // seconds -> "2:54:00"
  time(s) {
    if (s == null) return null;
    const h = Math.floor(s / 3600);
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(Math.round(s % 60)).padStart(2, '0');
    return `${h}:${m}:${sec}`;
  },
  // seconds -> "2:54" (h:mm, for tight spots)
  timeShort(s) {
    if (s == null) return null;
    return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`;
  },
  // decimal minutes per mile (7.82) -> "7:49"
  pace(dec) {
    if (dec == null) return null;
    let m = Math.floor(dec);
    let s = Math.round((dec - m) * 60);
    if (s === 60) { m += 1; s = 0; }
    return `${m}:${String(s).padStart(2, '0')}`;
  },
  // 1 -> "1st", 22 -> "22nd", 11 -> "11th"
  ordinal(n) {
    const lastTwo = n % 100;
    const suffix = lastTwo >= 11 && lastTwo <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] ?? 'th');
    return `${n}${suffix}`;
  },
  // "Grandma's Marathon 2023" -> "Grandma's '23"; virtual -> "(v)"
  shortName(name, date) {
    const nameYear = (name.match(/\d{4}/) ?? [])[0];
    const year = nameYear ?? (date ? date.slice(0, 4) : null);
    const yr = year ? `'${year.slice(2)}` : '';
    let short = name
      .replace("Grandma's Marathon", "Grandma's")
      .replace('Marathon', '')
      .replace('Virtual', '(v)')
      .replace(/\s+/g, ' ').trim();
    if (nameYear) short = short.replace(nameYear, yr).replace(/\s+/g, ' ').trim();
    else if (year) short = `${short} ${yr}`;
    return short;
  },
  isVirtual(m) { return m.name?.includes('Virtual') ?? false; },
};

// Fastest-N highlight gradient (blue -> teal).
function gradientColor(rank, total, dark) {
  const t = total > 1 ? rank / (total - 1) : 0;
  const l = dark ? 58 + t * 10 : 42 + t * 6;
  return `hsl(${Math.round(215 + t * (15 - 215))}, 82%, ${l}%)`;
}

// Recent-N highlight gradient (gold -> green).
function recentColor(rank, total, dark) {
  const t = total > 1 ? rank / (total - 1) : 0;
  const l = dark ? 60 : 45;
  return `hsl(${Math.round(48 + t * (20 - 48))}, 90%, ${l}%)`;
}

// ECharts labelLayout.moveOverlap ignores endLabels, so converging lines
// stack their labels. Re-space them in pixel space after setOption: sort by
// final-value y, push apart to minGap, apply per-series offsets.
function staggerEndLabels(chart, minGap = 15) {
  const series = chart.getOption().series ?? [];
  const items = [];
  series.forEach((s, i) => {
    if (!s.endLabel?.show) return;
    const v = s.data[s.data.length - 1];
    const y = chart.convertToPixel({ yAxisIndex: 0 }, v);
    if (Number.isFinite(y)) items.push({ i, y0: y, y });
  });
  if (items.length < 2) return;
  items.sort((a, b) => a.y - b.y);
  for (let k = 1; k < items.length; k++) {
    if (items[k].y - items[k - 1].y < minGap) items[k].y = items[k - 1].y + minGap;
  }
  const patch = series.map(() => ({}));
  for (const it of items) {
    patch[it.i] = { endLabel: { offset: [4, it.y - it.y0] } };
  }
  chart.setOption({ series: patch });
}

// nWeeks+1 bars for offsets -nWeeks..0, scaled to a shared globalMax so rows
// stay visually comparable within a bucket's table.
function sparklineSVG(weeksArr, nWeeks, globalMax, isPR) {
  const map = Object.fromEntries((weeksArr ?? []).map(w => [w.offset, w.miles]));
  const vals = Array.from({ length: nWeeks + 1 }, (_, i) => map[i - nWeeks] ?? 0);
  const peak = Math.max(0, ...vals);
  const barW = 5, gap = 2;
  const width = vals.length * (barW + gap) - gap;
  const bars = vals.map((v, i) => {
    const h = globalMax > 0 ? (v / globalMax) * 20 : 0;
    const x = i * (barW + gap);
    return `<rect x="${x}" y="${(20 - h).toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" />`;
  }).join('');
  const opacity = isPR ? 0.9 : 0.45;
  return `<svg class="spark" width="${width}" height="20" viewBox="0 0 ${width} 20" style="fill:var(--series-1);fill-opacity:${opacity}"><title>${peak.toFixed(1)} mi peak</title>${bars}</svg>`;
}

// Click-to-sort table headers. Reads the CURRENT tbody at click time (pages
// re-render tbodies in place), reorders existing <tr> nodes so row classes/
// links/handlers survive. Idempotent — safe to call on every re-render.
// initial: { col, dir } labels the column the renderer already sorted by, so
// an arrow is visible on load — the cue that headers sort at all. No reorder.
function makeSortable(tableEl, initial) {
  if (tableEl.dataset.sortable === '1') return;
  tableEl.dataset.sortable = '1';

  if (!document.getElementById('sortable-style')) {
    const style = document.createElement('style');
    style.id = 'sortable-style';
    style.textContent = `
      th.sorted-asc::after { content: ' ▲'; font-size: 0.5625rem; }
      th.sorted-desc::after { content: ' ▼'; font-size: 0.5625rem; }
      table[data-sortable] thead th { cursor: pointer; user-select: none; }
      table[data-sortable] thead th:hover { color: var(--text-secondary); }
      table[data-sortable] thead th:not(.sorted-asc):not(.sorted-desc):hover::after {
        content: ' ↕'; font-size: 0.5625rem; color: var(--muted);
      }
    `;
    document.head.appendChild(style);
  }

  if (initial) {
    const th = tableEl.querySelectorAll('thead th')[initial.col];
    if (th) th.classList.add(initial.dir === 'asc' ? 'sorted-asc' : 'sorted-desc');
  }

  // "—"/empty -> null (always sorts last); H:MM:SS or M:SS (+/- prefix ok)
  // -> seconds; YYYY-MM-DD stays a string (sorts correctly as-is); numbers
  // (commas/decimals ok) -> float; anything else -> lowercase string.
  function parseCell(raw) {
    const s = raw.trim();
    if (s === '' || s === '—') return { type: 'null', value: null };
    const time = s.match(/^([+-])?(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (time) {
      const sign = time[1] === '-' ? -1 : 1;
      const [h, m, sec] = time[4] != null ? [time[2], time[3], time[4]] : ['0', time[2], time[3]];
      return { type: 'num', value: sign * (Number(h) * 3600 + Number(m) * 60 + Number(sec)) };
    }
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return { type: 'str', value: s };
    if (/^[+-]?\d[\d,]*(\.\d+)?$/.test(s)) return { type: 'num', value: parseFloat(s.replace(/,/g, '')) };
    return { type: 'str', value: s.toLowerCase() };
  }

  function cellValue(cell) {
    return parseCell(cell?.dataset.sort ?? cell?.textContent ?? '');
  }

  function compare(a, b, dir) {
    if (a.type === 'null' && b.type === 'null') return 0;
    if (a.type === 'null') return 1;
    if (b.type === 'null') return -1;
    const cmp = a.type === 'num' && b.type === 'num'
      ? a.value - b.value
      : (a.value < b.value ? -1 : a.value > b.value ? 1 : 0);
    return dir * cmp;
  }

  tableEl.querySelectorAll('thead th').forEach((th, colIndex) => {
    th.addEventListener('click', () => {
      const tbody = tableEl.querySelector('tbody');
      if (!tbody) return;
      const rows = Array.from(tbody.children);
      const parsed = rows.map(row => cellValue(row.children[colIndex]));

      let dir;
      if (th.classList.contains('sorted-asc')) dir = -1;
      else if (th.classList.contains('sorted-desc')) dir = 1;
      else dir = parsed.find(p => p.type !== 'null')?.type === 'num' ? -1 : 1;

      rows.map((row, i) => ({ row, val: parsed[i] }))
        .sort((a, b) => compare(a.val, b.val, dir))
        .forEach(({ row }) => tbody.appendChild(row));

      tableEl.querySelectorAll('thead th').forEach(t => t.classList.remove('sorted-asc', 'sorted-desc'));
      th.classList.add(dir === 1 ? 'sorted-asc' : 'sorted-desc');
    });
  });
}

// Terrain/route page helpers.

const MILE_METERS = 1609.344;
const FT_PER_METER = 3.28084;

function speedToPace(speedMps) {
  if (speedMps == null || speedMps <= 0) return null;
  return (MILE_METERS / speedMps) / 60;
}

// Label for a grade bin {lo_pct, hi_pct}; null marks an open end.
function gradeBinLabel(b) {
  if (b.lo_pct == null) return `< ${b.hi_pct.toFixed(0)}%`;
  if (b.hi_pct == null) return `≥ ${b.lo_pct.toFixed(0)}%`;
  return `${b.lo_pct.toFixed(0)}–${b.hi_pct.toFixed(0)}%`;
}

function statTile(label, value, sub, note) {
  return `
    <div class="stat">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${value}${sub ? `<span class="stat-sub">${sub}</span>` : ''}</div>
      ${note ? `<div class="stat-sub">${note}</div>` : ''}
    </div>
  `;
}

// Runnability classes read through the athlete's own terrain thresholds
// (terrain.py's classify_grade), not a fixed grade-band scheme.
const CLASS_LABEL = { run: 'Run', grind: 'Grind', hike: 'Hike' };

function classColor(theme, cls) {
  return theme.route[cls] ?? theme.route.neutral;
}

function classLegendHtml(theme) {
  return ['run', 'grind', 'hike'].map(cls => `
    <span><span class="swatch" style="background:${classColor(theme, cls)}"></span>${CLASS_LABEL[cls]}</span>
  `).join('');
}

// Runs of consecutive points ({dist_m, ele_m, cls, ...}) sharing a class;
// a point without a class is a run of class null.
function classRuns(points) {
  const runs = [];
  for (const p of points) {
    const cls = p.cls ?? null;
    const last = runs[runs.length - 1];
    if (last && last.cls === cls) last.points.push(p);
    else runs.push({ cls, points: [p] });
  }
  return runs;
}

// ECharts option for an elevation profile (miles by feet) with each class run
// banded under one neutral line. Points are in route_point_classes' shape.
function classedElevationOption(theme, points) {
  const data = points.map(p => [p.dist_m / MILE_METERS, p.ele_m != null ? p.ele_m * FT_PER_METER : null]);
  const runs = classRuns(points);
  const markAreaData = runs
    .map((run, i) => {
      const last = run.points[run.points.length - 1];
      return {
        cls: run.cls,
        start: run.points[0].dist_m / MILE_METERS,
        end: (runs[i + 1] ? runs[i + 1].points[0] : last).dist_m / MILE_METERS,
      };
    })
    .filter(b => b.cls != null)
    .map(b => [
      { xAxis: b.start, itemStyle: { color: classColor(theme, b.cls), opacity: 0.22 } },
      { xAxis: b.end },
    ]);
  return {
    animation: false,
    grid: { top: 12, right: 32, bottom: 32, left: 56 },
    tooltip: chartAxisTooltip(theme, (items) => {
      if (!items.length) return '';
      const v = items[0].data[1];
      return `${items[0].axisValue.toFixed(2)} mi<br>${v != null ? Math.round(v) : '—'} ft`;
    }),
    xAxis: chartValueAxis(theme, {
      name: 'mi',
      nameGap: 6,
      min: 0,
      max: data.length ? data[data.length - 1][0] : undefined,
      axisLabel: { formatter: (v) => String(+v.toFixed(1)), showMaxLabel: false },
      axisLine: { lineStyle: { color: theme.gridline } },
      splitLine: { show: false },
    }),
    yAxis: chartValueAxis(theme, { name: 'ft', scale: true }),
    series: [{
      name: 'Elevation',
      type: 'line',
      data,
      showSymbol: false,
      areaStyle: { color: theme.muted, opacity: 0.1 },
      lineStyle: { width: 1.25, color: theme.text },
      markArea: markAreaData.length ? { silent: true, data: markAreaData } : undefined,
    }],
  };
}

function chartValueAxis(theme, { axisLabel = {}, ...rest } = {}) {
  return {
    type: 'value',
    nameTextStyle: { color: theme.muted, fontSize: 10 },
    axisLabel: { color: theme.muted, fontSize: 10, ...axisLabel },
    splitLine: { lineStyle: { color: theme.gridline, opacity: 0.4 } },
    ...rest,
  };
}

function chartCategoryAxis(theme, data, axisLabel = {}) {
  return {
    type: 'category',
    data,
    axisLabel: { color: theme.muted, fontSize: 10, ...axisLabel },
    axisLine: { lineStyle: { color: theme.gridline } },
  };
}

function chartAxisTooltip(theme, formatter) {
  return {
    trigger: 'axis',
    backgroundColor: theme.surface,
    borderColor: theme.gridline,
    textStyle: { fontSize: 12, color: theme.text },
    formatter,
  };
}

// Shared by the terrain page's four charts: bar/line over a category axis.
function chartGrid(bottom) {
  return { top: 16, right: 24, bottom, left: 56 };
}

function esc(text) {
  return String(text).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function failure(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === 'string') return body.detail;
  } catch (e) { /* not JSON */ }
  return `HTTP ${res.status}`;
}

// {data} or {error}: a failed fetch never throws, so each panel decides
// for itself what a failure means.
async function getJson(url, init) {
  try {
    const res = await fetch(url, init);
    return res.ok ? { data: await res.json() } : { error: await failure(res) };
  } catch (e) {
    return { error: 'Network error.' };
  }
}

// Load / injury-risk charts (/api/load), shared by training.html and
// plan.html's Trends tab: acute:chronic ratio over shaded risk bands, and
// monotony with its 2.0 line. Returns the instances (for resize) and a
// one-line caption for the latest day.
const ACWR_BANDS = [
  { from: 0, to: 0.8, key: 'low', label: 'Low' },
  { from: 0.8, to: 1.3, key: 'optimal', label: 'Optimal' },
  { from: 1.3, to: 1.5, key: 'caution', label: 'Caution' },
  { from: 1.5, to: 3, key: 'high', label: 'High' },
];
const MONOTONY_HIGH = 2.0;

function renderLoadCharts(acwrEl, monotonyEl, data) {
  const theme = chartTheme();
  const good = getComputedStyle(document.documentElement).getPropertyValue('--good').trim();
  const bandColor = { low: theme.baseline, optimal: good, caution: theme.series[2], high: theme.series[5] };
  const bandLabel = Object.fromEntries(ACWR_BANDS.map(b => [b.key, b.label]));
  const dayLabel = d => new Date(d + 'T00:00:00').toLocaleString('en-US', { month: 'short', day: 'numeric' });

  function tooltip(params) {
    const d = data.days[params[0].dataIndex];
    const lines = [
      d.acwr != null ? `Ratio: ${d.acwr.toFixed(2)} (${bandLabel[d.acwr_band]})` : 'Ratio: not enough history',
      d.load_other ? `Day load: ${Math.round(d.load)} (run ${Math.round(d.load_run)} · other ${Math.round(d.load_other)})` : `Day load: ${Math.round(d.load)}`,
      `7-day load: ${Math.round(d.acute)}`,
      d.chronic_weekly != null ? `28-day weekly avg: ${Math.round(d.chronic_weekly)}` : null,
      d.monotony != null ? `Monotony: ${d.monotony.toFixed(2)}` : null,
      d.strain != null ? `Strain: ${d.strain}` : null,
    ].filter(Boolean);
    return `<div style="font-weight:600;margin-bottom:2px;color:${theme.text}">${dayLabel(d.date)}</div>${lines.join('<br>')}`;
  }

  function base(extraY, series) {
    return {
      animation: false,
      grid: { top: 12, right: 16, bottom: 28, left: 48 },
      tooltip: chartAxisTooltip(theme, tooltip),
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: theme.gridline } },
        axisTick: { show: false },
        axisLabel: { fontSize: 11, color: theme.muted },
        ...(theme.compact ? { splitNumber: 4 } : {}),
      },
      yAxis: Object.assign({
        type: 'value',
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: theme.gridline } },
        axisLabel: { fontSize: 11, color: theme.muted },
      }, extraY),
      series,
    };
  }

  const yMax = Math.ceil(Math.max(1.6, ...data.days.map(d => d.acwr ?? 0)) * 10) / 10;
  const acwrChart = echarts.init(acwrEl);
  acwrChart.setOption(base({ min: 0, max: yMax }, [{
    name: 'Acute:chronic',
    type: 'line',
    data: data.days.map(d => [d.date, d.acwr]),
    showSymbol: false,
    connectNulls: false,
    lineStyle: { color: theme.text, width: 1.75 },
    itemStyle: { color: theme.text },
    markArea: {
      silent: true,
      data: ACWR_BANDS.map(b => ([
        {
          yAxis: b.from,
          itemStyle: { color: withAlpha(bandColor[b.key], theme.isDark ? 0.18 : 0.12) },
          label: { show: true, position: 'insideRight', formatter: b.label, fontSize: 10, color: theme.muted },
        },
        { yAxis: Math.min(b.to, yMax) },
      ])),
    },
  }]));

  const monotonyChart = echarts.init(monotonyEl);
  monotonyChart.setOption(base({ min: 0 }, [{
    name: 'Monotony',
    type: 'line',
    data: data.days.map(d => [d.date, d.monotony]),
    showSymbol: false,
    connectNulls: false,
    lineStyle: { color: theme.series[4], width: 1.5 },
    itemStyle: { color: theme.series[4] },
    markLine: {
      silent: true,
      symbol: ['none', 'none'],
      data: [{ yAxis: MONOTONY_HIGH }],
      lineStyle: { color: theme.series[5], type: 'dashed', width: 1 },
      label: { formatter: String(MONOTONY_HIGH), fontSize: 10, color: theme.muted },
    },
  }]));

  const c = data.current;
  const parts = [];
  if (c && c.acwr != null) parts.push(`Today: ratio ${c.acwr.toFixed(2)} (${bandLabel[c.acwr_band].toLowerCase()})`);
  if (c && c.monotony != null) parts.push(`monotony ${c.monotony.toFixed(2)}${c.monotony > MONOTONY_HIGH ? ' (high)' : ''}`);
  if (data.estimated_runs) parts.push(`${data.estimated_runs} run${data.estimated_runs === 1 ? '' : 's'} without HR charged the median rate`);
  if (data.unscored_other) parts.push(`${data.unscored_other} non-run activit${data.unscored_other === 1 ? 'y' : 'ies'} without HR not counted`);
  return { charts: [acwrChart, monotonyChart], captionText: parts.join(' · ') };
}
