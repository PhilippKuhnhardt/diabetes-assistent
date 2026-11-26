// Glucose charts: the day overview ("Verlauf", "Tag durchgehen"), each meal's curve with its counted
// window, and the interactive window editor. The server sends readings with their colour tone, target
// bands, events and each meal's window; the chart only places them. Chart units equal CSS pixels at
// the measured width, so text and touch targets keep their size.
import { html, useState, useEffect, useRef } from "./html.js";
import { MINUTE, num, timeOnly, sportNames, UNIT } from "./lib.js";
import { t } from "./i18n.js";

export function chartLayout(width) {
  const clamped = Math.max(300, Math.min(900, Math.round(width || 600)));
  return { width: clamped, left: 36, right: clamped - 12, plotTop: 70, plotBottom: 258, height: 284,
    lanes: { insulin: 14, carbs: 34, sport: 46 } };
}

// Verlauf's meal cards: the same chart, lower, with its event lanes kept.
export function compactLayout(width) {
  const clamped = Math.max(260, Math.min(900, Math.round(width || 600)));
  return { width: clamped, left: 30, right: clamped - 8, plotTop: 50, plotBottom: 150, height: 172, compact: true,
    lanes: { insulin: 10, carbs: 26, sport: 34 } };
}

// The width of an element, kept current while it is resized, opened (a <details>) or shown again.
export function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const observer = new ResizeObserver(() => setWidth(node.clientWidth));
    observer.observe(node);
    setWidth(node.clientWidth);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

export function glucosePoints(entries) {
  return entries.map(point => ({ t: new Date(point.at).getTime(), g: Number(point.sgv), tone: point.tone }))
    .filter(point => Number.isFinite(point.g)).sort((a, b) => a.t - b.t);
}

// The server sends each meal's counted window and the CGM value at both ends.
export function mealWindow(meal) {
  const window = meal.window;
  const time = value => new Date(value).getTime();
  return {
    mealAt: time(meal.at), start: time(window.start), end: time(window.end),
    autoStart: time(window.auto_start), autoEnd: time(window.auto_end), manual: window.manual,
    startGlucose: window.start_glucose, endGlucose: window.end_glucose,
    sourceId: meal.source_id || null,
  };
}

export function chartEvents(treatments) {
  return treatments.flatMap(item => {
    const at = new Date(item.at).getTime();
    if (item.eventType === "Exercise") {
      return [{ kind: "sport", t: at, end: at + Number(item.duration || 0) * MINUTE, label: sportNames[item.sport_type] || t("sport.sport") }];
    }
    const result = [];
    if (Number(item.insulin) > 0) result.push({ kind: "insulin", id: item.id, t: at, amount: Number(item.insulin), rapid: Boolean(item.rapid) });
    if (Number(item.carbs) > 0) result.push({ kind: "carbs", id: item.id, t: at, amount: Number(item.carbs) });
    return result;
  });
}

// The server's limits for a manual meal window, in milliseconds.
export function windowLimits(dayData) {
  const limits = dayData?.window_limits || { lead_minutes: 120, lag_minutes: 120, min_minutes: 30, max_minutes: 600 };
  return { lead: limits.lead_minutes * MINUTE, lag: limits.lag_minutes * MINUTE,
    min: limits.min_minutes * MINUTE, max: limits.max_minutes * MINUTE };
}

export function chartView(range, limits) {
  const from = Math.min(range.start, range.autoStart) - 45 * MINUTE;
  const to = Math.max(range.end, range.autoEnd, range.mealAt + 240 * MINUTE) + 75 * MINUTE;
  return {
    from: Math.max(from, range.mealAt - limits.lead - 30 * MINUTE),
    to: Math.min(to, range.mealAt + limits.max + 60 * MINUTE),
  };
}

export function chartScales(layout, view, points) {
  const visible = points.filter(point => point.t >= view.from && point.t <= view.to).map(point => point.g);
  const top = Math.max(300, Math.ceil((Math.max(0, ...visible) + 20) / 50) * 50);
  const bottom = Math.min(40, Math.floor((Math.min(400, ...visible) - 10) / 10) * 10);
  return {
    x: t => layout.left + (t - view.from) / (view.to - view.from) * (layout.right - layout.left),
    y: g => layout.plotBottom - (g - bottom) / (top - bottom) * (layout.plotBottom - layout.plotTop),
    top, bottom,
  };
}

// The meal's own entry (carbs and bolus) always counts, even when the start was moved after it.
export const windowIncludes = (range, event) => (range.sourceId && event.id === range.sourceId) || (event.t >= range.start && event.t < range.end);

const ICONS = {
  insulin: html`<path d="M-5 5l3-3m-1-1 6-6 3 3-6 6zM4-7l3 3M5-8l3 3"/>`,
  carbs: html`<path d="M0-3c-2-2-6-1-6 3s3 7 6 7 6-3 6-7-4-5-6-3zM0-3c0-2 1-4 3-5"/>`,
};

function EventLanes({ events, scales, view, range, layout }) {
  const labelEnds = {};
  const shown = events.filter(event => (event.end || event.t) >= view.from && event.t <= view.to).sort((a, b) => a.t - b.t);
  const iconsCovered = (kind, from, to, own) => shown.some(other => other !== own && other.kind === kind &&
    scales.x(other.t) + 8 > from && scales.x(other.t) - 8 < to);
  return shown.map(event => {
    const x = scales.x(Math.max(event.t, view.from));
    const included = windowIncludes(range, event) ? " is-included" : "";
    if (event.kind === "sport") {
      const width = Math.max(4, scales.x(Math.min(event.end, view.to)) - x);
      const top = layout.lanes.sport;
      return html`<g class=${"chart-event sport" + included}><rect x=${x.toFixed(1)} y=${top} width=${width.toFixed(1)} height="14" rx="4" />
        <text x=${(x + 4).toFixed(1)} y=${top + 11}>${event.label}</text></g>`;
    }
    const y = layout.lanes[event.kind];
    const label = event.kind === "insulin" ? num(event.amount, 1) + " " + UNIT.insulin : num(event.amount, 0) + " g";
    // Labels near the right edge flip left; a label that would overlap its lane neighbour is left out.
    const labelWidth = label.length * 6;
    const flip = x + 10 + labelWidth > layout.right;
    const textStart = flip ? x - 10 - labelWidth : x + 10;
    const showLabel = textStart >= (labelEnds[event.kind] ?? -Infinity) && !iconsCovered(event.kind, textStart, textStart + labelWidth, event);
    if (showLabel) labelEnds[event.kind] = textStart + labelWidth + 4;
    return html`<g class=${"chart-event " + event.kind + (event.rapid === false ? " non-rapid" : "") + included}>
      <line x1=${x.toFixed(1)} y1=${y + 9} x2=${x.toFixed(1)} y2=${layout.plotBottom} class="event-guide" />
      <g transform=${"translate(" + x.toFixed(1) + " " + y + ")"}>${ICONS[event.kind]}</g>
      ${showLabel && html`<text x=${(flip ? x - 10 : x + 10).toFixed(1)} y=${y + 4} text-anchor=${flip ? "end" : null}>${label}</text>`}</g>`;
  });
}

function Handle({ name, at, scales, layout, interactive, reading, active, snapped }) {
  const value = reading?.value != null ? Number(reading.value) : null;
  const dotY = value == null ? layout.plotBottom : scales.y(value);
  const label = name === "start" ? t("window.startHandle") : t("window.endHandle");
  return html`<g class=${"window-handle" + (active ? " is-active" : "") + (snapped ? " is-snapped" : "")} data-handle=${name}
    transform=${"translate(" + scales.x(at).toFixed(1) + " 0)"} tabindex=${interactive ? "0" : null} role=${interactive ? "slider" : null}
    aria-label=${interactive ? label : null} aria-valuetext=${interactive ? t("common.clock", { time: timeOnly(at) }) : null}>
    <line class="handle-line" x1="0" y1=${layout.plotTop - 6} x2="0" y2=${interactive ? 290 : layout.plotBottom} />
    <circle class="handle-dot" data-tone=${reading?.tone || "none"} cx="0" cy=${dotY.toFixed(1)} r=${interactive ? 7 : 4.5} hidden=${value == null} />
    ${interactive && html`<rect class="handle-hit" x="-24" y="62" width="48" height="262" />
      <g class="handle-knob"><rect x="-21" y="288" width="42" height="30" rx="15" /><path d="M-5 297v12M0 297v12M5 297v12" /></g>`}</g>`;
}

// One chart. `range` is a meal window (or {start: -Infinity, end: Infinity, mealAt} for the day),
// `overview` draws the day without a window, `editor` adds the window editor's handles and readings.
// `sport` lists sessions acting on a meal ({from, until} of their effect): a blue band along the bottom, darker
// where it lies inside the window, so the share a meal takes is visible.
export function GlucoseChart({ layout, range, view, entries, treatments, bands, overview, clipId, editor, sport }) {
  const interactive = Boolean(editor);
  const points = glucosePoints(entries);
  const scales = chartScales(layout, view, points);
  const visible = points.filter(point => point.t >= view.from - 10 * MINUTE && point.t <= view.to + 10 * MINUTE);
  const height = interactive ? 324 : layout.height;
  const { left, right, plotTop, plotBottom, width } = layout;
  const grid = [];
  for (let g = Math.ceil(scales.bottom / 50) * 50; g <= scales.top; g += 50) grid.push(g);
  const hour = 60 * MINUTE;
  const step = [1, 2, 3, 6].map(hours => hours * hour).find(candidate => (view.to - view.from) / candidate * 48 <= right - left) || 6 * hour;
  const ticks = [];
  for (let t = Math.ceil(view.from / step) * step; t <= view.to; t += step) ticks.push(t);
  const limit = (g, className) => g > scales.bottom && g < scales.top
    ? html`<line class=${className} x1=${left} x2=${right} y1=${scales.y(g).toFixed(1)} y2=${scales.y(g).toFixed(1)} />` : null;
  const segments = [];
  let segment = [];
  visible.forEach((point, index) => {
    if (index && point.t - visible[index - 1].t > 15 * MINUTE) { segments.push(segment); segment = []; }
    segment.push(scales.x(point.t).toFixed(1) + "," + scales.y(point.g).toFixed(1));
  });
  segments.push(segment);
  const last = visible.filter(point => point.t <= view.to).pop();
  const bandRects = (bands || []).map(band => {
    const from = Math.max(new Date(band.from).getTime(), view.from), to = Math.min(new Date(band.to).getTime(), view.to);
    const high = Math.min(Number(band.high_mgdl), scales.top), low = Math.max(Number(band.low_mgdl), scales.bottom);
    if (to <= from || high <= low) return null;
    return html`<rect class="target-band" x=${scales.x(from).toFixed(1)} y=${scales.y(high).toFixed(1)}
      width=${(scales.x(to) - scales.x(from)).toFixed(1)} height=${(scales.y(low) - scales.y(high)).toFixed(1)} />`;
  });
  const label = overview
    ? t("chart.overviewLabel", { from: timeOnly(view.from), to: timeOnly(view.to) })
    : t("chart.windowLabel", { from: timeOnly(range.start), to: timeOnly(range.end) });
  const clip = clipId || (overview ? "overview-clip" : interactive ? "editor-clip" : "chart-clip-" + range.mealAt);
  const reading = name => editor ? editor.reading(range[name]) : name === "start" ? range.startGlucose : range.endGlucose;
  return html`<svg class=${"window-chart-svg" + (interactive ? " is-interactive" : "")} viewBox=${"0 0 " + width + " " + height} role="img" aria-label=${label}>
    <defs><clipPath id=${clip}><rect x=${left} y="0" width=${right - left} height=${height} /></clipPath></defs>
    <rect class="plot-bg" x=${left} y=${plotTop} width=${right - left} height=${plotBottom - plotTop} />
    <g clip-path=${"url(#" + clip + ")"}>
      ${bandRects}
      ${grid.map(g => html`<line class="grid" x1=${left} x2=${right} y1=${scales.y(g).toFixed(1)} y2=${scales.y(g).toFixed(1)} />`)}
      ${limit(250, "limit-high")}${limit(70, "limit-low")}
      ${!overview && html`<rect class="window-shade" x=${scales.x(range.start).toFixed(1)} y=${plotTop - 6}
        width=${(scales.x(range.end) - scales.x(range.start)).toFixed(1)} height=${plotBottom - plotTop + 6} />`}
      ${!overview && (range.manual || interactive) && (range.autoStart !== range.start || range.autoEnd !== range.end) &&
        [range.autoStart, range.autoEnd].map(t => html`<line class="auto-marker" x1=${scales.x(t).toFixed(1)} x2=${scales.x(t).toFixed(1)} y1=${plotTop} y2=${plotBottom} />`)}
      ${(sport || []).map(band => {
        const from = Math.max(band.from, view.from), to = Math.min(band.until, view.to);
        const inFrom = Math.max(from, range.start), inTo = Math.min(to, range.end);
        if (to <= from) return null;
        return html`<g class="sport-band"><rect x=${scales.x(from).toFixed(1)} y=${plotBottom - 7} width=${(scales.x(to) - scales.x(from)).toFixed(1)} height="7" />
          ${inTo > inFrom && html`<rect class="is-inside" x=${scales.x(inFrom).toFixed(1)} y=${plotBottom - 7} width=${(scales.x(inTo) - scales.x(inFrom)).toFixed(1)} height="7" />`}</g>`;
      })}
      <${EventLanes} events=${chartEvents(treatments)} scales=${scales} view=${view} range=${range} layout=${layout} />
      ${segments.filter(item => item.length > 1).map(item => html`<polyline class="curve" points=${item.join(" ")} />`)}
      ${overview
        ? last && html`<circle class="handle-dot" data-tone=${last.tone || "none"} cx=${scales.x(last.t).toFixed(1)} cy=${scales.y(last.g).toFixed(1)} r="4.5" />`
        : ["start", "end"].map(name => html`<${Handle} name=${name} at=${range[name]} scales=${scales} layout=${layout} interactive=${interactive}
            reading=${reading(name)} active=${editor?.active === name} snapped=${Boolean(editor?.snapped[name])} />`)}
    </g>
    <line class="axis" x1=${left} x2=${right} y1=${plotBottom} y2=${plotBottom} />
    ${ticks.map(t => html`<line class="tick" x1=${scales.x(t).toFixed(1)} x2=${scales.x(t).toFixed(1)} y1=${plotBottom} y2=${plotBottom + 5} />
      <text class="x-label" x=${scales.x(t).toFixed(1)} y=${plotBottom + 19} text-anchor="middle">${timeOnly(t)}</text>`)}
    ${grid.map(g => html`<text class="y-label" x=${left - 6} y=${(scales.y(g) + 4).toFixed(1)} text-anchor="end">${g}</text>`)}
    ${!layout.compact && html`<text class="y-unit" x="2" y=${plotTop - 16}>mg/dL</text>`}</svg>`;
}

// The day as one chart: today the last hours up to now, otherwise the whole day.
export function DayChart({ data, view, clipId, className = "day-overview-chart", id }) {
  const [ref, width] = useWidth();
  return html`<div id=${id} class=${className} ref=${ref}>${width > 0 && data && html`<${GlucoseChart} layout=${chartLayout(width)}
    range=${{ start: -Infinity, end: Infinity, mealAt: view.from }} view=${view} entries=${data.entries} treatments=${data.treatments}
    bands=${data.target_bands} overview clipId=${clipId} />`}</div>`;
}

// A meal's curve with its counted window; drawn once its block is open and has a width.
export function MealChart({ meal, data, compact }) {
  const [ref, width] = useWidth();
  const range = mealWindow(meal);
  const sport = (meal.sport?.events || []).filter(event => event.effect_from && event.effect_until)
    .map(event => ({ from: new Date(event.effect_from).getTime(), until: new Date(event.effect_until).getTime() }));
  return html`<div class="meal-chart-plot" data-meal-id=${meal.id} ref=${ref}>${width > 0 && html`<${GlucoseChart}
    layout=${compact ? compactLayout(width) : chartLayout(width)} sport=${sport}
    range=${range} view=${chartView(range, windowLimits(data))} entries=${data.entries} treatments=${data.treatments} bands=${data.target_bands} />`}</div>`;
}
