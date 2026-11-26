// "Verlauf" (formerly "Tag durchgehen", redesigned 2026-11-23): the place to page through past days and
// the only place a day's data is maintained (page roles, AGENTS.md). A week strip picks the day; each main
// meal is a card with the entries the server grouped under it, its curve and counted window ("Passt"
// checks it, "Zeitraum anpassen" opens the window editor) and exclusion; sport sessions are cards of their
// own (the day's chart, effects and night cards were removed 2026-11-23). Everything shown comes from the day payload
// (/api/day) and the week (/api/days/week); actions post and then reload the day.
import { html, useState, useEffect, useLayoutEffect, useRef, useMemo } from "./html.js";
import { MINUTE, api, post, toast, num, factorNum, signedChange, sportChange, timeOnly, trendSymbol, names, sportNames,
  dayDate, UNIT, Icon, FactorIcons, ExcludeButton } from "./lib.js";
import { t } from "./i18n.js";
import { sportSummary, signedMgdl } from "./sport.js";
import { MealChart, mealWindow, chartEvents, chartView, chartLayout, windowLimits,
  GlucoseChart, useWidth } from "./charts.js";

const entryLabels = Object.fromEntries(["finger", "basal", "meal", "snack", "carbs", "correction", "exercise", "other"]
  .map(kind => [kind, t("entryKind." + kind)]));

export function entryLabel(item, data) {
  if (item.kind === "exercise") return sportNames[item.sport_kind] || t("sport.sport");
  const meal = item.kind === "meal" ? data.meals.find(row => row.source_id === item.id) : null;
  return meal ? names[meal.meal] : entryLabels[item.kind];
}

export const dayTypeSources = {
  weekday: t("dayType.sources.weekday"),
  vacation: t("dayType.sources.vacation"),
  manual: t("dayType.sources.manual"),
};

// Local midnight to midnight of the shown day as the server counts it (travel days are shorter or longer).
export function dayBounds(data, day) {
  if (data?.day === day && data.day_start && data.day_end) {
    return { dayStart: new Date(data.day_start).getTime(), dayEnd: new Date(data.day_end).getTime() };
  }
  const dayStart = new Date(day + "T00:00:00").getTime();
  const next = new Date(dayStart + 36 * 60 * MINUTE);
  return { dayStart, dayEnd: new Date(next.toLocaleDateString("sv-SE") + "T00:00:00").getTime() };
}

// Runs an action from a button: the button waits, errors become a toast.
async function act(event, action) {
  const button = event.currentTarget;
  button.disabled = true;
  try { await action(); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

// ---- Verlauf (user request 2026-11-23, design B): a week strip to pick the day, then the day's meals as
// cards with their curve and counted window ("Passt" checks it, "Zeitraum anpassen" opens the editor),
// sport sessions as cards of their own between them.

const sumOf = (items, key) => items.reduce((sum, item) => sum + (Number(item[key]) || 0), 0);

// What a meal is called beyond its kind: the dish, else the description of its own entry. Often long, so
// the card clamps it to two lines and the entry list below shows each in full.
function mealName(meal, items) {
  const own = items.find(item => item.id === meal.source_id) || items[0];
  return meal.dish?.name || own?.dish?.name || own?.description || "";
}

// One entry inside a meal or loose in the day: tap opens its card; a blood glucose reading can only be deleted.
function EntryRow({ item, data, ctx }) {
  const label = entryLabel(item, data);
  const amounts = [Number(item.carbs) ? num(item.carbs) + " g" : "", Number(item.insulin) ? num(item.insulin) + " " + UNIT.insulin : "",
    item.kind === "basal" ? num(item.basal_units) + " " + UNIT.insulin + (item.nightscout_synced ? "" : " · " + t("history.localOnly")) : "",
    item.kind === "finger" && item.glucose_mgdl != null ? num(item.glucose_mgdl, 0) + " mg/dL" : ""].filter(Boolean).join(" + ");
  const detail = item.description || item.notes || "";
  if (item.kind === "finger") {
    const remove = event => {
      if (!window.confirm(t("history.confirmFingerDelete", { time: timeOnly(item.at) }))) return;
      return act(event, async () => {
        await post("/api/treatment/delete", { id: item.id, day: ctx.day });
        toast(t("common.deletedFromNightscout"));
        await ctx.reload();
      });
    };
    return html`<li class="history-entry"><div class="history-entry-row"><time>${timeOnly(item.at)}</time><b>${label}</b><span>${amounts}</span>
      <button type="button" class="delete-treatment quiet" aria-label=${t("history.deleteFinger", { time: timeOnly(item.at) })} onClick=${remove}>${t("common.delete")}</button></div></li>`;
  }
  const editable = item.kind !== "other";
  return html`<li class="history-entry"><button type="button" class="history-entry-row edit-entry" data-id=${item.id} disabled=${!editable}
      aria-label=${t(editable ? "common.editAt" : "common.itemAt", { label, time: timeOnly(item.at) })} onClick=${() => ctx.onEdit(item.id)}>
    <time>${timeOnly(item.at)}</time><b>${label}<${FactorIcons} marks=${item} /></b><span>${amounts}</span>
    ${detail && html`<small>${detail}</small>`}</button></li>`;
}

function Calculation({ result }) {
  if (!result || !result.start) return html`<p class="hint">${result?.reason || t("history.noCalculation")}</p>`;
  const parts = [
    t("history.carbsInWindow", { grams: num(result.carbs_total) }),
    num(result.rapid_units_in_window, 2) + " " + UNIT.insulin,
    result.delta_mgdl != null ? "ΔG " + signedChange(result.delta_mgdl, 0) : "",
    Number(result.sport_effect_mgdl) ? t("sport.sport") + " " + signedChange(sportChange(result.sport_effect_mgdl), 0) : "",
    Number(result.prior_insulin_effect_mgdl) >= 1 ? t("history.priorInsulin", { mgdl: num(result.prior_insulin_effect_mgdl, 0) }) : "",
  ].filter(Boolean);
  return html`<p class="history-calc">${parts.join(" · ")}${result.reason && html`<br />${result.reason}`}</p>`;
}

// A session acting on this meal: its share and where it comes from; a tap opens the session.
function SportChip({ event, ctx }) {
  const text = t("sport.sport") + " " + signedMgdl(sportChange(event.effect_mgdl)) + " · " +
    (sportNames[event.kind] || t("sport.sport")) + " " + (event.previous_day ? t("history.previousDay") : timeOnly(event.at));
  const own = ctx.sessions.has(event.id);
  return own ? html`<button type="button" class="chip sport-chip" onClick=${() => ctx.onEdit(event.id)}>${text}</button>`
    : html`<span class="chip sport-chip">${text}</span>`;
}

function MealCard({ meal, items, data, ctx }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const range = mealWindow(meal);
  const calc = meal.calculation || {};
  const review = meal.review || {};
  const factor = calc.factor_units_per_ke;
  const entries = items.filter(item => item.kind !== "exercise");
  const running = !review.checkable;
  const task = (review.open || []).length > 0 && !review.checked && !meal.excluded;
  const name = mealName(meal, entries);
  const hasCurve = data.entries.some(point => {
    const t = new Date(point.at).getTime();
    return t >= range.start - 30 * MINUTE && t <= range.end + 30 * MINUTE;
  });
  async function check() {
    setSaving(true);
    try {
      await post("/api/analysis/window-check", { meal_id: meal.id, day: ctx.day, checked: !review.checked });
      await ctx.reload();
    } catch (error) { toast(error.message, true); }
    finally { setSaving(false); }
  }
  const listId = "meal-entries-" + meal.id;
  return html`<article class=${"history-meal" + (task ? " needs-check" : "") + (review.checked ? " is-checked" : "") + (meal.excluded ? " is-excluded" : "")} id=${"meal-" + meal.id}>
    <div class="history-meal-head">
      <div class="history-meal-title"><b class="meal-type">${names[meal.meal]}</b><time>${timeOnly(meal.at)}</time><${FactorIcons} marks=${meal.quality} /></div>
      <span class=${"history-factor" + (factor == null ? " is-missing" : "")}>${factor != null
        ? html`${factorNum(factor)} <small>${UNIT.factor}</small>` : running ? html`<small>${t("history.running")}</small>` : "—"}</span>
      ${name && html`<p class="history-meal-name" title=${name}>${name}</p>`}
    </div>
    <div class="history-meal-meta">
      <span>${t("history.mealTotals", { grams: num(sumOf(entries, "carbs"), 0), units: num(sumOf(entries, "insulin"), 1), unit: UNIT.insulin })}</span>
      <span class="day-meal-window">${timeOnly(range.start) + "–" + timeOnly(range.end) + (range.manual ? " · " + t("history.manual") : "")}</span>
      ${(meal.sport?.events || []).map(event => html`<${SportChip} key=${event.id} event=${event} ctx=${ctx} />`)}
      ${meal.vacation && html`<span class="history-meal-flag" title=${t("history.vacationTitle")}>${t("history.vacation")}</span>`}
      ${meal.excluded && html`<span class="history-meal-flag">${t("history.excluded")}</span>`}
    </div>
    ${task && html`<p class="history-task">${t("history.task", { reason: calc.reason || t("history.noFactor") })}</p>`}
    ${hasCurve ? html`<div class="meal-chart history-chart"><${MealChart} meal=${meal} data=${data} compact /></div>`
      : html`<p class="hint">${t("history.noCurve")}</p>`}
    <div class="history-actions">
      ${review.checkable && html`<button type="button" class=${"window-check" + (review.checked ? " is-checked" : " primary")} aria-pressed=${String(Boolean(review.checked))}
        disabled=${saving} onClick=${check}>${review.checked ? html`<${Icon} name="check" /> ${t("history.checked")}` : t("history.fits")}</button>`}
      ${hasCurve && html`<button type="button" class="open-window-editor" aria-label=${t("window.adjust")} onClick=${() => ctx.openEditor(meal)}>${t("window.short")}<span class="wide-only">${t("window.shortRest")}</span></button>`}
      <button type="button" class="history-more-entries" aria-expanded=${String(open)} aria-controls=${listId} onClick=${() => setOpen(!open)}>
        ${t("history.entriesCount", { count: entries.length })}</button>
    </div>
    <div id=${listId} class="history-meal-details" hidden=${!open}>
      <ul class="history-entries">${entries.map(item => html`<${EntryRow} key=${item.id} item=${item} data=${data} ctx=${ctx} />`)}</ul>
      <${Calculation} result=${calc} />
      ${meal.notes && html`<p class="meal-note">${meal.notes}</p>`}
      ${review.excludable && html`<div class="history-exclude"><${ExcludeButton} mealId=${meal.id} day=${ctx.day} excluded=${meal.excluded} onDone=${ctx.reload} /></div>`}
    </div></article>`;
}

// A sport session between the meals: what it does and which meals it reaches; a tap opens its card,
// the same one "Heute" opens (web/ui/sport.js).
function SportCard({ item, ctx }) {
  const summary = sportSummary(item);
  return html`<button type="button" class="history-sport edit-entry" data-id=${item.id} onClick=${() => ctx.onEdit(item.id)}
    aria-label=${t("common.openAt", { label: summary.title, time: timeOnly(item.at) })}>
    <time>${timeOnly(item.at)}</time><b>${summary.title}</b><span class="history-sport-effect">${summary.effect}</span>
    ${summary.parts.length > 0 && html`<span class="history-sport-parts">${summary.parts.map(part =>
      html`<span key=${part.key} class=${part.rest ? "is-rest" : ""}>${part.text}</span>`)}</span>`}</button>`;
}

// The day's entries grouped as the server does it (`meal_id`, day_view.block_meal_id): each main meal with
// its own entry, snacks, corrections and sport in its window; `rest` holds what belongs to no meal.
export function mealBlocks(data) {
  const blocks = new Map(data.meals.map(meal => [meal.id, { meal, items: [] }]));
  const rest = [];
  for (const item of data.timeline) {
    const block = blocks.get(item.meal_id);
    if (block) block.items.push(item);
    else rest.push(item);
  }
  return { blocks: [...blocks.values()], rest };
}

// One chronological list: meal cards, sport cards and the entries that belong to no meal.
function DayList({ data, ctx }) {
  if (!data.timeline.length && !data.meals.length) return html`<div class="empty">${t("history.noEntries")}</div>`;
  const { blocks, rest } = mealBlocks(data);
  const sessions = data.timeline.filter(item => item.kind === "exercise");
  const loose = rest.filter(item => item.kind !== "exercise");
  const rows = [...blocks.map(block => ({ t: new Date(block.meal.at).getTime(), block })),
    ...sessions.map(item => ({ t: new Date(item.at).getTime(), sport: item })),
    ...loose.map(item => ({ t: new Date(item.at).getTime(), item }))].sort((a, b) => a.t - b.t);
  // Entries outside meals that follow each other are one small list.
  const out = [];
  for (const row of rows) {
    if (row.item && out.length && out[out.length - 1].items) out[out.length - 1].items.push(row.item);
    else out.push(row.item ? { items: [row.item] } : row);
  }
  return out.map(row => row.block
    ? html`<${MealCard} key=${"meal-" + row.block.meal.id} meal=${row.block.meal} items=${row.block.items} data=${data} ctx=${ctx} />`
    : row.sport ? html`<${SportCard} key=${row.sport.id} item=${row.sport} ctx=${ctx} />`
    : html`<ul key=${"loose-" + row.items[0].id} class="history-loose history-entries">${row.items.map(item =>
        html`<${EntryRow} key=${item.id} item=${item} data=${data} ctx=${ctx} />`)}</ul>`);
}

// Work or free day for the breakfast factor; the entry form asks it before breakfast, Verlauf any time.
// `data` names the buttons' data attribute (data-<data>=work|free), `hint` the source of the current value.
export function DayTypeSwitch({ value, onChoose, disabled, label, data, hint, hintId, extra = "" }) {
  return html`<div class="entry-switch" role="group" aria-label=${label}>${[["work", t("dayType.workDay")], ["free", t("dayType.freeDay")]].map(([key, text]) =>
      html`<button type="button" class="day-type-option" ...${{ ["data-" + data]: key }} aria-pressed=${String(value === key)}
        disabled=${disabled} onClick=${() => onChoose(key)}>${text}</button>`)}</div>
    ${hintId && html`<p id=${hintId} class="hint">${(dayTypeSources[hint] || "") + extra}</p>`}`;
}

// The day's work/free switch in Verlauf's day head: short, the source as a tooltip.
function HistoryDayType({ data, day, reload }) {
  const [saving, setSaving] = useState(false);
  if (!data.day_type) return null;
  async function choose(value) {
    if (value === data.day_type) return;
    setSaving(true);
    try { await post("/api/analysis/day-type", { day, day_type: value }); await reload(); }
    catch (error) { toast(error.message, true); }
    finally { setSaving(false); }
  }
  const title = (dayTypeSources[data.day_type_source] || "") + (data.vacation && data.day_type_source !== "vacation" ? " · " + t("dayType.vacationDay") : "");
  return html`<div id="review-day-type" class="history-day-type" role="group" aria-label=${t("dayType.label")} title=${title}>${[["work", t("dayType.work")], ["free", t("dayType.free")]].map(([key, text]) =>
    html`<button type="button" class="day-type-option" data-review-day-type=${key} aria-pressed=${String(data.day_type === key)}
      disabled=${saving} onClick=${() => choose(key)}>${text}</button>`)}</div>`;
}

// Monday to Sunday of the shown day (GET /api/days/week): a dot per day (green: every meal checked,
// amber: a task open, grey: still to check) and its day type. Arrows move a week.
const plusDays = (date, days) => new Date(new Date(date + "T12:00:00").getTime() + days * 24 * 60 * MINUTE).toLocaleDateString("sv-SE");
// Monday of a date's week, the key of a week in the strip.
const weekOf = date => plusDays(date, -((new Date(date + "T12:00:00").getDay() + 6) % 7));

function WeekDays({ rows, day, onDay }) {
  if (!rows) return html`<div class="week-days" aria-hidden="true">${Array.from({ length: 7 }, (_, i) => html`<span key=${i} class="week-day is-empty"></span>`)}</div>`;
  return html`<div class="week-days" role="group" aria-label=${t("week.pickDay")}>${rows.map(row => {
    const state = row.future || !row.meals ? "none" : row.done ? "done" : row.tasks ? "open" : "todo";
    const label = dayDate(row.day, { weekday: "long", day: "numeric", month: "long" }) +
      (state === "none" ? "" : ", " + t("week.states." + state));
    return html`<button type="button" key=${row.day} class="week-day" data-day=${row.day} data-state=${state} aria-current=${row.day === day ? "date" : null}
      aria-label=${label} disabled=${row.future} onClick=${() => onDay(row.day)}>
      <span>${dayDate(row.day, { weekday: "short" }).replace(".", "")}</span><b>${Number(row.day.slice(8))}</b>
      <i class="week-dot" aria-hidden="true"></i>
      <em>${row.future ? "" : row.day_type === "free" ? t("dayType.free") : t("week.workShort")}</em></button>`;
  })}</div>`;
}

// The weeks as one row that follows the finger and snaps to a week (user request 2026-11-23): the shown week
// sits between the week before and after (none after the current week), each drawn from /api/days/week; when
// the row comes to rest on a neighbour, that week is shown and the row is re-centred on it.
function WeekStrip({ day, today, data, onDay }) {
  const [weeks, setWeeks] = useState({});
  const current = weekOf(day);
  const atToday = current === weekOf(today);
  const keys = [plusDays(current, -7), current, ...(atToday ? [] : [plusDays(current, 7)])];
  const index = 1;
  useEffect(() => {
    let live = true;
    // The shown week afresh with each loaded day, its neighbours once, so a swipe lands on a drawn week.
    for (const key of keys) {
      if (key !== current && weeks[key]) continue;
      api("/api/days/week?date=" + encodeURIComponent(key)).then(result => live && setWeeks(all => ({ ...all, [key]: result }))).catch(() => {});
    }
    return () => { live = false; };
  }, [day, data]);
  const shift = days => {
    const next = plusDays(day, days);
    onDay(next > today ? today : next);
  };
  const track = useRef(null);
  const settle = useRef(0);
  useLayoutEffect(() => {
    const el = track.current;
    if (el) el.scrollLeft = index * el.clientWidth;
  }, [current, atToday]);
  useEffect(() => () => clearTimeout(settle.current), []);
  const onScroll = () => {
    clearTimeout(settle.current);
    settle.current = setTimeout(() => {
      const el = track.current;
      if (!el || !el.clientWidth) return;
      const at = el.scrollLeft / el.clientWidth, snapped = Math.round(at);
      if (Math.abs(at - snapped) > 0.02 || snapped === index) return;
      shift((snapped - index) * 7);
    }, 120);
  };
  return html`<div class="week-strip">
    <div class="week-head"><button type="button" class="quiet week-prev" aria-label=${t("week.previous")} onClick=${() => shift(-7)}>‹</button>
      <span>${weeks[current] ? t("week.number", { week: weeks[current].week }) : ""}</span>
      <button type="button" class="quiet week-next" aria-label=${t("week.next")} disabled=${atToday} onClick=${() => shift(7)}>›</button></div>
    <div class="week-track" ref=${track} onScroll=${onScroll}>${keys.map(key => html`<div key=${key}
      class=${"week-panel" + (key === current ? " is-current" : "")} inert=${key !== current} aria-hidden=${key === current ? null : "true"}>
      <${WeekDays} rows=${weeks[key]?.days} day=${day} onDay=${onDay} /></div>`)}</div></div>`;
}

// ---- Window editor: the counted window of one meal, moved by dragging, tapping or nudging. ----
const SNAP_PIXELS = 16;
const readingValue = reading => reading?.value != null ? Number(reading.value) : null;

function WindowEditor({ meal, readings, data, day, onClose, onSaved }) {
  const dialog = useRef(null);
  const chart = useRef(null);
  const base = useMemo(() => mealWindow(meal), [meal]);
  const limits = windowLimits(data);
  const events = useMemo(() => chartEvents(data.treatments), [data]);
  const [ed, setEd] = useState({ start: base.start, end: base.end, active: "end", snapped: {} });
  const [view, setView] = useState(() => chartView(base, limits));
  const [summary, setSummary] = useState(null);
  const [saving, setSaving] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [ref, measured] = useWidth();
  const drag = useRef(null), tap = useRef(null);
  const layout = chartLayout(measured - 12);
  // A handle clamped to a limit can sit between minutes; it reads the minute it is in.
  const reading = t => readings.get(t) || readings.get(Math.floor(t / MINUTE) * MINUTE) || null;
  const range = { ...base, start: ed.start, end: ed.end };

  useEffect(() => {
    dialog.current.showModal();
    // iOS Safari may still scroll during a handle drag without a non-passive touch listener.
    const stop = event => { if (drag.current) event.preventDefault(); };
    chart.current.addEventListener("touchmove", stop, { passive: false });
    return () => chart.current?.removeEventListener("touchmove", stop);
  }, []);

  // The server counts the candidate window (same rule as the factor); the editor asks after each move.
  useEffect(() => {
    let live = true;
    const timer = setTimeout(async () => {
      try {
        const result = await post("/api/analysis/window/preview", { meal_id: meal.id, day,
          start: new Date(ed.start).toISOString(), end: new Date(ed.end).toISOString() });
        if (live) setSummary(result);
      } catch (error) {
        toast(error.message, true);
      }
    }, 120);
    return () => { live = false; clearTimeout(timer); };
  }, [ed.start, ed.end]);

  const minEnd = start => Math.max(start, base.mealAt) + limits.min;
  const bounds = (handle, state) => handle === "start"
    ? { min: Math.max(base.mealAt - limits.lead, view.from), max: base.mealAt + limits.lag }
    : { min: minEnd(state.start), max: Math.min(state.start + limits.max, view.to) };
  const snapTargets = () => {
    const targets = [{ t: base.autoStart, label: t("window.defaultStart") }, { t: base.autoEnd, label: t("window.defaultEnd") }];
    for (const event of events) {
      if (event.kind === "insulin") targets.push({ t: event.t, label: num(event.amount, 1) + " " + UNIT.insulin });
      if (event.kind === "carbs") targets.push({ t: event.t, label: t("window.carbs", { grams: num(event.amount, 0) }) });
      if (event.kind === "sport") targets.push({ t: event.t, label: t("window.sportStart", { label: event.label }) }, { t: event.end, label: t("window.sportEnd", { label: event.label }) });
    }
    return targets;
  };
  function place(state, handle, raw, snapTolerance = 0) {
    const { min, max } = bounds(handle, state);
    const clamp = value => Math.min(max, Math.max(min, value));
    let t = clamp(raw);
    let snapped = null;
    if (snapTolerance) {
      for (const target of snapTargets()) {
        const distance = Math.abs(target.t - t);
        if (target.t >= min && target.t <= max && distance <= snapTolerance && (!snapped || distance < Math.abs(snapped.t - t))) snapped = target;
      }
    }
    t = snapped ? snapped.t : clamp(Math.round(t / MINUTE) * MINUTE);
    if (snapped && state.snapped[handle]?.t !== snapped.t) navigator.vibrate?.(8);
    const next = { ...state, [handle]: t, snapped: { ...state.snapped, [handle]: snapped } };
    if (handle === "start") next.end = Math.min(Math.max(next.end, minEnd(next.start)), next.start + limits.max);
    return next;
  }
  const settle = next => { setEd(next); setView(chartView({ ...base, start: next.start, end: next.end }, limits)); };

  function pointerTime(event) {
    const rect = chart.current.querySelector("svg").getBoundingClientRect();
    const unitsPerPixel = layout.width / rect.width;
    const x = (event.clientX - rect.left) * unitsPerPixel;
    const msPerUnit = (view.to - view.from) / (layout.right - layout.left);
    return { t: view.from + (x - layout.left) * msPerUnit, msPerPixel: msPerUnit * unitsPerPixel };
  }
  function onPointerDown(event) {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    if (!chart.current.querySelector("svg")) return;
    const { t, msPerPixel } = pointerTime(event);
    let handle = event.target.closest(".window-handle")?.dataset.handle;
    if (!handle) {
      const distance = name => Math.abs(ed[name] - t) / msPerPixel;
      const nearest = distance("start") <= distance("end") ? "start" : "end";
      if (distance(nearest) <= 28) handle = nearest;
    }
    if (!handle) { tap.current = { x: event.clientX, y: event.clientY }; return; }
    event.preventDefault();
    drag.current = { handle, pointerId: event.pointerId };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
    setEd(state => ({ ...state, active: handle }));
  }
  function onPointerMove(event) {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId) return;
    event.preventDefault();
    const { t, msPerPixel } = pointerTime(event);
    setEd(state => place(state, current.handle, t, SNAP_PIXELS * msPerPixel));
  }
  function onPointerUp(event) {
    if (drag.current) {
      drag.current = null;
      setDragging(false);
      settle(ed);
      return;
    }
    const start = tap.current;
    tap.current = null;
    if (event.type !== "pointerup" || !start || Math.hypot(event.clientX - start.x, event.clientY - start.y) > 8) return;
    // A tap moves the selected handle, which works well on small touch screens.
    const { t, msPerPixel } = pointerTime(event);
    settle(place(ed, ed.active, t, SNAP_PIXELS * msPerPixel));
  }
  const nudge = (handle, minutes) => {
    const next = place(ed, handle, ed[handle] + minutes * MINUTE);
    settle({ ...next, active: handle, snapped: { ...next.snapped, [handle]: null } });
  };
  function onKeyDown(event) {
    const handle = event.target.closest?.(".window-handle")?.dataset.handle;
    if (!handle || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    nudge(handle, (event.key === "ArrowLeft" ? -1 : 1) * (event.shiftKey ? 5 : 1));
  }

  const isDefault = ed.start === base.autoStart && ed.end === base.autoEnd;
  const g0 = readingValue(reading(ed.start)), g1 = readingValue(reading(ed.end));
  async function save(reset = false) {
    if (!reset && !isDefault && (g0 == null || g1 == null)) {
      toast(t("window.needsCgm"), true);
      return;
    }
    setSaving(true);
    try {
      const body = reset || isDefault ? { meal_id: meal.id, day, reset: true }
        : { meal_id: meal.id, day, start: new Date(ed.start).toISOString(), end: new Date(ed.end).toISOString() };
      await post("/api/analysis/window", body);
      dialog.current.close();
      toast(body.reset ? t("window.resetDone") : t("window.saved"));
      await onSaved();
    } catch (error) {
      toast(error.message, true);
    } finally {
      setSaving(false);
    }
  }

  const active = reading(ed[ed.active]);
  const activeValue = readingValue(active);
  const minutes = Math.round((ed.end - ed.start) / MINUTE);
  const changed = ed.start !== base.start || ed.end !== base.end;
  const Cell = ({ label, value, detail }) => html`<div><span>${label}</span><strong>${value}</strong>${detail && html`<small>${detail}</small>`}</div>`;
  return html`<dialog id="window-dialog" ref=${dialog} aria-labelledby="window-dialog-title" onClose=${onClose}>
    <div class="section-head"><div><span id="window-meal-type" class="meal-type">${names[meal.meal] + " · " + t("common.clock", { time: timeOnly(meal.at) }) + " · " + t("window.carbs", { grams: num(meal.carbs) })}</span>
      <h2 id="window-dialog-title">${t("window.adjust")}</h2></div>
      <button id="close-window" type="button" class="quiet" aria-label=${t("common.close")} onClick=${() => dialog.current.close()}>×</button></div>
    <div id="window-readout" class="window-readout" data-tone=${active?.tone || "none"} aria-live="polite">
      <span class="readout-label">${(ed.active === "start" ? t("window.start") : t("window.end")) + " · " + t("common.clock", { time: timeOnly(ed[ed.active]) })}${ed.snapped[ed.active] && " · " + t("window.snapped", { label: ed.snapped[ed.active].label })}</span>
      <span class="readout-value"><strong>${activeValue == null ? "—" : num(activeValue, 0)}</strong><span class="readout-trend">${activeValue == null ? "" : trendSymbol(active.trend)}</span><small>mg/dL</small></span></div>
    <div class="window-handle-switch" role="group" aria-label=${t("window.pickHandle")}>${[["start", t("window.start")], ["end", t("window.end")]].map(([handle, label]) =>
      html`<button type="button" data-window-handle=${handle} aria-pressed=${String(ed.active === handle)} onClick=${() => setEd(state => ({ ...state, active: handle }))}>${label}</button>`)}</div>
    <div id="window-chart" class=${"window-chart" + (dragging ? " is-dragging" : "")} ref=${node => { chart.current = node; ref.current = node; }}
      onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${onPointerUp} onPointerCancel=${onPointerUp} onKeyDown=${onKeyDown}>
      ${measured > 0 && html`<${GlucoseChart} layout=${layout} range=${range} view=${view} entries=${data.entries} treatments=${data.treatments}
        bands=${data.target_bands} editor=${{ reading, active: ed.active, snapped: ed.snapped }} />`}</div>
    <div class="window-nudge" role="group" aria-label=${t("window.nudge")}>${[-5, -1, 1, 5].map(minutes =>
      html`<button type="button" data-nudge=${minutes} onClick=${() => nudge(ed.active, minutes)}>${minutes > 0 ? "+" + minutes : "−" + -minutes} min</button>`)}</div>
    <div id="window-summary" class="window-summary" aria-live="polite">
      <div class="window-facts">
        <${Cell} label=${t("window.start")} value=${timeOnly(ed.start)} detail=${g0 == null ? t("window.noCgm") : num(g0, 0) + " mg/dL"} />
        <${Cell} label=${html`${t("window.end")} <small>${t("window.exclusive")}</small>`} value=${timeOnly(ed.end)} detail=${g1 == null ? t("window.noCgm") : num(g1, 0) + " mg/dL"} />
        <${Cell} label="ΔG" value=${g0 == null || g1 == null ? "—" : signedChange(g1 - g0, 0)} />
        <${Cell} label=${t("window.duration")} value=${Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " min"} />
        <${Cell} label=${t("window.carbsInWindow")} value=${summary ? num(summary.carbs_g, 0) + " g" : "…"} />
        <${Cell} label=${t("window.rapidInsulin")} value=${summary ? num(summary.rapid_units, 1) + " " + UNIT.insulin : "…"} /></div>
      <p class="window-state">${isDefault ? t("window.automatic") : t("window.manual")}${changed && html` · <b>${t("window.unsaved")}</b>`}${(g0 == null || g1 == null) &&
        html` · <span class="window-warning">${t("window.needsCgm")}</span>`}</p></div>
    <div class="form-actions"><button id="save-window" class="primary" type="button" disabled=${saving} onClick=${() => save()}>${t("window.save")}</button>
      <button id="default-window" type="button" class="quiet" onClick=${() => settle({ ...ed, start: base.autoStart, end: base.autoEnd, snapped: {} })}>${t("window.default")}</button>
      <button id="cancel-window" type="button" class="quiet" onClick=${() => dialog.current.close()}>${t("common.cancel")}</button></div></dialog>`;
}

// The Verlauf page. `status` is "loading" while the day is fetched, "error" when it failed.
export function DayReview({ data, day, today, status, reload, onEdit, onAnalysisChanged, onDay }) {
  const [editor, setEditor] = useState(null);
  const head = html`<div class="section-head history-head"><h2>${t("nav.history")}</h2></div>
    <${WeekStrip} day=${day} today=${today} data=${data} onDay=${onDay} />`;
  if (!data || data.day !== day && status !== "ready") return html`<section class="panel history">${head}
    <div id="timeline" class="day-list" aria-live="polite"><span id="record-count" class="hint" hidden></span>
      <div class="empty">${status === "error" ? t("history.loadFailed") : t("history.loading")}</div></div></section>`;
  async function openEditor(meal) {
    try {
      const result = await api("/api/analysis/window-editor?" + new URLSearchParams({ day, meal_id: meal.id }));
      setEditor({ meal, readings: new Map(result.readings.map(row => [new Date(row.t).getTime(), row])) });
    } catch (error) {
      toast(error.message, true);
    }
  }
  const sessions = new Set(data.timeline.filter(item => item.kind === "exercise").map(item => item.id));
  const ctx = { day, reload, onEdit, onAnalysisChanged, openEditor, sessions };
  const review = data.review || {};
  const total = review.total ?? data.meals.length;
  const tasks = data.meals.filter(meal => (meal.review?.open || []).length && !meal.review.checked && !meal.excluded);
  return html`<section class="panel history">${head}
    <div class="history-day-head">
      <h3>${dayDate(day, { weekday: "short", day: "numeric", month: "numeric" })}</h3>
      ${total > 0 && html`<span id="review-summary" class=${"chip" + (review.done ? " is-ok" : "")}>${review.done
        ? html`<${Icon} name="check" /> ${t("history.dayChecked")}` : t("history.checkedOf", { done: total - (review.unchecked ?? 0), total })}</span>`}
      <span id="record-count" class="hint">${t("common.entries", { count: data.timeline.length })}</span>
      <${HistoryDayType} data=${data} day=${day} reload=${reload} />
    </div>
    ${tasks.length > 0 && html`<ul class="history-tasks">${tasks.map(meal => html`<li key=${meal.id}><a href=${"#meal-" + meal.id}>
      <b>${names[meal.meal] + " " + timeOnly(meal.at)}</b><span>${meal.calculation?.reason || t("history.noFactor")}</span></a></li>`)}</ul>`}
    ${(data.meals.flatMap(meal => meal.sport?.events || []).filter(event => event.previous_day)
      .filter((event, index, all) => all.findIndex(other => other.id === event.id) === index)).map(event => html`<p key=${event.id} class="history-previous-sport">
        ${t("history.previousSport", { sport: sportNames[event.kind] || t("sport.sport"), time: timeOnly(event.at) })}</p>`)}
    <div id="timeline" class="day-list history-list" aria-live="polite">${status === "error"
      ? html`<div class="empty">${t("history.loadFailed")}</div>` : html`<${DayList} data=${data} ctx=${ctx} />`}</div>
    ${!review.done && review.unchecked > 0 && review.checkable && html`<button id="check-day" type="button" class="primary check-day"
      onClick=${event => act(event, async () => { await post("/api/review/day", { day, checked: true }); await reload(); toast(t("history.dayChecked")); })}>
      <${Icon} name="check" /> ${t("history.markDay")}</button>`}
    ${editor && html`<${WindowEditor} key=${editor.meal.id} meal=${editor.meal} readings=${editor.readings} data=${data} day=${day}
      onClose=${() => setEditor(null)} onSaved=${async () => { await reload(); onAnalysisChanged(); }} />`}
  </section>`;
}
