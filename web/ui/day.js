// "Tag durchgehen": the cockpit for one day and the only place a day's data is maintained (page roles,
// AGENTS.md): the day's chart, how the model did ("Wirkungen"), the night, and one chronological list in
// which each main meal is a block with the entries the server grouped under it, its sport, exclusion and
// curve with the counted window ("Zeitraum anpassen" opens the window editor). Everything shown comes
// from the day payload (/api/day); actions post and then reload the day.
import { html, useState, useEffect, useRef, useMemo } from "./html.js";
import { MINUTE, api, post, toast, num, factorNum, signedChange, sportChange, timeOnly, trendSymbol, names, sportNames,
  CERTAINTY_LABELS, Icon, FactorIcons, DishBadge, PlaceBadge, CertaintyBadge, ExcludeButton } from "./lib.js";
import { DayChart, MealChart, mealWindow, chartEvents, chartView, chartLayout, windowLimits, glucosePoints, windowIncludes,
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

// A typed BZ effect: a whole number up to ±500 (0 only where it means "none").
function readEffect(input, allowZero) {
  const amount = Number(input.value);
  const ok = input.value !== "" && Number.isInteger(amount) && Math.abs(amount) <= 500 && (allowZero || amount !== 0);
  input.setCustomValidity(ok ? "" : allowZero
    ? "Bitte eine BZ-Wirkung zwischen −500 und +500 mg/dL eingeben (0 = kein Einfluss)."
    : "Bitte eine BZ-Wirkung zwischen −500 und +500 mg/dL eingeben, nicht 0.");
  if (!ok) input.reportValidity();
  return ok ? amount : null;
}

// Sport counts by itself with the share of each session acting in the meal window; the meal can take
// its own value or none, and "Automatisch" goes back to the sessions.
function SportReview({ sport, mealId, day, reload, onAnalysisChanged }) {
  const input = useRef(null);
  const status = sport.status || "auto";
  const auto = sportChange(sport.suggested_effect_mgdl);
  if (status === "auto" && !sport.suggested) return null;
  const own = status === "affected";
  const effect = own ? sportChange(sport.effect_mgdl) : status === "auto" ? auto : 0;
  const save = (event, next) => {
    const amount = next === "affected" ? readEffect(input.current, false) : 0;
    if (amount == null) return;
    return act(event, async () => {
      await post("/api/analysis/sport", { meal_id: mealId, day, status: next, effect_mgdl: sportChange(amount) });
      toast(next === "unaffected" ? "Kein Sporteinfluss vermerkt" : next === "auto" ? "Sport wieder automatisch" : "Sporteinfluss gespeichert");
      await reload();
      onAnalysisChanged();
    });
  };
  return html`<div class="sport-review sport-review-meal">
    <div class="sport-line"><div class=${"sport-state" + (status === "unaffected" ? "" : " is-applied")}>
      ${status === "unaffected" ? html`<span>Kein Sporteinfluss</span>`
        : html`<${Icon} name="check" /><span>Sport ${signedChange(effect, 0)}${own ? " · eigener Wert" : " · automatisch"}</span>`}
      ${(sport.events || []).length > 0 && html`<span class="sport-source">${sport.events.map(item =>
        (sportNames[item.kind] || "Sport") + " " + timeOnly(item.at) + " " + signedChange(sportChange(item.effect_mgdl), 0)).join(", ")}</span>`}</div></div>
    ${(status !== "auto" && sport.suggested || status !== "unaffected") && html`<div class="sport-actions">
      ${status !== "auto" && sport.suggested && html`<button type="button" class="sport-auto quiet" onClick=${event => save(event, "auto")}>Automatisch ${signedChange(auto, 0)}</button>`}
      ${status !== "unaffected" && html`<button type="button" class="sport-none quiet" onClick=${event => save(event, "unaffected")}>Kein Einfluss</button>`}</div>`}
    <details class="sport-custom"><summary>Eigenen Wert für diese Mahlzeit</summary>
      <div class="sport-custom-fields"><label><span>BZ-Wirkung in mg/dL (− senkt, + hebt)</span>
        <input class="sport-effect" type="number" min="-500" max="500" step="1" ref=${input} defaultValue=${effect || ""} /></label>
        <button type="button" class="sport-save" onClick=${event => save(event, "affected")}>Setzen</button></div></details></div>`;
}

// A sport session's own effect: what it lowers over the hours after it ends, default from its kind.
function SportSession({ item, day, reload, onAnalysisChanged }) {
  const input = useRef(null);
  const save = (event, reset) => {
    const amount = reset ? 0 : readEffect(input.current, true);
    if (amount == null) return;
    return act(event, async () => {
      await post("/api/analysis/sport-session", reset ? { id: item.id, day, reset: true } : { id: item.id, day, effect_mgdl: sportChange(amount) });
      toast(reset ? "Sporteinfluss wieder Standard" : "Sporteinfluss gespeichert");
      await reload();
      onAnalysisChanged();
    });
  };
  return html`<details class="sport-custom sport-session"><summary>Einfluss ändern</summary>
    <div class="sport-custom-fields"><label><span>BZ-Wirkung in mg/dL (− senkt, + hebt)</span>
      <input class="sport-effect" type="number" min="-500" max="500" step="1" ref=${input} defaultValue=${sportChange(item.sport_effect_mgdl)} /></label>
      <button type="button" class="sport-session-save" onClick=${event => save(event, false)}>Setzen</button>
      ${item.sport_effect_custom && html`<button type="button" class="sport-session-reset quiet" onClick=${event => save(event, true)}>Standard ${signedChange(sportChange(item.sport_effect_default_mgdl), 0)}</button>`}
    </div></details>`;
}

// Inside a meal, an entry names its place and carb certainty only where they differ from the meal head.
function EntryRow({ item, data, meal, ctx }) {
  const details = [
    Number(item.carbs) ? num(item.carbs) + " g KH" : "",
    Number(item.insulin) ? num(item.insulin) + " IE" : "",
    item.kind === "basal" ? num(item.basal_units) + " IE" + (item.nightscout_synced ? "" : " · nur lokal, noch nicht in Nightscout") : "",
    item.kind === "exercise" ? num(item.duration_minutes, 0) + " min" : "",
    item.kind === "exercise" && item.sport_effect_mgdl != null
      ? "Einfluss " + signedChange(sportChange(item.sport_effect_mgdl), 0) + (item.sport_effect_custom ? " (eigener Wert)" : "") : "",
    item.glucose_mgdl != null
      ? (item.glucose_at === item.at ? "BZ " : "BZ um " + timeOnly(item.glucose_at) + ": ") + num(item.glucose_mgdl, 0) + " mg/dL" : "",
  ].filter(Boolean);
  const label = entryLabel(item, data);
  const remove = event => {
    if (!window.confirm("BZ blutig um " + timeOnly(item.at) + " Uhr wirklich aus Nightscout löschen?")) return;
    return act(event, async () => {
      await post("/api/treatment/delete", { id: item.id, day: ctx.day });
      toast("Eintrag aus Nightscout gelöscht");
      await ctx.reload();
    });
  };
  const action = item.kind === "other" ? null : item.kind === "finger"
    ? html`<button type="button" class="delete-treatment quiet" aria-label=${"Blutige Messung um " + timeOnly(item.at) + " löschen"} onClick=${remove}>Löschen</button>`
    : html`<button type="button" class="edit-entry quiet" data-id=${item.id} aria-label=${(item.kind === "basal" ? "Basal" : label) + " um " + timeOnly(item.at) + " bearbeiten"}
        onClick=${() => ctx.onEdit(item.id)}>Bearbeiten</button>`;
  return html`<article class="timeline-item"><time>${timeOnly(item.at)}</time><div>
    <div class="entry-title"><strong>${label}</strong><${FactorIcons} marks=${item} />
      ${item.dish && (!meal || item.dish.id !== meal.dish?.id) && html`<${DishBadge} dish=${item.dish} />`}
      ${item.place && (!meal || item.place.id !== meal.place?.id) && html`<${PlaceBadge} place=${item.place} />`}
      ${meal && item.carbs_certainty && item.carbs_certainty !== meal.quality?.carbs_certainty && html`<${CertaintyBadge} level=${item.carbs_certainty} />`}</div>
    <p>${details.length ? details.map((part, index) => html`${index ? " · " : ""}<span>${part}</span>`) : item.eventType}</p>
    ${item.description && html`<small>${item.description}</small>`}
    ${item.notes && html`<small>${item.notes}</small>`}
    ${item.kind === "exercise" && item.sport_effect_mgdl != null && html`<${SportSession} item=${item} day=${ctx.day} reload=${ctx.reload} onAnalysisChanged=${ctx.onAnalysisChanged} />`}
    </div><div class="timeline-actions">${action}</div></article>`;
}

function Calculation({ result }) {
  if (!result || !result.start) return html`<div class="factor unavailable"><span>Insulinfaktor</span><strong>—</strong><small>${result?.reason || "Noch keine Berechnung"}</small></div>`;
  const valid = result.factor_units_per_ke != null;
  const summary = [
    num(result.carbs_total) + " g KH",
    num(result.rapid_units_in_window, 2) + " IE",
    ...(Number(result.prior_insulin_effect_mgdl) >= 1 ? ["früheres Insulin −" + num(result.prior_insulin_effect_mgdl, 0) + " mg/dL"] : []),
    "Ende " + timeOnly(result.end) + (result.manual_window ? " (manuell)" : ""),
  ].join(" · ");
  return html`<div class="calc-block"><div class=${"factor" + (valid ? "" : " unavailable")}><span>Insulinfaktor</span>
    <strong>${valid ? html`${factorNum(result.factor_units_per_ke)} <small>IE/KE</small>` : "—"}</strong>
    <small>${summary}</small>${result.reason && html`<small>${result.reason}</small>`}</div></div>`;
}

function MealBlock({ meal, items, data, ctx }) {
  const range = mealWindow(meal);
  const calc = meal.calculation || {};
  const check = (meal.review?.open || []).length > 0;
  const calculated = Boolean(meal.review?.calculated);
  const certainty = meal.quality?.carbs_certainty;
  const hasCurve = data.entries.some(point => {
    const t = new Date(point.at).getTime();
    return t >= range.start - 30 * MINUTE && t <= range.end + 30 * MINUTE;
  });
  return html`<article class=${"day-meal" + (check ? " needs-check" : "") + (meal.excluded ? " is-excluded" : "")} id=${"meal-" + meal.id}>
    <div class="day-meal-head">
      <div class="day-meal-title"><span class="meal-type">${names[meal.meal]}</span><${FactorIcons} marks=${meal.quality} />
        <span class="day-meal-window">${timeOnly(range.start) + "–" + timeOnly(range.end) + (range.manual ? " · manuell" : "")}</span></div>
      ${(meal.dish || meal.place || certainty || meal.vacation) && html`<div class="day-meal-tags"><${DishBadge} dish=${meal.dish} /><${PlaceBadge} place=${meal.place} />
        ${meal.vacation && html`<span class="place-badge vacation-badge" title="Urlaubstag: zählt nach dem Urlaub nicht in die Automatik">Urlaub</span>`}
        <${CertaintyBadge} level=${certainty} /></div>`}
      <div class="day-meal-result">${calculated
        ? html`<span class="day-meal-factor">${factorNum(calc.factor_units_per_ke)} <small>IE/KE</small></span>`
        : html`<span class="day-meal-status">${calc.reason || "Noch keine Berechnung"}</span>`}
        ${meal.review?.excludable && html`<${ExcludeButton} mealId=${meal.id} day=${ctx.day} excluded=${meal.excluded} onDone=${ctx.reload} />`}</div></div>
    ${items.map(item => html`<${EntryRow} key=${item.id} item=${item} data=${data} meal=${meal} ctx=${ctx} />`)}
    <${SportReview} sport=${meal.sport || {}} mealId=${meal.id} day=${ctx.day} reload=${ctx.reload} onAnalysisChanged=${ctx.onAnalysisChanged} />
    <details class="day-meal-details" open=${!calculated}><summary>Kurve und Zeitraum</summary>
      <${Calculation} result=${meal.calculation} />
      ${hasCurve ? html`<div class="meal-chart"><${MealChart} meal=${meal} data=${data} />
        <div class="meal-chart-foot"><span>${timeOnly(range.start) + "–" + timeOnly(range.end)} Uhr${range.manual && html` · <b>manuell</b>`}</span>
          <button type="button" class="open-window-editor" onClick=${() => ctx.openEditor(meal)}>Zeitraum anpassen</button></div></div>`
        : html`<p class="hint">Keine CGM-Kurve verfügbar.</p>`}
      ${meal.notes && html`<p class="meal-note">${meal.notes}</p>`}</details></article>`;
}

// One chronological list: each main meal as a block holding the entries the server grouped under it.
function DayList({ data, ctx }) {
  if (!data.timeline.length) return html`<div class="empty">Für diesen Tag liegen noch keine Einträge vor.</div>`;
  const blocks = new Map(data.meals.map(meal => [meal.id, { meal, items: [] }]));
  const rows = [];
  for (const item of data.timeline) {
    const block = blocks.get(item.meal_id);
    if (block) block.items.push(item);
    else rows.push({ t: new Date(item.at).getTime(), item });
  }
  return [...[...blocks.values()].map(block => ({ t: new Date(block.meal.at).getTime(), block })), ...rows]
    .sort((a, b) => a.t - b.t)
    .map(row => row.block
      ? html`<${MealBlock} key=${"meal-" + row.block.meal.id} meal=${row.block.meal} items=${row.block.items} data=${data} ctx=${ctx} />`
      : html`<${EntryRow} key=${row.item.id} item=${row.item} data=${data} ctx=${ctx} />`);
}

function ReviewSummary({ data }) {
  const review = data.review || {};
  return html`<div id="review-summary" class="review-summary" aria-live="polite">
    ${review.blocked > 0 && html`<a class="review-chip needs-check" href=${"#meal-" + review.first_open}>${review.blocked === 1 ? "1 Mahlzeit nicht berechenbar" : review.blocked + " Mahlzeiten nicht berechenbar"}</a>`}
    ${review.calculated > 0 && html`<span class="review-chip">${review.calculated === 1 ? "1 Faktor zählt" : review.calculated + " Faktoren zählen"}</span>`}
    ${review.excluded > 0 && html`<span class="review-chip">${review.excluded} ausgeklammert</span>`}
    ${data.meals.length > 0 && !review.blocked && html`<span class="review-chip is-clean"><${Icon} name="check" /> Tag geprüft</span>`}</div>`;
}

// Work or free day for the breakfast factor; the entry form asks it before breakfast, "Tag durchgehen" any time.
// `data` names the buttons' data attribute (data-<data>=work|free), `hint` the source of the current value.
export function DayTypeSwitch({ value, onChoose, disabled, label, data, hint, hintId, extra = "" }) {
  return html`<div class="entry-switch" role="group" aria-label=${label}>${[["work", "Arbeitstag"], ["free", "Freier Tag"]].map(([key, text]) =>
      html`<button type="button" class="day-type-option" ...${{ ["data-" + data]: key }} aria-pressed=${String(value === key)}
        disabled=${disabled} onClick=${() => onChoose(key)}>${text}</button>`)}</div>
    <p id=${hintId} class="hint">${(dayTypeSources[hint] || "") + extra}</p>`;
}

function ReviewDayType({ data, day, reload }) {
  const [saving, setSaving] = useState(false);
  if (!data.day_type) return null;
  async function choose(value) {
    setSaving(true);
    try { await post("/api/analysis/day-type", { day, day_type: value }); await reload(); }
    catch (error) { toast(error.message, true); }
    finally { setSaving(false); }
  }
  return html`<div id="review-day-type" class="review-day-type">
    <${DayTypeSwitch} label="Tagesart" value=${data.day_type} data="review-day-type" disabled=${saving} onChoose=${choose}
      hintId="review-day-type-hint" hint=${data.day_type_source} extra=${data.vacation && data.day_type_source !== "vacation" ? " · Urlaubstag" : ""} /></div>`;
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
  const points = useMemo(() => glucosePoints(data.entries), [data]);
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

// The whole review panel. `status` is "loading" while the day is fetched, "error" when it failed.
export function DayReview({ data, day, status, reload, onEdit, onAnalysisChanged }) {
  const [editor, setEditor] = useState(null);
  if (!data) return html`<section class="panel"><div class="section-head"><h2>Tag durchgehen</h2><span id="record-count" class="hint"></span></div>
    <div id="timeline" class="day-list" aria-live="polite">${status === "error" ? html`<div class="empty">Daten konnten nicht geladen werden.</div>`
      : html`<div class="empty">Nightscout-Daten werden geladen …</div>`}</div></section>`;
  async function openEditor(meal) {
    try {
      const result = await api("/api/analysis/window-editor?" + new URLSearchParams({ day, meal_id: meal.id }));
      setEditor({ meal, readings: new Map(result.readings.map(row => [new Date(row.t).getTime(), row])) });
    } catch (error) {
      toast(error.message, true);
    }
  }
  const ctx = { day, reload, onEdit, onAnalysisChanged, openEditor };
  const { dayStart, dayEnd } = dayBounds(data, day);
  return html`<section class="panel">
    <div class="section-head"><h2>Tag durchgehen</h2><span id="record-count" class="hint">${data.timeline.length + (data.timeline.length === 1 ? " Eintrag" : " Einträge")}</span></div>
    <${ReviewDayType} data=${data} day=${day} reload=${reload} />
    <${ReviewSummary} data=${data} />
    <${DayChart} id="review-chart" className="day-overview-chart review-chart" data=${data} view=${{ from: dayStart, to: dayEnd }} clipId="review-clip" />
    <${Effects} day=${day} data=${data} />
    ${status !== "loading" && html`<${Night} night=${data.night || { reason: "Nachtanalyse nicht verfügbar." }} />`}
    <div id="timeline" class="day-list" aria-live="polite">${status === "loading" ? html`<div class="empty">Nightscout-Daten werden geladen …</div>`
      : status === "error" ? html`<div class="empty">Daten konnten nicht geladen werden.</div>` : html`<${DayList} data=${data} ctx=${ctx} />`}</div>
    ${editor && html`<${WindowEditor} key=${editor.meal.id} meal=${editor.meal} readings=${editor.readings} data=${data} day=${day}
      onClose=${() => setEditor(null)} onSaved=${async () => { await reload(); onAnalysisChanged(); }} />`}
  </section>`;
}
