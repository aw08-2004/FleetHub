const config = document.getElementById('machine-config');
const MACHINE = config.dataset.machine;
// How much history the panels OPEN on, from hub.live_default_window_seconds. Small by
// default (a minute), because this page is watched live far more often than it is read as an
// archive -- a whole day fitted into 400 px is a flat line you have to zoom into before it
// says anything. The fallback matches the registry default, for a page served by an older
// hub that doesn't emit the attribute.
const LIVE_WINDOW_MS = (Number(config.dataset.liveWindowSeconds) || 60) * 1000;
// The temperature at which a reading is marked hot -- hub.hot_temp_threshold_c, the same
// number behind the Dashboard's "over N °C" tile. Infinity for a page served by an older
// hub, which marks nothing rather than guessing a threshold.
const HOT_THRESHOLD_C = Number(config.dataset.hotThresholdC) || Infinity;

const zoomPlugin = window['chartjs-plugin-zoom'];
if (zoomPlugin) Chart.register(zoomPlugin.default || zoomPlugin);

const rootStyles = getComputedStyle(document.documentElement);
const chartGridColor = rootStyles.getPropertyValue('--card-border').trim();

const socket = connectSocketWithStatus();
const dayPicker = document.getElementById('day-picker');
const resolutionEl = document.getElementById('resolution');
const resolutionInUseEl = document.getElementById('resolution-in-use');
const dynamicResolutionEl = document.getElementById('dynamic-resolution');
const noDataEl = document.getElementById('no-data');
const tempCard = document.getElementById('temp-card');
const uptimeLabelEl = document.getElementById('stat-uptime-label');
const uptimeValueEl = document.getElementById('stat-uptime');

// The Uptime card doubles as a "Last seen" card when the machine is offline -- a stale
// uptime is meaningless, and last-seen is what you actually want for a machine that's gone quiet.
//
// "Last seen" is written with fmtAgoInto (common.js), which marks the element so fleet-live.js
// keeps it ticking -- it used to be drawn once and say "2m ago" for as long as the page was
// open. Uptime must therefore REMOVE that mark, or the ticker would overwrite the uptime it
// just wrote with a last-seen time the moment the machine came back.
function showUptime(uptimeSeconds) {
    uptimeLabelEl.textContent = t('machine.uptime');
    delete uptimeValueEl.dataset.agoAt;
    uptimeValueEl.textContent = formatUptime(uptimeSeconds);
    uptimeValueEl.removeAttribute('title');
}

function showLastSeen(updatedAt) {
    uptimeLabelEl.textContent = t('machine.last_seen');
    fmtAgoInto(uptimeValueEl, updatedAt || null);
}

// The status pill and "Last seen X ago" beside the H1. `enrolled` is remembered from the last
// full record because a live report does not carry it, and the pill would otherwise drop
// its "not enrolled" qualifier on every reading. fmtAgoInto keeps the age ticking.
const headStatusEl = document.getElementById('machine-head-status');
const headSeenEl = document.getElementById('machine-head-seen');
const headSeenAgoEl = document.getElementById('machine-head-seen-ago');
let headEnrolled;

function showHeadStatus(status, updatedAt, enrolled) {
    if (enrolled !== undefined) headEnrolled = enrolled;
    if (!headStatusEl) return;
    setMachineStatusPill(headStatusEl, { status, enrolled: headEnrolled });
    headStatusEl.hidden = false;
    if (headSeenEl && headSeenAgoEl) {
        fmtAgoInto(headSeenAgoEl, updatedAt || null);
        headSeenEl.hidden = !updatedAt;
    }
}
const VIEWPORT_RELOAD_DEBOUNCE_MS = 250;
let viewportReloadTimer = null;
let lastHistoryRequest = null;
let historyLoadInFlight = false;
let viewingToday = true;
// Follow mode: every live reading re-anchors the visible window so the newest point sits at
// the right edge. On for today, off the moment the operator pans or zooms (they've asked to
// look somewhere specific, and yanking the axis out from under them each tick is unusable),
// and back on via Reset. Deliberately separate from `viewingToday`: a past day has no
// "latest" to follow, but you can also be on today and simply not want it.
let followLive = true;

function getLocalDateString() {
    const now = new Date();
    const local = new Date(now.getTime() - (now.getTimezoneOffset() * 60000));
    return local.toISOString().split('T')[0];
}

function formatDateForApi(date) {
    const pad = (value) => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function getDayRange(dateString) {
    const start = new Date(`${dateString}T00:00:00`);
    if (Number.isNaN(start.getTime())) return null;
    const end = new Date(start.getTime() + 24 * 60 * 60 * 1000);
    return { startMs: start.getTime(), endMs: Math.min(end.getTime(), Date.now()) };
}

// What the panels SHOW when the page opens (and what Reset returns to). Distinct from
// getDayRange, which stays the bound on where panning and zooming may reach -- the whole day
// is one pan away, it just isn't what you land on. A past day has no live edge to sit at, so
// it opens on the whole day exactly as before.
function getInitialRange(dateString) {
    const day = getBoundRange(dateString);
    if (!day) return null;
    if (dateString !== getLocalDateString()) return day;
    return { startMs: Math.max(day.startMs, day.endMs - openWindowMs), endMs: day.endMs };
}

const DAY_MS = 24 * 60 * 60 * 1000;

// Whether the chosen range reaches past today's midnight -- the 7 d and 30 d pills. Those
// requests go out WITHOUT `date` (the server clamps a dated request to that one day) and
// their pan bound is the whole window rather than the calendar day.
function isMultiDay() {
    return openWindowMs > DAY_MS && dayPicker.value === getLocalDateString();
}

// Where panning and zooming may reach: the calendar day, or for a multi-day range the last
// 7 or 30 days up to now. getDayRange stays the one-day answer the day picker means.
function getBoundRange(dateString) {
    if (!(openWindowMs > DAY_MS && dateString === getLocalDateString())) {
        return getDayRange(dateString);
    }
    const now = Date.now();
    return { startMs: now - openWindowMs, endMs: now };
}

// The window the cards open on and follow, set by the range pills (hub 1.189.0): Live (the
// hub's live window, a minute by default), 1 h, 6 h, 24 h, 7 d or 30 d -- all "the last N up
// to now". A past day always shows the whole day, so picking one lights 24 h, and a pill
// pressed while on a past day jumps back to today: "the last hour" of a day that is over
// has no meaning.
let openWindowMs = LIVE_WINDOW_MS;

function chooseResolutionForSpan(spanMs) {
    if (spanMs <= 45 * 60 * 1000) return 'raw';
    if (spanMs <= 6 * 60 * 60 * 1000) return '10s';
    if (spanMs <= 18 * 60 * 60 * 1000) return '1m';
    // About 700-900 points per metric at every step below: 3 days of 5m, 8 of 15m, 30 of
    // 1h. 15m and 1h are new with the 7 d and 30 d ranges (app.py VALID_RESOLUTIONS).
    if (spanMs <= 3 * DAY_MS) return '5m';
    if (spanMs <= 8 * DAY_MS) return '15m';
    return '1h';
}

function syncResolutionControl() {
    resolutionEl.disabled = dynamicResolutionEl.checked;
}

function getSelectedResolution(spanMs) {
    if (dynamicResolutionEl.checked) {
        const resolved = chooseResolutionForSpan(spanMs);
        resolutionEl.value = resolved;
        return resolved;
    }
    return (resolutionEl.value || '5m').trim().toLowerCase();
}

function setResolutionInUse(resolution) {
    resolutionInUseEl.textContent = t('history.in_use',
        { value: resolution || t('history.resolution_label.unknown') });
}

function toChartTimestamp(value) {
    if (typeof value === 'number' && Number.isFinite(value)) {
        return value > 1e12 ? value : value * 1000;
    }
    if (typeof value === 'string' && value.trim()) {
        const normalized = value.includes('T') ? value : value.replace(' ', 'T');
        const parsed = Date.parse(normalized);
        return Number.isNaN(parsed) ? null : parsed;
    }
    return null;
}

function buildHistoryUrl(date, minMs, maxMs, resolution) {
    const params = new URLSearchParams();
    // A dated request is clamped to that day on the server; a 7 d or 30 d window must not be.
    if (!isMultiDay()) params.set('date', date);
    params.set('from', formatDateForApi(new Date(minMs)));
    params.set('to', formatDateForApi(new Date(maxMs)));
    params.set('resolution', resolution);
    params.set('limit', 'all');
    // One request returns every panel's series: { metrics: { key: [{x, y}], ... } }.
    return `/api/machines/${encodeURIComponent(MACHINE)}/history?${params.toString()}`;
}

function scheduleViewportReload() {
    if (!dynamicResolutionEl.checked || !selectedDayRange) return;
    if (viewportReloadTimer !== null) clearTimeout(viewportReloadTimer);
    viewportReloadTimer = setTimeout(() => {
        loadVisibleViewport();
        viewportReloadTimer = null;
    }, VIEWPORT_RELOAD_DEBOUNCE_MS);
}

// ---- Historical multi-panel dashboard ----------------------------------------
// One Chart.js line panel per metric, Komodo-style. METRICS is the single source of truth
// for which panels exist and how each looks; a panel renders only when its collection
// toggle (data-enabled-metrics, from settings.py's metrics.* knobs) is on. `diag` maps a
// metric to its key in the live `diagnostics` payload, for real-time appends over the socket.
const ENABLED_METRICS = (() => {
    try { return JSON.parse(config.dataset.enabledMetrics || '{}'); }
    catch (e) { return {}; }
})();

// `rate: true` marks a metric stored in bytes per second. Those panels label their axis
// and tooltip through formatRate() instead of pinning a fixed "B/s", so a 400 MB/s NVMe
// and a 2 KB/s idle NIC are both readable -- at a fixed B/s the former is an unreadable
// nine-digit tick and the latter is a flat line at the bottom of the axis.
// `bits: true` additionally renders that rate in bits, which is the unit network hardware
// is specified and sold in. Network only -- see BIT_UNITS.
// A rate metric's `unit` never reaches the screen: panelConfig() replaces the axis title
// with t('machine.per_second') and the tooltip returns formatRate(), both of which carry
// the unit per-tick instead. It stays in the table as the row's own documentation of what
// it charts -- which is exactly why the network rows say 'b/s' and the disk rows 'B/s'.
// `label` is a function, not a string: this table is built at module load, and a panel
// title has to be the operator's language rather than whatever the file was written in.
// One literal key per metric, so the key scan in tests/test_i18n.py can see them all.
const METRICS = [
    { key: 'cpu_load',   label: () => t('machine.metric.cpu_load'),   unit: '%',   color: '#10b981', max: 100, diag: 'cpu_load_pct' },
    { key: 'memory',     label: () => t('machine.metric.memory'),     unit: '%',   color: '#f59e0b', max: 100, diag: 'memory_load_pct' },
    { key: 'disk',       label: () => t('machine.metric.disk'),       unit: '%',   color: '#3b82f6', max: 100, diag: 'disk_load_pct' },
    { key: 'net_rx',     label: () => t('machine.metric.net_rx'),     unit: 'b/s', color: '#22d3ee', rate: true, bits: true, diag: 'net_rx_bps' },
    { key: 'net_tx',     label: () => t('machine.metric.net_tx'),     unit: 'b/s', color: '#ec4899', rate: true, bits: true, diag: 'net_tx_bps' },
    { key: 'disk_read',  label: () => t('machine.metric.disk_read'),  unit: 'B/s', color: '#14b8a6', rate: true, diag: 'disk_read_bps' },
    { key: 'disk_write', label: () => t('machine.metric.disk_write'), unit: 'B/s', color: '#f43f5e', rate: true, diag: 'disk_write_bps' },
    { key: 'gpu_temp',   label: () => t('machine.metric.gpu_temp'),   unit: '°C',  color: '#8b5cf6', diag: 'gpu_temp' },
    { key: 'gpu_load',   label: () => t('machine.metric.gpu_load'),   unit: '%',   color: '#a855f7', max: 100, diag: 'gpu_load_pct' },
    // `decimals: 0` -- a fan reports whole revolutions per minute, and "1200.0 RPM" reads as
    // precision the sensor doesn't have. Everything else keeps the default single decimal.
    { key: 'fan_rpm',    label: () => t('machine.metric.fan_rpm'),    unit: 'RPM', color: '#38bdf8', decimals: 0, diag: 'fan_rpm' },
    { key: 'cpu_power',  label: () => t('machine.metric.cpu_power'),  unit: 'W',   color: '#eab308', diag: 'cpu_power_w' },
    { key: 'gpu_power',  label: () => t('machine.metric.gpu_power'),  unit: 'W',   color: '#c084fc', diag: 'gpu_power_w' },
    { key: 'battery',    label: () => t('machine.metric.battery'),    unit: '%',   color: '#84cc16', max: 100, diag: 'battery_pct' },
    { key: 'temp',       label: () => t('machine.metric.temp'),       unit: '°C',  color: '#f97316' },
];

function metricLabel(metric) {
    return typeof metric.label === 'function' ? metric.label() : String(metric.label);
}

// A reading as a number, or NaN when there is no reading. **Use this, never bare Number(),
// on anything the server may send as null**: Number(null) is 0, and 0 passes
// Number.isFinite -- so an absent fan duty rendered as "Driven at 0%" with an empty bar
// beside a fan at 19,999 RPM, and a metric the machine never sent plotted a flat 0% line
// on its live chart while hiding the panel's "no data" note. Booleans and empty strings
// are absent too, for the same reason.
function toNumber(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') {
        return NaN;
    }
    return Number(value);
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];
// Network throughput is quoted in BITS everywhere an operator would check it against
// something: the NIC is a 1 Gb/s card, the switch port is 100 Mb/s, the ISP sells 500 Mb/s.
// A chart labelled MB/s is the same reading off by 8x from all of them. Disk deliberately
// stays in bytes for the mirror-image reason -- drives are specified in MB/s.
const BIT_UNITS = ['b', 'Kb', 'Mb', 'Gb', 'Tb'];

function scaleBytes(bytes, base, units = BYTE_UNITS) {
    let value = Math.abs(Number(bytes));
    let step = 0;
    while (value >= base && step < units.length - 1) { value /= base; step += 1; }
    return { value: Number(bytes) < 0 ? -value : value, unit: units[step] };
}

// Throughput scales in 1000s, capacity in 1024s -- deliberately different, because each
// matches what the operator is comparing against. Chart.js picks round tick values in raw
// bytes (400000, 800000...), so a binary axis would label them 390.6 KB/s and 781.3 KB/s;
// decimal makes those gridlines land on 400 KB/s and 800 KB/s, and it is what network gear
// reports anyway. Disk capacity stays binary so "476 GB" matches what Explorer shows for
// the same drive.
//
// `asBits` converts the same stored bytes/s to bits for display. Presentation only -- the
// agent reports bytes, the database stores bytes, and rule thresholds on metric.net_*_bps
// stay in bytes. Only the two network panels pass it.
function formatRate(bytesPerSecond, asBits) {
    if (!Number.isFinite(toNumber(bytesPerSecond))) return '--';
    const units = asBits ? BIT_UNITS : BYTE_UNITS;
    const scaled = asBits ? Number(bytesPerSecond) * 8 : Number(bytesPerSecond);
    const { value, unit } = scaleBytes(scaled, 1000, units);
    // Whole numbers below 1 KB/s: a "0.0 B/s" axis tick reads as broken. One decimal above
    // -- enough to tell 1.4 from 1.9 MB/s without noise. Round values keep their integer
    // form, so an 800 KB/s gridline is labelled "800 KB/s", not "800.0 KB/s".
    if (unit === units[0] || Number.isInteger(value)) return `${Math.round(value)} ${unit}/s`;
    return `${value.toFixed(1)} ${unit}/s`;
}

// Absolute size (GB in, human units out) for the Storage cards. A decimal below 100 only
// -- "412.0 GB" is false precision next to a number an operator reads as "about 400".
function formatGb(gb) {
    if (!Number.isFinite(toNumber(gb))) return '--';
    const { value, unit } = scaleBytes(Number(gb) * 1024 * 1024 * 1024, 1024);
    return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

const gridEl = document.getElementById('metric-grid');
const panels = [];              // { metric, chart, emptyEl, titleEl }
let selectedDayRange = null;
let syncingXRange = false;      // guards the cross-panel zoom/pan mirroring below
// Total physical RAM (GB) for this machine -- a constant we learn from the latest
// diagnostics. Lets the Memory panel say what 100% is and convert a % point to GB on hover.
let memTotalGb = null;

function formatMemTooltip(pct) {
    if (!Number.isFinite(memTotalGb)) return t('machine.percent', { value: pct.toFixed(1) });
    const usedGb = (pct / 100) * memTotalGb;
    return t('machine.memory_tooltip', {
        used: usedGb.toFixed(1), total: memTotalGb.toFixed(0), percent: pct.toFixed(0),
    });
}

// Reflect the machine's total RAM into the Memory panel: title becomes "Memory (16 GB)"
// so 100% is unambiguous, and the tooltip (via memTotalGb) starts reporting GB.
function updateMemoryTotal(totalGb) {
    if (!Number.isFinite(totalGb)) return;
    memTotalGb = totalGb;
    const panel = panels.find((p) => p.metric.key === 'memory');
    if (panel && panel.titleEl) {
        panel.titleEl.textContent = t('machine.memory_with_total',
                                      { total: totalGb.toFixed(0) });
    }
}

function metricEnabled(metric) {
    return ENABLED_METRICS[metric.key] !== false;   // default on for unknown keys
}

function panelConfig(metric) {
    const yScale = { title: { display: true, text: metric.unit }, grid: { color: chartGridColor } };
    if (metric.max !== undefined) { yScale.min = 0; yScale.max = metric.max; }
    if (metric.rate) {
        // Each tick scales on its own value, so the axis stays readable whatever range the
        // zoom lands on. The axis title drops the unit -- it now lives on every tick.
        yScale.min = 0;
        yScale.title.text = t('machine.per_second');
        yScale.ticks = { callback: (value) => formatRate(value, metric.bits) };
    }
    return {
        type: 'line',
        data: {
            datasets: [{
                label: metricLabel(metric), data: [], parsing: false,
                // The soft area under the line (variant M1): the metric's colour at ~15%
                // alpha, filled to the axis. `origin` rather than `start` so a rate panel
                // whose axis is pinned at 0 fills to 0 and not to its lowest point.
                borderColor: metric.color, backgroundColor: `${metric.color}26`,
                fill: 'origin',
                borderWidth: 2, tension: 0.25, pointRadius: 0,
                pointHoverRadius: 6, pointHitRadius: 20,
            }],
        },
        options: {
            responsive: true, maintainAspectRatio: false, normalized: true,
            animation: { duration: 0 },
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                // At most six horizontal ticks, in the tooltip's own 24-hour format. Chart.js's
                // defaults drew ~15 labels per panel rotated 45 degrees, in the adapter's
                // 12-hour "5:33:01 p.m." -- a grey hatch under every chart, and a different
                // clock from the tooltip above it. Seconds only appear once zoomed in far
                // enough for the ticks to be seconds apart.
                // Compact by default: a card's chart is a shape, not a reading -- the value,
                // the minimum and the peak are printed above and below it. setPanelExpanded()
                // turns both axes back on for the full-size chart.
                x: {
                    type: 'time',
                    display: false,
                    time: {
                        tooltipFormat: 'HH:mm:ss',
                        displayFormats: { second: 'HH:mm:ss', minute: 'HH:mm', hour: 'HH:mm', day: 'MMM d' },
                    },
                    ticks: { maxTicksLimit: 6, maxRotation: 0, autoSkip: true },
                    grid: { color: chartGridColor },
                },
                y: Object.assign(yScale, { display: false }),
            },
            plugins: {
                decimation: { enabled: true, algorithm: 'lttb', samples: 400 },
                legend: { display: false },
                peakMarker: { color: metric.color },
                tooltip: {
                    mode: 'index', intersect: false,
                    callbacks: {
                        label: (ctx) => {
                            if (metric.key === 'memory') return formatMemTooltip(ctx.parsed.y);
                            if (metric.rate) return formatRate(ctx.parsed.y, metric.bits);
                            return `${ctx.parsed.y.toFixed(metric.decimals ?? 1)} ${metric.unit}`;
                        },
                    },
                },
                zoom: {
                    pan: { enabled: true, mode: 'x' },
                    zoom: {
                        wheel: { enabled: true }, pinch: { enabled: true },
                        drag: { enabled: true, backgroundColor: 'rgba(34, 197, 94, 0.15)' },
                        mode: 'x',
                    },
                    onZoomComplete: ({ chart }) => onPanelRangeChanged(chart),
                    onPanComplete: ({ chart }) => onPanelRangeChanged(chart),
                },
            },
        },
    };
}

// The peak dot: drawn by a tiny plugin rather than as a styled data point, because the
// decimation plugin hands Chart.js a resampled array whose indexes are not the data's -- a
// scriptable pointRadius keyed on "the index of the max" marked a different point on every
// zoom. The peak itself is computed by updatePanelStats() from the real data and stored on
// the chart, so the dot and the "peak" caption cannot disagree.
const peakMarkerPlugin = {
    id: 'peakMarker',
    afterDatasetsDraw(chart, args, options) {
        const peak = chart.$peak;
        if (!peak) return;
        const xs = chart.scales.x;
        const ys = chart.scales.y;
        if (!xs || !ys || peak.x < xs.min || peak.x > xs.max) return;
        const { ctx } = chart;
        ctx.save();
        ctx.beginPath();
        ctx.arc(xs.getPixelForValue(peak.x), ys.getPixelForValue(peak.y), 3.5, 0, 2 * Math.PI);
        ctx.fillStyle = options.color || '#888';
        ctx.fill();
        ctx.restore();
    },
};
Chart.register(peakMarkerPlugin);

// One reading in the metric's own unit, the way the tooltip already wrote it.
function formatMetricValue(metric, y) {
    if (!Number.isFinite(y)) return t('machine.unknown');
    if (metric.rate) return formatRate(y, metric.bits);
    return `${y.toFixed(metric.decimals ?? 1)} ${metric.unit}`;
}

// The metrics where a HIGHER number is the worse one. Their badge goes amber above the
// average and green below it; every other metric (throughput, battery, power, fan speed)
// gets a neutral badge, because "more network than usual" is not good or bad by itself.
const WORSE_WHEN_HIGH = new Set(['temp', 'gpu_temp', 'cpu_load', 'memory', 'gpu_load', 'disk']);

// The visible part of a series in one pass: count, sum, the lowest and highest point. The
// axis bounds `lo`/`hi` are +/-Infinity when the chart has none (a past day shown whole).
function summarizeVisible(points, lo, hi) {
    const out = { n: 0, sum: 0, min: null, peak: null };
    for (const pt of points) {
        if (pt.x < lo || pt.x > hi || !Number.isFinite(pt.y)) continue;
        out.sum += pt.y;
        out.n += 1;
        if (!out.min || pt.y < out.min.y) out.min = pt;
        if (!out.peak || pt.y > out.peak.y) out.peak = pt;
    }
    return out;
}

// The badge's modifier: neutral for a metric where higher is not worse, else up or down.
function deltaModifier(metric, delta) {
    if (!WORSE_WHEN_HIGH.has(metric.key)) return 'metric-card__delta--flat';
    return delta > 0 ? 'metric-card__delta--up' : 'metric-card__delta--down';
}

// The latest reading against the visible average. Hidden with fewer than two points: one
// reading has no average to differ from.
function renderDelta(panel, latest, summary) {
    const el = panel.deltaEl;
    el.hidden = !(latest && summary.n > 1);
    if (el.hidden) return;
    const metric = panel.metric;
    const delta = latest.y - summary.sum / summary.n;
    const span = summary.peak.y - summary.min.y;
    el.className = 'metric-card__delta';
    // "Steady" when the difference is under 5% of the visible spread -- a 0.1 °C wobble on
    // a flat line badged "+0.1 vs avg" in amber was noise dressed as news.
    if (span === 0 || Math.abs(delta) < span * 0.05) {
        el.textContent = t('machine.history.steady');
        el.classList.add('metric-card__delta--flat');
        return;
    }
    const sign = delta > 0 ? '+' : '−';
    const magnitude = metric.rate
        ? formatRate(Math.abs(delta), metric.bits)
        : `${Math.abs(delta).toFixed(metric.decimals ?? 1)} ${metric.unit}`;
    el.textContent = t('machine.history.vs_avg', { delta: sign + magnitude });
    el.classList.add(deltaModifier(metric, delta));
}

// When the peak was. 24-hour, like the charts' own axis and tooltip (HH:mm) -- the locale
// default put "09:23 PM" under a chart whose axis said 21:23. With the date too once the
// visible range is longer than a day: "peak at 07:55" over 30 days names no day.
function peakTimeLabel(peakMs, visibleSpanMs) {
    const parts = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
    const withDay = visibleSpanMs > DAY_MS ? { ...parts, month: 'short', day: 'numeric' } : parts;
    return new Date(peakMs).toLocaleString([], withDay);
}

// The numbers around a card's chart, all over what is ON SCREEN: the latest reading, its
// difference from the visible average, and the visible minimum and peak (with its time).
// "Visible" rather than "the whole day" so a zoom into a spike re-captions the spike.
// Split into the helpers above in hub 1.189.0's review, when SonarCloud measured this one
// function at three times its complexity limit.
function updatePanelStats(panel) {
    const ds = panel.chart.data.datasets[0];
    const all = ds._data || ds.data;   // the decimation plugin keeps the originals in _data
    const opts = panel.chart.options.scales.x;
    const lo = Number.isFinite(opts.min) ? opts.min : -Infinity;
    const hi = Number.isFinite(opts.max) ? opts.max : Infinity;
    const summary = summarizeVisible(all, lo, hi);
    const latest = all.length ? all[all.length - 1] : null;
    const metric = panel.metric;
    // Memory's headline stays a percentage: the card title already says how much 100% is
    // ("Memory (16 GB)"), and the GB form belongs to the hover tooltip.
    panel.valueEl.textContent = latest ? formatMetricValue(metric, latest.y) : t('machine.unknown');
    renderDelta(panel, latest, summary);

    const { min, peak } = summary;
    panel.chart.$peak = peak && min && peak.y > min.y ? peak : null;
    if (!min || !peak) {
        panel.rangeEl.textContent = '';
        return;
    }
    // The axis bounds when the chart has them; otherwise what the data covers.
    let visibleSpan = hi - lo;
    if (!Number.isFinite(visibleSpan)) visibleSpan = all[all.length - 1].x - all[0].x;
    panel.rangeEl.textContent = t('machine.history.min_peak', {
        min: formatMetricValue(metric, min.y),
        peak: formatMetricValue(metric, peak.y),
        time: peakTimeLabel(peak.x, visibleSpan),
    });
}

// Live readings arrive once a second per panel; the captions only need to keep up with a
// person reading them, so they are recomputed at most once a second for the whole grid.
let statsTimer = null;
function scheduleStats() {
    if (statsTimer !== null) return;
    statsTimer = setTimeout(() => {
        statsTimer = null;
        for (const p of panels) updatePanelStats(p);
    }, 1000);
}

// A card's chart at full size, with both axes -- the old panel, for reading actual values
// off the gridlines. Spans the whole grid row so it is as wide as the old two-up panel was.
function setPanelExpanded(panel, expanded) {
    panel.container.classList.toggle('metric-card--expanded', expanded);
    panel.expandBtn.setAttribute('aria-pressed', expanded ? 'true' : 'false');
    const label = expanded ? t('machine.history.shrink') : t('machine.history.expand');
    panel.expandBtn.title = label;
    panel.expandBtn.setAttribute('aria-label', label);
    panel.chart.options.scales.x.display = expanded;
    panel.chart.options.scales.y.display = expanded;
    panel.chart.resize();
    panel.chart.update('none');
}

function buildPanels() {
    gridEl.replaceChildren();
    panels.length = 0;
    for (const metric of METRICS) {
        if (!metricEnabled(metric)) continue;

        // A metric CARD (hub 1.189.0, variant M1): label and expand button, the latest value
        // with its badge, the chart, then the visible minimum and peak. Still one card per
        // enabled metric -- all fourteen when the machine reports them -- built from METRICS.
        const container = document.createElement('div');
        container.className = 'metric-panel metric-card';

        const head = document.createElement('div');
        head.className = 'metric-panel__head';
        const title = document.createElement('span');
        title.className = 'metric-panel__title';
        title.textContent = metricLabel(metric);
        const expandBtn = document.createElement('button');
        expandBtn.type = 'button';
        expandBtn.className = 'metric-card__expand';
        expandBtn.setAttribute('aria-pressed', 'false');
        expandBtn.title = t('machine.history.expand');
        expandBtn.setAttribute('aria-label', t('machine.history.expand'));
        expandBtn.textContent = '\u2922';
        head.append(title, expandBtn);
        container.appendChild(head);

        const valueRow = document.createElement('div');
        valueRow.className = 'metric-card__value-row';
        const valueEl = document.createElement('span');
        valueEl.className = 'metric-card__value';
        valueEl.textContent = t('machine.unknown');
        const deltaEl = document.createElement('span');
        deltaEl.className = 'metric-card__delta';
        deltaEl.hidden = true;
        valueRow.append(valueEl, deltaEl);
        container.appendChild(valueRow);

        const chartBox = document.createElement('div');
        chartBox.className = 'metric-panel__chart';
        const canvas = document.createElement('canvas');
        chartBox.appendChild(canvas);
        container.appendChild(chartBox);

        const rangeEl = document.createElement('div');
        rangeEl.className = 'stat-card__meta metric-card__range';
        container.appendChild(rangeEl);

        const emptyEl = document.createElement('div');
        emptyEl.className = 'stat-card__meta metric-panel__empty';
        emptyEl.textContent = t('machine.no_data_range');
        emptyEl.style.display = 'none';
        container.appendChild(emptyEl);

        gridEl.appendChild(container);
        const chart = new Chart(canvas.getContext('2d'), panelConfig(metric));
        const panel = { metric, chart, emptyEl, titleEl: title, container,
                        valueEl, deltaEl, rangeEl, expandBtn };
        expandBtn.addEventListener('click', () => setPanelExpanded(
            panel, !container.classList.contains('metric-card--expanded')));
        panels.push(panel);
    }
    // If diagnostics already told us the RAM size before the panels existed, apply it now.
    if (Number.isFinite(memTotalGb)) updateMemoryTotal(memTotalGb);
}

// Mirror one panel's zoom/pan onto every other panel so the whole grid shares a time axis,
// then (in dynamic mode) reload the visible window at an appropriate resolution.
function onPanelRangeChanged(sourceChart) {
    if (syncingXRange) return;
    const xs = sourceChart.scales?.x;
    if (!xs || !Number.isFinite(Number(xs.min)) || !Number.isFinite(Number(xs.max))) return;
    // The operator has chosen a range by hand; stop dragging the axis back to the live edge
    // under them. Reset is how they say they're done looking.
    followLive = false;
    applyXRangeToAll(Number(xs.min), Number(xs.max), sourceChart);
    sourceChart.options.scales.x.min = Number(xs.min);
    sourceChart.options.scales.x.max = Number(xs.max);
    for (const p of panels) updatePanelStats(p);
    scheduleViewportReload();
}

function applyXRangeToAll(minMs, maxMs, exceptChart) {
    syncingXRange = true;
    try {
        for (const p of panels) {
            if (p.chart === exceptChart) continue;
            p.chart.options.scales.x.min = minMs;
            p.chart.options.scales.x.max = maxMs;
            p.chart.update('none');
        }
    } finally {
        syncingXRange = false;
    }
}

function applyRange(chart, minMs, maxMs, resetZoom) {
    if (resetZoom) {
        chart.options.scales.x.min = undefined;
        chart.options.scales.x.max = undefined;
        chart.update('none');
        if (typeof chart.resetZoom === 'function') chart.resetZoom();
    } else {
        chart.options.scales.x.min = minMs;
        chart.options.scales.x.max = maxMs;
        chart.update('none');
    }
}

// The newest load asked for while another was running. Dropping it was harmless while only
// scroll-zooms called this (the next wheel tick asked again), but a range pill pressed during
// the page's opening load was silently lost -- the 6 h pill lit, the cards kept showing the
// live minute. Found in the preview. Only the latest request is kept: it supersedes the rest.
let pendingHistoryLoad = null;

async function loadHistoryRange(minMs, maxMs, resolution, resetZoom) {
    if (!dayPicker.value || panels.length === 0) return;
    if (historyLoadInFlight) {
        pendingHistoryLoad = [minMs, maxMs, resolution, resetZoom];
        return;
    }
    historyLoadInFlight = true;
    try {
        const historyRes = await fetch(buildHistoryUrl(dayPicker.value, minMs, maxMs, resolution));
        const body = await historyRes.json();
        const series = (body && body.metrics) || {};
        let anyData = false;
        for (const p of panels) {
            const points = (series[p.metric.key] || [])
                .map((point) => {
                    const x = toChartTimestamp(point.x ?? point.timestamp ?? point.ts_text);
                    const y = toNumber(point.y);
                    if (x === null || !Number.isFinite(y)) return null;
                    return { x, y };
                })
                .filter(Boolean);
            p.chart.data.datasets[0].data = points;
            p.emptyEl.style.display = points.length ? 'none' : 'block';
            if (points.length) anyData = true;
            setPanelVisible(p, panelHasSubject(p, points.length));
            applyRange(p.chart, minMs, maxMs, resetZoom);
            updatePanelStats(p);
        }
        noDataEl.style.display = anyData ? 'none' : 'block';
        setResolutionInUse(resolution);
        lastHistoryRequest = { minMs, maxMs, resolution };
    } finally {
        historyLoadInFlight = false;
        if (pendingHistoryLoad) {
            const next = pendingHistoryLoad;
            pendingHistoryLoad = null;
            // Not awaited: this finally must not wait on the next load. A failed fetch
            // leaves the last good series on the cards, as it always has.
            void loadHistoryRange(...next);
        }
    }
}

async function loadVisibleViewport() {
    if (!selectedDayRange || historyLoadInFlight || panels.length === 0) return;
    const xScale = panels[0].chart.scales?.x;
    if (!xScale) return;
    const scaleMin = Number(xScale.min);
    const scaleMax = Number(xScale.max);
    if (!Number.isFinite(scaleMin) || !Number.isFinite(scaleMax)) return;
    const minMs = Math.max(selectedDayRange.startMs, Math.floor(scaleMin));
    const maxMs = Math.min(selectedDayRange.endMs, Math.ceil(scaleMax));
    if (maxMs <= minMs) return;
    const resolution = getSelectedResolution(maxMs - minMs);
    if (
        lastHistoryRequest &&
        lastHistoryRequest.resolution === resolution &&
        Math.abs(lastHistoryRequest.minMs - minMs) < 10000 &&
        Math.abs(lastHistoryRequest.maxMs - maxMs) < 10000
    ) {
        return;
    }
    await loadHistoryRange(minMs, maxMs, resolution, false);
}

async function loadSelectedDay() {
    const date = dayPicker.value;
    if (!date) return;
    const range = getBoundRange(date);
    if (!range) return;
    viewingToday = date === getLocalDateString();
    // The day stays the PAN BOUND; only the opening view narrows. loadVisibleViewport clamps
    // to selectedDayRange, so panning left off the initial minute still fetches the rest of
    // the day rather than hitting an invisible wall at the window edge.
    selectedDayRange = range;
    await loadInitialRange(date);
}

// Load (or reload) the opening view for `date`. Follow mode is re-armed here rather than at
// the call sites, so "open the page", "pick a day" and "Reset" cannot drift apart.
async function loadInitialRange(date) {
    const view = getInitialRange(date);
    if (!view) return;
    followLive = date === getLocalDateString();
    lastHistoryRequest = null;
    // resetZoom only when we are NOT following: follow mode needs an explicit x min/max to
    // anchor the window, and resetZoom's job is to throw exactly those away.
    await loadHistoryRange(
        view.startMs,
        view.endMs,
        getSelectedResolution(view.endMs - view.startMs),
        !followLive
    );
}

// Set here as well as in onPanelRangeChanged: the plugin's onZoomComplete is documented for
// user gestures, and a button that zooms while the next live reading yanks the axis back is
// worse than one that doesn't zoom at all.
document.getElementById('zoom-in').addEventListener('click', () => {
    followLive = false;
    for (const p of panels) if (typeof p.chart.zoom === 'function') p.chart.zoom(1.2);
    scheduleViewportReload();
});
document.getElementById('zoom-out').addEventListener('click', () => {
    followLive = false;
    for (const p of panels) if (typeof p.chart.zoom === 'function') p.chart.zoom(0.8);
    scheduleViewportReload();
});
document.getElementById('reset-zoom').addEventListener('click', () => {
    if (!selectedDayRange || !dayPicker.value) return;
    // Back to the opening view, not to the whole day: on today that re-arms follow mode,
    // which is what an operator who has finished inspecting a spike actually wants back.
    loadInitialRange(dayPicker.value);
});
dayPicker.addEventListener('change', () => {
    if (dayPicker.value !== getLocalDateString()) setActiveRange(24 * 60 * 60 * 1000);
    void loadSelectedDay();
});

const rangePills = Array.from(document.querySelectorAll('#history-range [data-window]'));
function setActiveRange(windowMs) {
    openWindowMs = windowMs;
    for (const pill of rangePills) {
        const on = pillWindowMs(pill) === windowMs;
        pill.classList.toggle('seg__item--on', on);
        pill.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
}
// `live` is the hub's own live window, not a fixed minute, so the pill and the page's
// opening view are always the same thing.
function pillWindowMs(pill) {
    return pill.dataset.window === 'live' ? LIVE_WINDOW_MS : Number(pill.dataset.window) * 1000;
}
for (const pill of rangePills) {
    pill.addEventListener('click', () => {
        setActiveRange(pillWindowMs(pill));
        dayPicker.value = getLocalDateString();
        // Through loadSelectedDay, not loadInitialRange: the pan bound itself changes when a
        // range crosses midnight (7 d, 30 d) or comes back inside it.
        void loadSelectedDay();
    });
}

// The range currently on screen, clamped to the selected day. Both resolution controls reload
// through this rather than through selectedDayRange: re-fetching the whole day would throw
// away whatever the operator had navigated to (including the opening live window) as a side
// effect of changing how finely it is sampled.
function currentViewRange() {
    if (!selectedDayRange) return null;
    const xs = panels[0]?.chart.scales?.x;
    const min = Number(xs?.min);
    const max = Number(xs?.max);
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return selectedDayRange;
    return {
        startMs: Math.max(selectedDayRange.startMs, Math.floor(min)),
        endMs: Math.min(selectedDayRange.endMs, Math.ceil(max)),
    };
}

function reloadAtCurrentView() {
    const view = currentViewRange();
    if (!view) return;
    lastHistoryRequest = null;
    loadHistoryRange(view.startMs, view.endMs,
                     getSelectedResolution(view.endMs - view.startMs), false);
}

resolutionEl.addEventListener('change', () => {
    if (dynamicResolutionEl.checked) return;
    reloadAtCurrentView();
});
dynamicResolutionEl.addEventListener('change', () => {
    syncResolutionControl();
    reloadAtCurrentView();
});
gridEl.addEventListener('wheel', () => {
    scheduleViewportReload();
}, { passive: true });

let lastCpuLoadPct = null;

function applyTemp(temp) {
    if (temp === undefined || temp === null) return;
    // 0 and below is "could not read" -- the hub's own rule (displayable_temp), applied
    // here too because the live socket carries the raw value. Showing "-12.0 °C" as the
    // headline contradicted the sensor picker below it, which already said no CPU sensor
    // was reporting.
    const value = toNumber(temp);
    document.getElementById('stat-temp').textContent = value > 0
        ? t('machine.temp_c', { value: value.toFixed(1) })
        : t('machine.temp_c', { value: t('machine.unknown') });
    tempCard.classList.toggle('stat-card--high-temp', value >= HOT_THRESHOLD_C);
    // The ring is drawn against a fixed 0-100 °C scale, not against the hot threshold: a
    // ring that is full at 80 °C on one hub and at 95 °C on another would make the same
    // reading look different from console to console. The threshold is the tile's caption.
    setGauge('gauge-temp', value > 0 ? value : null);
}

// ---- Bento gauges and meters --------------------------------------------------------
// A ring is one SVG circle whose dash is the fraction; a meter is one span whose width is.
// Both take a 0-100 value and clamp it, and both go EMPTY rather than vanishing on a
// missing reading -- the number beside them already says "--", and a tile that changes
// shape between readings is the row-jumping this layout exists to stop.
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * 26;   // r="26" in machine.html

function clampPct(value) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 0;
    return Math.max(0, Math.min(100, value));
}

function setGauge(id, pct) {
    const arc = document.getElementById(id);
    if (!arc) return;
    const len = GAUGE_CIRCUMFERENCE * clampPct(pct) / 100;
    arc.style.strokeDasharray = `${len} ${GAUGE_CIRCUMFERENCE}`;
    // A zero-length dash still draws its round cap, which left a dot at twelve o'clock on
    // a machine with no reading -- a "0 %" that was never measured.
    arc.style.visibility = len > 0 ? '' : 'hidden';
}

function setMeter(id, pct) {
    const fill = document.getElementById(id);
    if (fill) fill.style.width = `${clampPct(pct)}%`;
}

function formatMetric(value, suffix) {
    return typeof value === 'number' && Number.isFinite(value)
        ? `${value.toFixed(1)} ${suffix}` : t('machine.unknown');
}

// Clock speeds in GHz from 1000 MHz up -- "3412.7 MHz" is five significant figures for a
// number an operator reads as "about 3.4 GHz", and it is the unit Task Manager shows.
function formatClock(mhz) {
    if (typeof mhz !== 'number' || !Number.isFinite(mhz)) return t('machine.unknown');
    return mhz >= 1000 ? `${(mhz / 1000).toFixed(2)} GHz` : `${Math.round(mhz)} MHz`;
}

// ---- Storage cards ------------------------------------------------------------
// One tile per volume: fill bar, % occupied, and used/total. Rebuilt on every reading
// rather than patched in place -- a USB-attached fixed disk or a newly mounted volume
// changes the LIST, and diffing three text nodes is not worth the state it would need.
const diskGridEl = document.getElementById('disk-grid');
const diskEmptyEl = document.getElementById('disk-empty');

// Thresholds an operator acts on: amber at 80% (worth planning for), red at 90% (Windows
// itself starts complaining, and updates begin failing).
function diskFillClass(pct) {
    if (pct >= 90) return 'disk-tile__fill--danger';
    if (pct >= 80) return 'disk-tile__fill--warn';
    return '';
}

function renderDisks(disks) {
    if (!diskGridEl) return;
    const list = Array.isArray(disks) ? disks : [];
    // An empty list from a machine that IS reporting sensors means "no disks seen", but an
    // absent key means an older hub -- either way the previous tiles are stale, so clear.
    diskGridEl.replaceChildren();
    diskEmptyEl.style.display = list.length ? 'none' : 'block';

    for (const disk of list) {
        const pct = toNumber(disk.used_pct);
        const hasPct = Number.isFinite(pct);

        const tile = document.createElement('div');
        tile.className = 'disk-tile';

        const head = document.createElement('div');
        head.className = 'disk-tile__head';
        const name = document.createElement('span');
        name.className = 'disk-tile__name';
        // textContent: volume labels come from the agent, and /api/report is unauthenticated.
        name.textContent = disk.name || t('machine.disk_fallback');
        name.title = name.textContent;
        const value = document.createElement('span');
        value.className = 'disk-tile__pct';
        value.textContent = hasPct ? `${pct.toFixed(0)}%` : t('machine.unknown');
        head.append(name, value);

        const bar = document.createElement('div');
        bar.className = 'disk-tile__bar';
        const fill = document.createElement('div');
        fill.className = `disk-tile__fill ${diskFillClass(pct)}`.trim();
        fill.style.width = `${hasPct ? Math.min(100, Math.max(0, pct)) : 0}%`;
        bar.appendChild(fill);

        const meta = document.createElement('div');
        meta.className = 'stat-card__meta';
        // GB needs the agent's volume sensors (3.10.0+). Without them we still know the
        // percentage, so show the bar and say what's missing instead of an empty tile.
        if (Number.isFinite(toNumber(disk.used_gb)) && Number.isFinite(toNumber(disk.total_gb))) {
            const free = Number(disk.total_gb) - Number(disk.used_gb);
            meta.textContent = t('machine.disk_usage', {
                used: formatGb(disk.used_gb),
                total: formatGb(disk.total_gb),
                free: formatGb(free),
            });
        } else {
            meta.textContent = t('machine.disk_no_size');
        }

        tile.append(head, bar, meta);
        diskGridEl.appendChild(tile);
    }
}

// ---- Cooling cards ------------------------------------------------------------
// One tile per fan, same live-state argument as the Storage tiles above: how many fans a
// machine has is per-machine, and it CHANGES within a session -- a GPU stops its fans
// entirely when it cools below about 50 °C, so those sensors come and go. Rebuilt each
// reading for the same reason the disk tiles are.
const fanGridEl = document.getElementById('fan-grid');
const fanEmptyEl = document.getElementById('fan-empty');

function renderFans(fans) {
    if (!fanGridEl) return;
    const list = Array.isArray(fans) ? fans : [];
    fanGridEl.replaceChildren();
    fanEmptyEl.style.display = list.length ? 'none' : 'block';

    for (const fan of list) {
        const rpm = toNumber(fan.rpm);
        const duty = toNumber(fan.control_pct);
        const hasDuty = Number.isFinite(duty);

        // A fan at 0 RPM while the board is asking for duty is seized, unplugged, or dead --
        // the failure this card exists to make visible, and the one case worth colouring.
        const stalled = hasDuty && duty > 0 && rpm === 0;

        const tile = document.createElement('div');
        tile.className = 'fan-tile';
        // Which chip the fan hangs off ("Nuvoton NCT6687D", "NVIDIA RTX 3060") -- worth
        // having when a box reports "Fan #2" twice, but not worth a line of its own.
        if (fan.hardware) tile.title = fan.hardware;

        const head = document.createElement('div');
        head.className = 'fan-tile__head';
        const name = document.createElement('span');
        name.className = 'fan-tile__name';
        // textContent: sensor names come from the agent, and /api/report is unauthenticated.
        name.textContent = fan.name || t('machine.fan_fallback');
        name.title = name.textContent;
        const value = document.createElement('span');
        value.className = 'fan-tile__rpm';
        value.textContent = Number.isFinite(rpm)
            ? t('machine.fan_rpm', { value: Math.round(rpm) })
            : t('machine.unknown');
        head.append(name, value);

        // The bar shows the DUTY the board is asking for, not the speed: RPM has no
        // meaningful maximum to fill against (a case fan tops out around 1200, a blower at
        // 5000), whereas duty is a percentage by definition.
        const bar = document.createElement('div');
        bar.className = 'fan-tile__bar';
        const fill = document.createElement('div');
        fill.className = `fan-tile__fill ${stalled ? 'fan-tile__fill--stalled' : ''}`.trim();
        fill.style.width = `${hasDuty ? Math.min(100, Math.max(0, duty)) : 0}%`;
        bar.appendChild(fill);

        const meta = document.createElement('div');
        meta.className = 'stat-card__meta';
        if (stalled) {
            meta.textContent = t('machine.fan_stalled', { value: duty.toFixed(0) });
        } else if (hasDuty) {
            meta.textContent = t('machine.fan_duty', { value: duty.toFixed(0) });
        } else {
            meta.textContent = t('machine.fan_no_duty');
        }

        tile.append(head, bar, meta);
        fanGridEl.appendChild(tile);
    }
}

// ---- Show only the hardware this machine actually has --------------------------
// A card whose every reading is absent is hidden outright rather than left showing "--":
// an office PC has no discrete GPU, most laptops expose no fan, and a permanent row of
// dashes reads as "this is broken" rather than "not applicable here".
//
// Gated on diagnostics.has_sensors, which is the hub saying it HAS a sensor block for this
// machine. Without one -- a machine that has never reported -- "absent" and "not yet known"
// are indistinguishable, and hiding on the strength of that would strip the page of a PC
// that is merely offline.
let lastDiagnostics = {};

function hasReading(...values) {
    return values.some((v) => typeof v === 'number' && Number.isFinite(v));
}

function setCardVisible(id, visible) {
    const el = document.getElementById(id);
    if (el) el.hidden = !visible;
}

function applyPresence(d) {
    // The GPU card is decided BEFORE the has_sensors gate, unlike the rest. Its four lines are
    // all dashes on any machine with no GPU reading, known or not, and a tile of four "--"
    // says nothing a missing tile does not -- except, wrongly, that something is broken. It
    // stays as long as the machine reports ANY of the four, because a GPU that reports load
    // but no temperature is still a GPU worth showing. Starts hidden in the template.
    setCardVisible('card-gpu',
                   hasReading(d.gpu_temp, d.gpu_load_pct, d.gpu_clock_mhz, d.gpu_power_w));
    if (!d.has_sensors) return;
    // The CPU tile goes only when ALL three readings are absent; a missing clock or power
    // hides that one line inside it, which is what keeps the tiles from jumping rows
    // between a desktop and a laptop.
    setCardVisible('card-cpu', hasReading(d.cpu_load_pct, d.cpu_clock_mhz, d.cpu_power_w));
    setCardVisible('card-cpu-clock', hasReading(d.cpu_clock_mhz));
    setCardVisible('card-cpu-power', hasReading(d.cpu_power_w));
    setCardVisible('card-memory', hasReading(d.memory_load_pct));
    setCardVisible('card-storage', Array.isArray(d.disks) && d.disks.length > 0);
    setCardVisible('card-cooling', Array.isArray(d.fans) && d.fans.length > 0);
    for (const p of panels) setPanelVisible(p, panelHasSubject(p));
}

// Whether a chart panel has anything to be about. `points` is this panel's history for the
// loaded range when we have just fetched it; the live reading counts too, so a machine that
// only started reporting a sensor five minutes ago still gets its panel.
function panelHasSubject(panel, points) {
    // Temperature is the core metric and drives the alerts -- it keeps its panel even on a
    // machine reporting nothing, where an empty chart is itself the answer.
    if (panel.metric.key === 'temp') return true;
    if (!lastDiagnostics.has_sensors) return true;
    if (points) return true;
    if (hasReading(lastDiagnostics[panel.metric.diag])) return true;
    // Not yet loaded: leave whatever the last decision was rather than flickering the
    // panel out between a diagnostics update and the history that follows it.
    return points === undefined ? !panel.container.hidden : false;
}

function setPanelVisible(panel, visible) {
    const wasHidden = panel.container.hidden;
    panel.container.hidden = !visible;
    // Chart.js measures its canvas on creation; one built (or resized) while its container
    // was display:none comes back 0 px tall, so re-measure on the way in.
    if (wasHidden && visible) panel.chart.resize();
}

function applyDiagnostics(diagnostics) {
    const d = diagnostics || {};
    lastDiagnostics = d;
    renderDisks(d.disks);
    renderFans(d.fans);
    applyPresence(d);
    if (typeof d.mem_total_gb === 'number') updateMemoryTotal(d.mem_total_gb);
    lastCpuLoadPct = typeof d.cpu_load_pct === 'number' ? d.cpu_load_pct : null;
    document.getElementById('stat-cpu-load').textContent = formatMetric(d.cpu_load_pct, '%');
    setGauge('gauge-cpu', d.cpu_load_pct);
    showMemory(d);
    document.getElementById('stat-cpu-clock').textContent = formatClock(d.cpu_clock_mhz);
    document.getElementById('stat-cpu-power').textContent = formatMetric(d.cpu_power_w, 'W');
    // Through the catalog, like the template's server-rendered first paint: the row labels
    // ("Temp: {value}") are translated text, and hardcoding them here reverted the whole
    // card to English on the first live reading.
    const gpuTempEl = document.getElementById('stat-gpu-temp');
    gpuTempEl.textContent = t('machine.gpu_temp', { value: formatMetric(d.gpu_temp, '°C') });
    // A GPU over the hot threshold marks its card the way a hot CPU marks the temperature
    // card. Its readings are four small grey lines, so without this a 103 °C GPU at 99.9%
    // load looked exactly as calm as an idle one, two cards away from a large "3.3 %".
    const gpuHot = typeof d.gpu_temp === 'number' && d.gpu_temp >= HOT_THRESHOLD_C;
    document.getElementById('card-gpu').classList.toggle('stat-card--high-temp', gpuHot);
    gpuTempEl.classList.toggle('stat-card__meta--alert', gpuHot);
    // Load is the GPU tile's headline and bar, so it is not repeated as a fourth fact line.
    document.getElementById('stat-gpu-load-value').textContent = formatMetric(d.gpu_load_pct, '%');
    setMeter('bar-gpu', d.gpu_load_pct);
    document.getElementById('stat-gpu-clock').textContent =
        t('machine.gpu_clock', { value: formatClock(d.gpu_clock_mhz) });
    document.getElementById('stat-gpu-power').textContent =
        t('machine.gpu_power', { value: formatMetric(d.gpu_power_w, 'W') });
}

// Memory: the load percentage as the headline, used/total under it when the agent reports
// a total. Used GB comes from the same two numbers the Memory chart's tooltip uses.
function showMemory(d) {
    const pct = d.memory_load_pct;
    document.getElementById('stat-memory').textContent = formatMetric(pct, '%');
    setMeter('bar-memory', pct);
    const detail = document.getElementById('stat-memory-detail');
    if (hasReading(pct) && typeof d.mem_total_gb === 'number' && d.mem_total_gb > 0) {
        detail.textContent = t('machine.bento.memory_used', {
            used: (d.mem_total_gb * pct / 100).toFixed(1),
            total: d.mem_total_gb.toFixed(1),
        });
    } else {
        detail.textContent = '';
    }
}

// One profile value: the text, and the same text as the title, because the row is
// ellipsised at the tile's width and a serial cut to "9BVQ1..." is worth nothing.
function setProfileValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value || t('machine.unknown');
    el.title = value || '';
    el.dataset.copyValue = value || '';
}

async function loadMachineInfo() {
    try {
        const resp = await fetch('/api/machines/' + encodeURIComponent(MACHINE));
        if (!resp.ok) return;
        const info = await resp.json();
        applyDiagnostics(info.diagnostics);
        applyTemp(info.temp);
        showHeadStatus(info.status, info.updated_at, info.enrolled);
        if (info.status === 'offline') {
            showLastSeen(info.updated_at);
        } else {
            showUptime(info.uptime_seconds);
        }
        const dash = t('machine.unknown');
        document.getElementById('stat-version').textContent = info.companion_version || dash;
        setProfileValue('stat-manufacturer', info.manufacturer);
        setProfileValue('stat-model', info.model);
        setProfileValue('stat-serial', info.serial_number);
        setProfileValue('stat-service', info.service_tag);
        setProfileValue('stat-asset', info.asset_tag);
        showOperatingSystem(info);
        showDirectoryFacts(info);
    } catch (e) { /* non-critical */ }
}

// What this PC is running, with its build where one is known.
//
// The hub does the bucketing (normalize_os in app.py) and hands back the raw label, so this
// only decides how to say it. Hidden entirely when nothing is known: on a fleet whose
// agents predate OS reporting and whose hub has no directory sync, a permanent "--" reads
// as broken rather than as not-yet-collected.
//
// The build is appended rather than replacing the caption, because the two answer different
// questions -- "Windows 11 Pro" is what somebody asks for, "26100" is what decides whether
// a patch applies -- and an early Windows 11 machine reports a caption that still says 10,
// which is precisely when seeing both is worth something.
function showOperatingSystem(info) {
    const el = document.getElementById('stat-os');
    const row = document.getElementById('row-os');
    if (!el || !row) return;
    const os = info.os || {};
    if (!os.label) { row.hidden = true; return; }
    row.hidden = false;
    const build = info.os_build ? t('machine.os_build', { build: info.os_build }) : '';
    el.textContent = os.label + build;
    // Where it came from, when it is NOT the machine's own answer; otherwise the full text,
    // because the row is ellipsised.
    el.title = os.source === 'ad' ? t('inventory.os_from_directory') : os.label + build;
}

// "OU=PC_WSUS_TEST_W10,OU=FILADELFIA,DC=ccmf,DC=com,DC=py" -> "ccmf.com.py / FILADELFIA /
// PC_WSUS_TEST_W10": the domain first, then the OUs from the top down, which is the order
// ADUC's tree shows them in. A DN escapes a comma inside a name as "\,", so this splits on
// unescaped commas only -- a naive split(',') turns "OU=Smith\, J" into two OUs. A DN that
// does not parse as RDNs is shown as it came, never as an empty row.
function ouPath(dn) {
    const parts = [];
    let cur = '';
    for (let i = 0; i < dn.length; i++) {
        const ch = dn[i];
        if (ch === '\\' && i + 1 < dn.length) { cur += dn[++i]; continue; }
        if (ch === ',') { parts.push(cur); cur = ''; continue; }
        cur += ch;
    }
    parts.push(cur);
    const ous = [];
    const dcs = [];
    for (const part of parts) {
        const eq = part.indexOf('=');
        if (eq < 0) return dn;
        const type = part.slice(0, eq).trim().toUpperCase();
        const value = part.slice(eq + 1).trim();
        if (type === 'DC') dcs.push(value);
        else if (type === 'OU' || type === 'CN') ous.push(value);
    }
    ous.reverse();   // top-down, the order ADUC's tree shows them in
    const segments = ous;
    if (dcs.length) segments.unshift(dcs.join('.'));
    return segments.length ? segments.join(' / ') : dn;
}

// Click-to-copy on the profile values that get typed somewhere else: serial, service tag,
// asset tag and the OU. One delegated listener, so rows filled later need no wiring. What
// is copied is data-copy-value, never the visible text -- the OU row SHOWS a path but must
// copy the DN, and an unknown value ("--") copies nothing.
const profileEl = document.querySelector('.bento__profile');
if (profileEl) {
    const ou = document.getElementById('stat-ad-ou');
    if (ou) ou.dataset.copy = '';
    for (const dd of profileEl.querySelectorAll('dd[data-copy]')) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'kv__copy';
        btn.setAttribute('aria-label', t('machine.bento.copy'));
        btn.title = t('machine.bento.copy');
        // Two overlapping squares, the usual copy glyph; inline so it needs no icon font.
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>';
        dd.after(btn);
    }
    profileEl.addEventListener('click', async (event) => {
        const btn = event.target.closest('.kv__copy');
        if (!btn) return;
        const dd = btn.previousElementSibling;
        const value = dd?.dataset.copyValue;
        if (!value) return;
        try {
            await navigator.clipboard.writeText(value);
            btn.title = t('machine.bento.copied');
            btn.classList.add('kv__copy--done');
            setTimeout(() => {
                btn.title = t('machine.bento.copy');
                btn.classList.remove('kv__copy--done');
            }, 1500);
        } catch {
            // Deliberately not rethrown or logged: a refusal is expected, not a fault.
            // Clipboard refused (an http:// hub, or a browser policy): select the value
            // instead, so Ctrl+C is one keystroke away.
            const range = document.createRange();
            range.selectNodeContents(dd);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }
    });
}

// Active Directory facts (roadmap #4). Each row appears only when the directory actually
// supplied it: on a hub with no AD these stay hidden rather than showing "--" forever,
// and a machine AD has no computer object for shows nothing rather than a stale OU.
function showDirectoryFacts(info) {
    const ouEl = document.getElementById('stat-ad-ou');
    const ownerEl = document.getElementById('stat-ad-owner');
    const statusEl = document.getElementById('stat-ad-status');
    if (!ouEl || !ownerEl || !statusEl) return;

    document.getElementById('row-ad-ou').hidden = !info.ad_ou;
    if (info.ad_ou) {
        // Readable path in the row, the exact DN on hover and on copy: the DN is what an
        // operator pastes into ADUC or a script, the path is what they read.
        ouEl.textContent = ouPath(info.ad_ou);
        ouEl.title = info.ad_ou;
        ouEl.dataset.copyValue = info.ad_ou;
    }

    document.getElementById('row-ad-owner').hidden = !info.ad_owner;
    if (info.ad_owner) {
        ownerEl.textContent = info.ad_owner;
        ownerEl.title = info.ad_owner;
    }

    // Worth calling out: a machine still reporting telemetry whose computer account has
    // been disabled is usually a half-finished decommission, and nothing else on this
    // page would show it.
    statusEl.hidden = !info.ad_disabled;
    if (info.ad_disabled) {
        statusEl.replaceChildren();
        const pill = document.createElement('span');
        pill.className = 'status-pill status-pill--danger';
        const dot = document.createElement('span');
        dot.className = 'status-pill__dot';
        pill.append(dot, document.createTextNode(t('machine.ad_disabled')));
        statusEl.appendChild(pill);
    }
}

// ---- Primary sensor pin -------------------------------------------------------
// Populated from what this machine is actually reporting, so the operator picks a real
// name by recognition rather than typing one that has to match exactly.
const primarySensorSelect = document.getElementById('primary-sensor');
const primarySensorSave = document.getElementById('primary-sensor-save');
const primarySensorStatus = document.getElementById('primary-sensor-status');
const primarySensorOrder = document.getElementById('primary-sensor-order');
let savedPrimarySensor = '';

async function loadPrimarySensor() {
    const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/sensors`);
    if (!resp.ok) return;
    const body = await resp.json();

    savedPrimarySensor = body.primary_sensor_name || '';
    // Rebuild, keeping the "follow the fleet order" option at the top. Its label stays
    // short on purpose -- the preference chain can be five names long, and putting it in
    // the option text stretches the select across the whole card. It goes in the help
    // line below instead, where it costs no layout.
    primarySensorSelect.replaceChildren();
    const followOpt = document.createElement('option');
    followOpt.value = '';
    followOpt.textContent = t('machine.sensor.follow_fleet');
    primarySensorSelect.appendChild(followOpt);

    primarySensorOrder.textContent = (body.preference && body.preference.length)
        ? t('machine.sensor_order', { order: body.preference.join(' → ') })
        : '';

    for (const s of body.sensors || []) {
        const opt = document.createElement('option');
        opt.value = s.name;
        // textContent, never innerHTML: these names come from the agent, and /api/report
        // is unauthenticated.
        opt.textContent = s.value === null || s.value === undefined
            ? s.name
            : t('machine.sensor_with_temp', { name: s.name, value: s.value });
        primarySensorSelect.appendChild(opt);
    }

    // A pinned sensor the machine isn't currently reporting would otherwise vanish from
    // the list and look unset. Show it, flagged, so the operator can see why the pin
    // isn't taking effect.
    if (savedPrimarySensor && !(body.sensors || []).some((s) => s.name === savedPrimarySensor)) {
        const missing = document.createElement('option');
        missing.value = savedPrimarySensor;
        missing.textContent = t('machine.sensor_missing', { name: savedPrimarySensor });
        primarySensorSelect.appendChild(missing);
    }

    primarySensorSelect.value = savedPrimarySensor;
    primarySensorSave.hidden = true;
    primarySensorStatus.textContent = (!body.sensors || !body.sensors.length)
        ? t('machine.sensor_none')
        : '';
}

// ------------------------------------------------------------------- release channel
//
// Which agent build this machine follows (roadmap #21). The picker is only built when the
// server said `can_manage`: an operator with `view` should see which train the PC is on --
// that is inventory -- without being handed a control that would 403.

const channelLine = document.getElementById('stat-channel');
const channelNote = document.getElementById('stat-channel-note');
const channelPicker = document.getElementById('channel-picker');
const channelSelect = document.getElementById('channel-select');
let savedChannel = '';

// The same three-component comparison as processes.js and fleet-terminal.js (VERSIONING.md
// lists all of them). Only ever fed a validated pin and a reported version.
function versionLess(a, b) {
    const pa = String(a).split('.').map(Number);
    const pb = String(b).split('.').map(Number);
    for (let i = 0; i < 3; i++) {
        const x = pa[i] || 0, y = pb[i] || 0;
        if (x !== y) return x < y;
    }
    return false;
}

// Literal t() keys on purpose -- tests/test_i18n.py only sees literal calls.
function channelNoteText(body) {
    const policy = body.update_policy || {};
    const pin = policy.mode === 'pinned' ? policy.version : '';
    const running = body.running_version || '';
    if (pin && running && !body.honours_pin) {
        return t('machine.channel_pin_unsupported', {
            running, min: body.pin_aware_version || '',
        });
    }
    if (pin && body.pin_manifest_exists === false) {
        return t('machine.channel_pin_missing', { version: pin });
    }
    if (pin && body.platform === 'android' && running && versionLess(pin, running)) {
        return t('machine.channel_pin_android_downgrade', { version: pin, running });
    }
    if (pin) return t('machine.channel_pinned_version', { version: pin });
    if (body.following_stable) {
        return t('machine.channel_following_stable', { stable: body.latest_stable_version || '' });
    }
    if (body.ahead_of_stable) {
        return t('machine.channel_ahead', {
            running, stable: body.latest_stable_version,
        });
    }
    return '';
}

async function loadChannel() {
    const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/channel`);
    if (!resp.ok) return;
    const body = await resp.json();

    const label = (body.channels.find((c) => c.name === body.effective_channel) || {}).label
        || body.effective_channel;
    channelLine.textContent = body.pinned
        ? t('machine.channel_pinned', { value: label })
        : t('machine.channel', { value: label });

    // The line that stops a deliberately-stalled machine looking broken. Moving a PC off
    // beta does not roll it back -- every updater installs only what is strictly newer -- so
    // it sits on the build it has until stable passes it. Saying that here is the difference
    // between an explained state and a support call.
    //
    // One note at most, the most consequential first: a pin this agent cannot honour, a pin
    // with nothing behind it, a downgrade Android will refuse, then the pin itself, then the
    // beta-following-stable and ahead-of-stable explanations.
    const note = channelNoteText(body);
    channelNote.hidden = !note;
    channelNote.textContent = note || '';

    channelPicker.hidden = !body.can_manage;
    if (!body.can_manage) return;

    savedChannel = body.channel || '';
    channelSelect.replaceChildren();
    const follow = document.createElement('option');
    follow.value = '';
    follow.textContent = t('machine.channel_follow_fleet');
    channelSelect.appendChild(follow);
    for (const c of body.channels) {
        const opt = document.createElement('option');
        opt.value = c.name;
        // textContent, never innerHTML -- these are catalog strings, but the rule here is
        // absolute rather than per-source.
        opt.textContent = c.label;
        channelSelect.appendChild(opt);
    }
    channelSelect.value = savedChannel;
}

let channelSaveSeq = 0;
channelSelect.addEventListener('change', saveChannel);

async function saveChannel() {
    if (channelSelect.value === savedChannel) return;
    const seq = ++channelSaveSeq;
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/channel`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel: channelSelect.value || null }),
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${resp.status}`);
        }
        if (seq !== channelSaveSeq) return;
        await loadChannel();
    } catch (e) {
        channelSelect.value = savedChannel;
        toast(e.message, { kind: 'error' });
    }
}

loadChannel();

// Picking a sensor saves it immediately -- no Save button (it stays hidden). A seq guard
// drops a slow response that a newer pick has already superseded.
let primarySensorSaveSeq = 0;

primarySensorSelect.addEventListener('change', savePrimarySensor);

async function savePrimarySensor() {
    if (primarySensorSelect.value === savedPrimarySensor) return;   // nothing changed
    const seq = ++primarySensorSaveSeq;
    primarySensorStatus.textContent = 'Saving…';
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/primary_sensor`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ primary_sensor_name: primarySensorSelect.value || null }),
        });
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${resp.status}`);
        }
        if (seq !== primarySensorSaveSeq) return;
        await loadPrimarySensor();
        primarySensorStatus.textContent = t('machine.sensor_saved');
    } catch (e) {
        if (seq !== primarySensorSaveSeq) return;
        primarySensorStatus.textContent = t('machine.sensor_save_failed', { error: e.message });
    }
}

// ---- Every sensor the machine reports ------------------------------------------
// The cards and charts above are a chosen dozen readings. This is the rest: the whole
// flattened LHM tree the agent already sends, grouped hardware -> category -> sensor, so
// the VRM temperature or the +12V rail is there when somebody needs it -- without any of
// them having to be promoted to a card first.
//
// Polled only while the section is open AND the tab is in front. Collapsed (the default)
// it costs nothing, which is what makes it affordable to show several hundred readings on
// a page that is otherwise a summary.
const sensorBrowserEl = document.getElementById('sensor-browser');
const sensorBrowserBody = document.getElementById('sensor-browser-body');
const sensorBrowserCount = document.getElementById('sensor-browser-count');
const sensorFilterEl = document.getElementById('sensor-filter');
const SENSOR_REFRESH_MS = 10000;        // the agent's own sensor reporting cadence
let sensorTree = [];
let sensorTimer = null;

async function loadAllSensors() {
    try {
        const resp = await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/sensors/all`);
        if (!resp.ok) return;
        const body = await resp.json();
        sensorTree = Array.isArray(body.hardware) ? body.hardware : [];
        sensorBrowserCount.textContent =
            tPlural('machine.sensors.count', Number(body.count) || 0);
        renderSensorTree();
    } catch (e) { /* non-critical */ }
}

// Value as the AGENT formatted it (text: "61.0 °C", "1120.0 RPM"), falling back to the raw
// number. That is what lets this table show a sensor type the hub has never heard of with
// the right unit -- the agent knows LHM's vocabulary, and the hub does not have to.
function sensorValueText(sensor) {
    if (typeof sensor.text === 'string' && sensor.text) return sensor.text;
    if (typeof sensor.value === 'number' && Number.isFinite(sensor.value)) {
        return String(sensor.value);
    }
    return t('machine.unknown');
}

function renderSensorTree() {
    const needle = (sensorFilterEl.value || '').trim().toLowerCase();
    sensorBrowserBody.replaceChildren();
    let shown = 0;

    for (const hw of sensorTree) {
        const hwName = hw.name || '';
        const groups = [];
        for (const group of hw.groups || []) {
            const matches = (group.sensors || []).filter((s) => !needle ||
                `${hwName} ${group.name || ''} ${s.name || ''}`.toLowerCase().includes(needle));
            if (matches.length) groups.push({ name: group.name || '', sensors: matches });
        }
        if (!groups.length) continue;

        const section = document.createElement('div');
        section.className = 'sensor-hw';
        const heading = document.createElement('div');
        heading.className = 'sensor-hw__name';
        // textContent throughout: every name here came off an agent, and /api/report is
        // unauthenticated.
        heading.textContent = hwName;
        section.appendChild(heading);

        for (const group of groups) {
            const groupEl = document.createElement('div');
            groupEl.className = 'sensor-group';
            groupEl.textContent = group.name;
            section.appendChild(groupEl);

            const table = document.createElement('table');
            table.className = 'data-table sensor-table';
            // Label/value pairs with no header row: nothing to filter, nothing to stack.
            table.dataset.tableTools = 'off';
            const tbody = document.createElement('tbody');
            for (const sensor of group.sensors) {
                const tr = document.createElement('tr');
                const nameCell = document.createElement('td');
                nameCell.textContent = sensor.name || '';
                const valueCell = document.createElement('td');
                valueCell.className = 'sensor-table__value';
                valueCell.textContent = sensorValueText(sensor);
                tr.append(nameCell, valueCell);
                tbody.appendChild(tr);
                shown += 1;
            }
            table.appendChild(tbody);
            section.appendChild(table);
        }
        sensorBrowserBody.appendChild(section);
    }

    if (!shown) {
        const empty = document.createElement('div');
        empty.className = 'stat-card__meta';
        empty.textContent = needle ? t('machine.sensors.no_match') : t('machine.sensors.none');
        sensorBrowserBody.appendChild(empty);
    }
}

function syncSensorPolling() {
    const active = sensorBrowserEl.open && document.visibilityState === 'visible';
    if (active && sensorTimer === null) {
        loadAllSensors();
        sensorTimer = setInterval(loadAllSensors, SENSOR_REFRESH_MS);
    } else if (!active && sensorTimer !== null) {
        clearInterval(sensorTimer);
        sensorTimer = null;
    }
}

sensorBrowserEl.addEventListener('toggle', syncSensorPolling);
document.addEventListener('visibilitychange', syncSensorPolling);

// The History section folds too now, and Chart.js measures a canvas when it is built: one
// built inside a folded section comes back zero pixels tall and STAYS that way, because
// nothing re-measures it on the way out. Same fix as setPanelVisible() applies to a panel
// hidden for having no readings, applied to the whole section at once.
const historyCardEl = document.getElementById('history-card');
if (historyCardEl) {
    historyCardEl.addEventListener('toggle', () => {
        if (!historyCardEl.open) return;
        for (const panel of panels) {
            if (!panel.container.hidden) panel.chart.resize();
        }
    });
}
// Re-render from what we already hold: filtering is a view of the last poll, so typing
// doesn't wait on the network.
sensorFilterEl.addEventListener('input', renderSensorTree);

// ---- "Somebody is watching this" ------------------------------------------------
// A machine reports every five seconds normally, with a full sensor block every other
// report -- so the panels on this page, whose window is sixty seconds wide, drew about a
// dozen points a minute and a three-second spike was one dot or none. While this page is
// OPEN AND IN FRONT we tell the hub so, and that machine reports every second instead (see
// hub/live.py). Nobody looking, nothing extra: the cost is bounded by attention.
//
// Pinging IS the subscription, and there is no unsubscribe -- the watch lapses ~20 seconds
// after the last ping, which is what covers the tab being closed, the laptop sleeping, or
// the browser being killed, none of which get to send a farewell.
//
// Deliberately NOT conditional on follow mode: an operator who panned to look at the last
// thirty seconds is still watching this machine live, and every reading still lands on the
// panels whether the axis is following or not.
const LIVE_WATCH_MS = (Number(config.dataset.livePollSeconds) || 5) * 1000;
let liveWatchTimer = null;

function observingLive() {
    return viewingToday && document.visibilityState === 'visible';
}

async function pingLiveWatch() {
    if (!observingLive()) return;
    try {
        await fetch(`/api/machines/${encodeURIComponent(MACHINE)}/live/watch`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: '{}' });
    } catch (e) {
        // A missed ping costs one slower cadence cycle; the next one renews. Never worth
        // anything visible on the page.
    }
}

// One timer for the life of the page rather than start/stop on every state change: the
// condition is re-tested each tick, so switching days, hiding the tab or coming back needs
// no wiring. Only the "came back to the tab" case is worth being prompt about, and that is
// the listener below -- otherwise the operator watches five-second steps for one more tick
// after alt-tabbing back.
liveWatchTimer = setInterval(pingLiveWatch, LIVE_WATCH_MS);
document.addEventListener('visibilitychange', pingLiveWatch);
pingLiveWatch();

// At 1 Hz a page left open all day would otherwise accumulate ~86k points per panel, so the
// live tail is bounded. Trimmed only while follow mode is on -- that is precisely when
// everything being dropped is off-screen to the left -- and generously, so panning back over
// the last couple of hours still finds the points it had. Anything older than the tail comes
// back from the history endpoint on the next viewport load, or on Reset.
const LIVE_TAIL_MS = 2 * 60 * 60 * 1000;

function trimLiveTail(chart, newestMs) {
    const data = chart.data.datasets[0].data;
    // Never shorter than the window on screen: with the 6 h or 24 h pill the two-hour tail
    // would trim points the operator is looking at.
    const cutoff = newestMs - Math.max(LIVE_TAIL_MS, openWindowMs);
    if (!data.length || data[0].x >= cutoff) return;
    let drop = 0;
    while (drop < data.length && data[drop].x < cutoff) drop += 1;
    data.splice(0, drop);
}

dayPicker.value = getLocalDateString();
syncResolutionControl();
buildPanels();
loadSelectedDay();
loadMachineInfo();
loadPrimarySensor();
// The machine record and its online/offline state, pushed. `status` is what flips the Uptime
// card to "Last seen" when this PC goes quiet -- before, an open page never noticed. A
// directory sync can change the AD facts, and an agent self-update the version. Readings
// themselves stay on the `new_temp` handler below, which is per report and in place.
window.FleetLive.watch(['machines', 'status', 'directory'], loadMachineInfo, { machine: MACHINE });
// The primary-sensor pin, re-read when somebody else changes it -- but never while this
// operator has the picker in hand, which would snap it back under them.
window.FleetLive.watch(['machines'], loadPrimarySensor, {
    machine: MACHINE,
    busy: () => document.activeElement === primarySensorSelect
        || primarySensorSelect.value !== savedPrimarySensor,
});

socket.on('new_temp', (msg) => {
    if (msg.machine !== MACHINE) return;
    applyDiagnostics(msg.diagnostics);
    applyTemp(msg.temp);
    // A live report means the machine is online now, so restore the uptime readout
    // (it may have been showing "Last seen" from an earlier offline load).
    if (msg.uptime_seconds !== undefined && msg.uptime_seconds !== null) {
        showUptime(msg.uptime_seconds);
    }
    // ...and so the header pill says online, seen just now.
    showHeadStatus('online', msg.timestamp || Math.floor(Date.now() / 1000));
    // Follow an agent self-update without a refresh. Only present when the client
    // reported one, so an older client's silence can't blank the version we already show.
    if (msg.companion_version) {
        document.getElementById('stat-version').textContent = msg.companion_version;
    }
    if (!viewingToday) return;
    // A 7 d or 30 d series is 15-minute or hourly averages. Appending a raw point every
    // second to it drew a hairy fringe at the right edge of a smooth line, and an hour of
    // them outnumbered the whole month on the chart. The tiles above stay live; these cards
    // catch up on the next range change or Reset.
    if (isMultiDay()) return;

    const x = toChartTimestamp(msg.timestamp_ms ?? msg.timestamp_epoch ?? msg.timestamp);
    if (x === null) return;
    // Append this report to every panel: temperature from msg.temp, the rest from the live
    // diagnostics block (which now carries disk & network alongside cpu/gpu/memory). A metric
    // the machine doesn't report is simply skipped for that tick.
    const diagnostics = msg.diagnostics || {};
    // Re-anchor the window on this reading BEFORE the per-panel loop, and apply it to every
    // panel rather than only the ones that carried a value this tick -- a grid whose panels
    // sat on two different time axes because one sensor went quiet is exactly the confusion
    // the cross-panel mirroring elsewhere in this file exists to prevent.
    // Date.now() as well as x: a machine whose clock runs behind the browser's would
    // otherwise park the window in the past and never show the live edge.
    const followMax = followLive ? Math.max(x, Date.now()) : null;
    for (const p of panels) {
        if (followLive) {
            p.chart.options.scales.x.min = followMax - openWindowMs;
            p.chart.options.scales.x.max = followMax;
        }
        const y = p.metric.key === 'temp' ? toNumber(msg.temp) : toNumber(diagnostics[p.metric.diag]);
        if (!Number.isFinite(y)) {
            if (followLive) p.chart.update('none');
            continue;
        }
        p.chart.data.datasets[0].data.push({ x, y });
        if (followLive) trimLiveTail(p.chart, followMax);
        p.emptyEl.style.display = 'none';
        p.chart.update('none');
    }
    scheduleStats();
    if (selectedDayRange) selectedDayRange.endMs = Math.max(selectedDayRange.endMs, x);
    noDataEl.style.display = 'none';
});
