// "Verlauf" (formerly "Tag durchgehen", redesigned 2026-11-23): the place to page through past days and
// the only place a day's data is maintained (page roles, AGENTS.md). A week strip picks the day; each main
// meal is a card with the entries the server grouped under it, its curve and counted window ("Passt"
// checks it, "Zeitraum anpassen" opens the window editor) and exclusion; sport sessions are cards of their
// own; the day's chart, effects and night are folded away. Everything shown comes from the day payload
// (/api/day) and the week (/api/days/week); actions post and then reload the day.
import { html, useState, useEffect, useRef, useMemo } from "./html.js";
import { MINUTE, api, post, toast, num, factorNum, signedChange, sportChange, timeOnly, trendSymbol, names, sportNames,
  dayDate, Icon, FactorIcons, ExcludeButton } from "./lib.js";
import { sportSummary, signedMgdl } from "./sport.js";
import { DayChart, MealChart, mealWindow, chartEvents, chartView, chartLayout, windowLimits,
  GlucoseChart, useWidth } from "./charts.js";

const entryLabels = { finger: "BZ blutig", basal: "Basal", meal: "Mahlzeit", snack: "Snack", carbs: "Kohlenhydrate", correction: "Korrektur / Bolus", exercise: "Sport", other: "Weiterer Eintrag" };

export function entryLabel(item, data) {
  if (item.kind === "exercise") return sportNames[item.sport_kind] || "Sport";
  const meal = item.kind === "meal" ? data.meals.find(row => row.source_id === item.id) : null;
  return meal ? names[meal.meal] : entryLabels[item.kind];
}

export const dayTypeSources = {
  weekday: "Standard nach Wochentag",
  vacation: "Urlaub ist aktiv",
  manual: "Für diesen Tag manuell gewählt",
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
// sport sessions as cards of their own between them, and the day's curve, effects and night folded away.

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
  const amounts = [Number(item.carbs) ? num(item.carbs) + " g" : "", Number(item.insulin) ? num(item.insulin) + " IE" : "",
    item.kind === "basal" ? num(item.basal_units) + " IE" + (item.nightscout_synced ? "" : " · nur lokal") : "",
    item.kind === "finger" && item.glucose_mgdl != null ? num(item.glucose_mgdl, 0) + " mg/dL" : ""].filter(Boolean).join(" + ");
  const detail = item.description || item.notes || "";
  if (item.kind === "finger") {
    const remove = event => {
      if (!window.confirm("BZ blutig um " + timeOnly(item.at) + " Uhr wirklich aus Nightscout löschen?")) return;
      return act(event, async () => {
        await post("/api/treatment/delete", { id: item.id, day: ctx.day });
        toast("Eintrag aus Nightscout gelöscht");
        await ctx.reload();
      });
    };
    return html`<li class="history-entry"><div class="history-entry-row"><time>${timeOnly(item.at)}</time><b>${label}</b><span>${amounts}</span>
      <button type="button" class="delete-treatment quiet" aria-label=${"Blutige Messung um " + timeOnly(item.at) + " löschen"} onClick=${remove}>Löschen</button></div></li>`;
  }
  const editable = item.kind !== "other";
  return html`<li class="history-entry"><button type="button" class="history-entry-row edit-entry" data-id=${item.id} disabled=${!editable}
      aria-label=${label + " um " + timeOnly(item.at) + (editable ? " bearbeiten" : "")} onClick=${() => ctx.onEdit(item.id)}>
    <time>${timeOnly(item.at)}</time><b>${label}<${FactorIcons} marks=${item} /></b><span>${amounts}</span>
    ${detail && html`<small>${detail}</small>`}</button></li>`;
}

function Calculation({ result }) {
  if (!result || !result.start) return html`<p class="hint">${result?.reason || "Noch keine Berechnung"}</p>`;
  const parts = [
    num(result.carbs_total) + " g KH im Zeitraum",
    num(result.rapid_units_in_window, 2) + " IE",
    result.delta_mgdl != null ? "ΔG " + signedChange(result.delta_mgdl, 0) : "",
    Number(result.sport_effect_mgdl) ? "Sport " + signedChange(sportChange(result.sport_effect_mgdl), 0) : "",
    Number(result.prior_insulin_effect_mgdl) >= 1 ? "früheres Insulin −" + num(result.prior_insulin_effect_mgdl, 0) + " mg/dL" : "",
  ].filter(Boolean);
  return html`<p class="history-calc">${parts.join(" · ")}${result.reason && html`<br />${result.reason}`}</p>`;
}

// A session acting on this meal: its share and where it comes from; a tap opens the session.
function SportChip({ event, ctx }) {
  const text = "Sport " + signedMgdl(sportChange(event.effect_mgdl)) + " · " +
    (sportNames[event.kind] || "Sport") + (event.previous_day ? " vom Vortag" : " " + timeOnly(event.at));
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
        ? html`${factorNum(factor)} <small>IE/KE</small>` : running ? html`<small>läuft</small>` : "—"}</span>
      ${name && html`<p class="history-meal-name" title=${name}>${name}</p>`}
    </div>
    <div class="history-meal-meta">
      <span>${num(sumOf(entries, "carbs"), 0) + " g KH · " + num(sumOf(entries, "insulin"), 1) + " IE"}</span>
      <span class="day-meal-window">${timeOnly(range.start) + "–" + timeOnly(range.end) + (range.manual ? " · manuell" : "")}</span>
      ${(meal.sport?.events || []).map(event => html`<${SportChip} key=${event.id} event=${event} ctx=${ctx} />`)}
      ${meal.vacation && html`<span class="chip" title="Urlaubstag: zählt nach dem Urlaub nicht in die Automatik">Urlaub</span>`}
      ${meal.excluded && html`<span class="chip">ausgeklammert</span>`}
      ${review.checked && html`<span class="chip is-ok"><${Icon} name="check" /> geprüft</span>`}
    </div>
    ${task && html`<p class="history-task">${calc.reason || "Faktor nicht berechenbar"}. Zeitraum anpassen oder mit „Passt“ bestätigen.</p>`}
    ${hasCurve ? html`<div class="meal-chart history-chart"><${MealChart} meal=${meal} data=${data} compact /></div>`
      : html`<p class="hint">Keine CGM-Kurve verfügbar.</p>`}
    <div class="history-actions">
      ${review.checkable && html`<button type="button" class=${"window-check" + (review.checked ? " is-checked" : " primary")} aria-pressed=${String(Boolean(review.checked))}
        disabled=${saving} onClick=${check}>${review.checked ? html`<${Icon} name="check" /> Geprüft` : "Passt"}</button>`}
      ${hasCurve && html`<button type="button" class="open-window-editor" onClick=${() => ctx.openEditor(meal)}>Zeitraum anpassen</button>`}
      <button type="button" class="history-more-entries" aria-expanded=${String(open)} aria-controls=${listId} onClick=${() => setOpen(!open)}>
        ${"Einträge (" + entries.length + ")"}</button>
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
    aria-label=${summary.title + " um " + timeOnly(item.at) + " öffnen"}>
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
  if (!data.timeline.length && !data.meals.length) return html`<div class="empty">Für diesen Tag liegen noch keine Einträge vor.</div>`;
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
  return html`<div class="entry-switch" role="group" aria-label=${label}>${[["work", "Arbeitstag"], ["free", "Freier Tag"]].map(([key, text]) =>
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
  const title = (dayTypeSources[data.day_type_source] || "") + (data.vacation && data.day_type_source !== "vacation" ? " · Urlaubstag" : "");
  return html`<div id="review-day-type" class="history-day-type" role="group" aria-label="Tagesart" title=${title}>${[["work", "Arbeit"], ["free", "Frei"]].map(([key, text]) =>
    html`<button type="button" class="day-type-option" data-review-day-type=${key} aria-pressed=${String(data.day_type === key)}
      disabled=${saving} onClick=${() => choose(key)}>${text}</button>`)}</div>`;
}

// Monday to Sunday of the shown day (GET /api/days/week): a dot per day (green: every meal checked, amber:
// a task open, grey: still to check), its day type and a mark for sport. Arrows move a week.
function WeekStrip({ day, today, data, onDay }) {
  const [week, setWeek] = useState(null);
  useEffect(() => {
    let live = true;
    api("/api/days/week?date=" + encodeURIComponent(day)).then(result => live && setWeek(result)).catch(() => {});
    return () => { live = false; };
  }, [day, data]);
  const shift = days => {
    const next = new Date(new Date(day + "T12:00:00").getTime() + days * 24 * 60 * MINUTE).toLocaleDateString("sv-SE");
    onDay(next > today ? today : next);
  };
  const rows = week?.days || [];
  return html`<div class="week-strip">
    <div class="week-head"><button type="button" class="quiet week-prev" aria-label="Woche zurück" onClick=${() => shift(-7)}>‹</button>
      <span>${week ? "KW " + week.week : ""}</span>
      <button type="button" class="quiet week-next" aria-label="Woche vor" disabled=${rows.some(row => row.day === today)} onClick=${() => shift(7)}>›</button></div>
    <div class="week-days" role="group" aria-label="Tag wählen">${rows.map(row => {
      const state = row.future || !row.meals ? "none" : row.done ? "done" : row.tasks ? "open" : "todo";
      const label = dayDate(row.day, { weekday: "long", day: "numeric", month: "long" }) +
        ({ done: ", geprüft", open: ", Aufgabe offen", todo: ", noch zu prüfen", none: "" })[state] + (row.sport ? ", mit Sport" : "");
      return html`<button type="button" class="week-day" data-day=${row.day} data-state=${state} aria-current=${row.day === day ? "date" : null}
        aria-label=${label} disabled=${row.future} onClick=${() => onDay(row.day)}>
        <span>${dayDate(row.day, { weekday: "short" }).replace(".", "")}</span><b>${Number(row.day.slice(8))}</b>
        <i class="week-dot" aria-hidden="true"></i>
        <em>${row.future ? "" : row.day_type === "free" ? "Frei" : "Arb"}</em>${row.sport && html`<s class="week-sport" aria-hidden="true"></s>`}</button>`;
    })}</div></div>`;
}

function Night({ night }) {
  const startTime = night.start ? timeOnly(night.start) : null;
  const endTime = night.end ? timeOnly(night.end) : null;
  const Metric = ({ label, value, detail }) => html`<div class="night-metric"><span>${label}</span><strong>${value == null ? "—" : html`${num(value, 1)} <small>mg/dL</small>`}<small>${detail}</small></strong></div>`;
  const values = [night.start_glucose, night.end_glucose, night.minimum_glucose].some(value => value != null);
  return html`<section id="night-card" class="night-card" aria-live="polite">
    <div class="section-head"><h2>Nacht</h2><span id="night-window" class="hint">${startTime && endTime ? startTime + " bis " + endTime + " Uhr" : "Drei Stunden nach letzter Injektion bis Beginn Frühstück"}</span></div>
    ${values && html`<div id="night-values" class="night-values">
      <${Metric} label="Startwert" value=${night.start_glucose} detail=${startTime ? "3 h nach letzter Injektion · " + startTime : "3 h nach letzter Injektion"} />
      <${Metric} label="Endwert" value=${night.end_glucose} detail=${endTime ? "Beginn Frühstück · " + endTime : "Beginn Frühstück"} />
      <div class="night-metric"><span>Veränderung</span><strong>${signedChange(night.delta_mgdl)}</strong></div>
      <${Metric} label="Minimalwert" value=${night.minimum_glucose} detail=${night.minimum_at ? "um " + timeOnly(night.minimum_at) : "im Nachtfenster"} /></div>`}
    ${night.reason && html`<p id="night-reason" class="hint">${night.reason}</p>`}</section>`;
}

// "Wirkungen": measured glucose against the 3-h model, and how strongly carbs raise and insulin lowers.
function effectsSummaryText(data) {
  const summary = data.summary;
  if (!summary?.count) return data.reason || "Noch keine Werte für das Modell.";
  const parts = ["Modell im Mittel " + num(summary.mean_abs_mgdl, 0) + " mg/dL daneben"];
  if (summary.max_above) parts.push("BZ bis " + signedChange(summary.max_above.mgdl, 0) + " über Modell um " + timeOnly(summary.max_above.at));
  if (summary.max_below) parts.push("bis " + signedChange(summary.max_below.mgdl, 0) + " darunter um " + timeOnly(summary.max_below.at));
  return parts.join(" · ");
}

function EffectsChart({ data, width }) {
  const W = Math.max(300, Math.min(900, Math.round(width))), L = 36, R = W - 10;
  const top = 8, mid = 168, rateTop = 186, rateBottom = 290, H = 312;
  const from = new Date(data.from).getTime(), to = Math.max(new Date(data.to).getTime(), from + 60 * MINUTE);
  const rows = data.rows.map(row => ({ t: new Date(row.at).getTime(), g: row.glucose_mgdl == null ? null : Number(row.glucose_mgdl),
    m: row.model_mgdl == null ? null : Number(row.model_mgdl), up: Number(row.raising_per_hour), down: Number(row.lowering_per_hour) }));
  const values = rows.flatMap(row => [row.g, row.m]).filter(value => value != null);
  const gTop = Math.max(250, Math.ceil((Math.max(0, ...values) + 10) / 50) * 50);
  const gBottom = Math.min(50, Math.floor(Math.min(400, ...values) / 10) * 10);
  const rateMax = Math.max(60, ...rows.map(row => Math.max(row.up, row.down)));
  const x = t => L + (t - from) / (to - from) * (R - L);
  const y = g => mid - (Math.min(Math.max(g, gBottom), gTop) - gBottom) / (gTop - gBottom) * (mid - top);
  const zero = (rateTop + rateBottom) / 2;
  const r = v => v / rateMax * (rateBottom - rateTop) / 2;
  const line = key => {
    let d = "", open = false;
    for (const row of rows) {
      if (row[key] == null) { open = false; continue; }
      d += (open ? "L" : "M") + x(row.t).toFixed(1) + " " + y(row[key]).toFixed(1);
      open = true;
    }
    return d;
  };
  const area = (key, sign) => rows.length ? "M" + x(rows[0].t).toFixed(1) + " " + zero + rows.map(row =>
    "L" + x(row.t).toFixed(1) + " " + (zero - sign * r(row[key])).toFixed(1)).join("") + "L" + x(rows[rows.length - 1].t).toFixed(1) + " " + zero + "Z" : "";
  const grid = [];
  for (let g = Math.ceil(gBottom / 50) * 50; g <= gTop; g += 50) grid.push(g);
  const ticks = [];
  for (let t = Math.ceil(from / (180 * MINUTE)) * 180 * MINUTE; t <= to; t += 180 * MINUTE) ticks.push(t);
  return html`<svg class="window-chart-svg" viewBox=${"0 0 " + W + " " + H} role="img" aria-label="Gemessener BZ, Modell und Wirkungen">
    ${grid.map(g => html`<line class="grid" x1=${L} x2=${R} y1=${y(g).toFixed(1)} y2=${y(g).toFixed(1)} /><text x=${L - 6} y=${(y(g) + 4).toFixed(1)} text-anchor="end">${g}</text>`)}
    ${ticks.map(t => html`<line class="grid" x1=${x(t).toFixed(1)} x2=${x(t).toFixed(1)} y1=${top} y2=${rateBottom} /><text x=${x(t).toFixed(1)} y=${rateBottom + 16} text-anchor="middle">${timeOnly(t)}</text>`)}
    <path class="model" d=${line("m")} /><path class="measured" d=${line("g")} />
    <path class="raising" d=${area("up", 1)} /><path class="lowering" d=${area("down", -1)} />
    <line class="axis" x1=${L} x2=${R} y1=${zero} y2=${zero} />
    <text x=${L - 6} y=${rateTop + 8} text-anchor="end">+${Math.round(rateMax)}</text>
    <text x=${L - 6} y=${rateBottom} text-anchor="end">−${Math.round(rateMax)}</text>
    <text class="y-unit" x=${L} y=${rateTop - 4}>mg/dL pro h</text></svg>`;
}

function Effects({ day, data }) {
  const [effects, setEffects] = useState(null);
  const [ref, width] = useWidth();
  useEffect(() => {
    let live = true;
    api("/api/day/effects?date=" + encodeURIComponent(day))
      .then(result => live && setEffects(result))
      .catch(error => live && setEffects({ day, rows: [], reason: error.message }));
    return () => { live = false; };
  }, [day, data]);
  const shown = effects && effects.day === day;
  return html`<details id="effects-card" class="effects-card" open hidden=${!shown}>
    <summary>Wirkungen über den Tag <span id="effects-summary" class="hint">${shown ? effectsSummaryText(effects) : ""}</span></summary>
    <div id="effects-chart" class="effects-chart" ref=${ref}>${shown && width > 0 && (effects.rows.length
      ? html`<${EffectsChart} data=${effects} width=${width} />` : html`<p class="hint">Keine Daten für diesen Tag.</p>`)}</div>
    <p class="effects-legend"><span class="legend-measured">gemessen</span><span class="legend-model">Modell aus dem BZ 3 h vorher</span><span class="legend-raising">hebt: KH, Fett/Eiweiß</span><span class="legend-lowering">senkt: Insulin</span></p></details>`;
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
    const targets = [{ t: base.autoStart, label: "Standardbeginn" }, { t: base.autoEnd, label: "Standardende" }];
    for (const event of events) {
      if (event.kind === "insulin") targets.push({ t: event.t, label: num(event.amount, 1) + " IE" });
      if (event.kind === "carbs") targets.push({ t: event.t, label: num(event.amount, 0) + " g KH" });
      if (event.kind === "sport") targets.push({ t: event.t, label: event.label + " Beginn" }, { t: event.end, label: event.label + " Ende" });
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
      toast("An Beginn und Ende wird ein CGM-Wert benötigt.", true);
      return;
    }
    setSaving(true);
    try {
      const body = reset || isDefault ? { meal_id: meal.id, day, reset: true }
        : { meal_id: meal.id, day, start: new Date(ed.start).toISOString(), end: new Date(ed.end).toISOString() };
      await post("/api/analysis/window", body);
      dialog.current.close();
      toast(body.reset ? "Standardzeitraum wiederhergestellt" : "Zeitraum gespeichert");
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
    <div class="section-head"><div><span id="window-meal-type" class="meal-type">${names[meal.meal] + " · " + timeOnly(meal.at) + " Uhr · " + num(meal.carbs) + " g KH"}</span>
      <h2 id="window-dialog-title">Zeitraum anpassen</h2></div>
      <button id="close-window" type="button" class="quiet" aria-label="Schließen" onClick=${() => dialog.current.close()}>×</button></div>
    <div id="window-readout" class="window-readout" data-tone=${active?.tone || "none"} aria-live="polite">
      <span class="readout-label">${(ed.active === "start" ? "Beginn" : "Ende") + " · " + timeOnly(ed[ed.active]) + " Uhr"}${ed.snapped[ed.active] && " · eingerastet: " + ed.snapped[ed.active].label}</span>
      <span class="readout-value"><strong>${activeValue == null ? "—" : num(activeValue, 0)}</strong><span class="readout-trend">${activeValue == null ? "" : trendSymbol(active.trend)}</span><small>mg/dL</small></span></div>
    <div class="window-handle-switch" role="group" aria-label="Griff auswählen">${[["start", "Beginn"], ["end", "Ende"]].map(([handle, label]) =>
      html`<button type="button" data-window-handle=${handle} aria-pressed=${String(ed.active === handle)} onClick=${() => setEd(state => ({ ...state, active: handle }))}>${label}</button>`)}</div>
    <div id="window-chart" class=${"window-chart" + (dragging ? " is-dragging" : "")} ref=${node => { chart.current = node; ref.current = node; }}
      onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${onPointerUp} onPointerCancel=${onPointerUp} onKeyDown=${onKeyDown}>
      ${measured > 0 && html`<${GlucoseChart} layout=${layout} range=${range} view=${view} entries=${data.entries} treatments=${data.treatments}
        bands=${data.target_bands} editor=${{ reading, active: ed.active, snapped: ed.snapped }} />`}</div>
    <div class="window-nudge" role="group" aria-label="Ausgewählten Griff verschieben">${[-5, -1, 1, 5].map(minutes =>
      html`<button type="button" data-nudge=${minutes} onClick=${() => nudge(ed.active, minutes)}>${minutes > 0 ? "+" + minutes : "−" + -minutes} min</button>`)}</div>
    <div id="window-summary" class="window-summary" aria-live="polite">
      <div class="window-facts">
        <${Cell} label="Beginn" value=${timeOnly(ed.start)} detail=${g0 == null ? "kein CGM-Wert" : num(g0, 0) + " mg/dL"} />
        <${Cell} label=${html`Ende <small>(exkl.)</small>`} value=${timeOnly(ed.end)} detail=${g1 == null ? "kein CGM-Wert" : num(g1, 0) + " mg/dL"} />
        <${Cell} label="ΔG" value=${g0 == null || g1 == null ? "—" : signedChange(g1 - g0, 0)} />
        <${Cell} label="Dauer" value=${Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " min"} />
        <${Cell} label="KH im Fenster" value=${summary ? num(summary.carbs_g, 0) + " g" : "…"} />
        <${Cell} label="Schnelles Insulin" value=${summary ? num(summary.rapid_units, 1) + " IE" : "…"} /></div>
      <p class="window-state">${isDefault ? "Automatischer Standardzeitraum" : "Manueller Zeitraum"}${changed && html` · <b>nicht gespeichert</b>`}${(g0 == null || g1 == null) &&
        html` · <span class="window-warning">An Beginn und Ende wird ein CGM-Wert benötigt.</span>`}</p></div>
    <div class="form-actions"><button id="save-window" class="primary" type="button" disabled=${saving} onClick=${() => save()}>Zeitraum speichern</button>
      <button id="default-window" type="button" class="quiet" onClick=${() => settle({ ...ed, start: base.autoStart, end: base.autoEnd, snapped: {} })}>Standard</button>
      <button id="cancel-window" type="button" class="quiet" onClick=${() => dialog.current.close()}>Abbrechen</button></div></dialog>`;
}

// The Verlauf page. `status` is "loading" while the day is fetched, "error" when it failed.
export function DayReview({ data, day, today, status, reload, onEdit, onAnalysisChanged, onDay, onRefresh }) {
  const [editor, setEditor] = useState(null);
  const [more, setMore] = useState(false);
  const head = html`<div class="section-head history-head"><h2>Verlauf</h2>
    <button type="button" class="quiet history-refresh" aria-label="Aktualisieren" title="Aktualisieren" onClick=${onRefresh}>↻</button></div>
    <${WeekStrip} day=${day} today=${today} data=${data} onDay=${onDay} />`;
  if (!data || data.day !== day && status !== "ready") return html`<section class="panel history">${head}
    <div id="timeline" class="day-list" aria-live="polite"><span id="record-count" class="hint" hidden></span>
      <div class="empty">${status === "error" ? "Daten konnten nicht geladen werden." : "Daten werden geladen …"}</div></div></section>`;
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
  const { dayStart, dayEnd } = dayBounds(data, day);
  const review = data.review || {};
  const meals = data.meals.length;
  const tasks = data.meals.filter(meal => (meal.review?.open || []).length && !meal.review.checked && !meal.excluded);
  return html`<section class="panel history">${head}
    <div class="history-day-head">
      <h3>${dayDate(day, { weekday: "short", day: "numeric", month: "numeric" })}</h3>
      ${meals > 0 && html`<span id="review-summary" class=${"chip" + (review.done ? " is-ok" : "")}>${review.done
        ? html`<${Icon} name="check" /> Tag geprüft` : (review.checked || 0) + " von " + meals + " geprüft"}</span>`}
      <span id="record-count" class="hint">${data.timeline.length + (data.timeline.length === 1 ? " Eintrag" : " Einträge")}</span>
      <${HistoryDayType} data=${data} day=${day} reload=${reload} />
    </div>
    ${tasks.length > 0 && html`<ul class="history-tasks">${tasks.map(meal => html`<li key=${meal.id}><a href=${"#meal-" + meal.id}>
      <b>${names[meal.meal] + " " + timeOnly(meal.at)}</b><span>${meal.calculation?.reason || "Faktor nicht berechenbar"}</span></a></li>`)}</ul>`}
    ${(data.meals.flatMap(meal => meal.sport?.events || []).filter(event => event.previous_day)
      .filter((event, index, all) => all.findIndex(other => other.id === event.id) === index)).map(event => html`<p key=${event.id} class="history-previous-sport">
        ${(sportNames[event.kind] || "Sport") + " vom Vortag, " + timeOnly(event.at) + " Uhr: wirkt noch in diesen Tag"}</p>`)}
    <div id="timeline" class="day-list history-list" aria-live="polite">${status === "error"
      ? html`<div class="empty">Daten konnten nicht geladen werden.</div>` : html`<${DayList} data=${data} ctx=${ctx} />`}</div>
    <details class="history-more" open=${more} onToggle=${event => setMore(event.currentTarget.open)}>
      <summary>BZ-Tag, Wirkungen, Nacht</summary>
      ${more && html`<${DayChart} id="review-chart" className="day-overview-chart review-chart" data=${data} view=${{ from: dayStart, to: dayEnd }} clipId="review-clip" />
        <${Effects} day=${day} data=${data} />
        <${Night} night=${data.night || { reason: "Nachtanalyse nicht verfügbar." }} />`}</details>
    ${editor && html`<${WindowEditor} key=${editor.meal.id} meal=${editor.meal} readings=${editor.readings} data=${data} day=${day}
      onClose=${() => setEditor(null)} onSaved=${async () => { await reload(); onAnalysisChanged(); }} />`}
  </section>`;
}
