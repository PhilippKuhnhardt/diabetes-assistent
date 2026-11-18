// One saved entry as a card (concept 2026-11-18), instead of the old "Eintrag bearbeiten" form.
// Meals, snacks, carbs and corrections show their numbers as tiles (KH with a small certainty
// meter, IE, the meal's factor), what was eaten, the meal marks and a grouped list for time, place,
// dish and note; "Nachbessern" continues the agent chat. Sport has its kind, start, duration, effect
// (standard, own value, none) and effect duration; basal its units. Saving and deleting stay in
// app.js (onSave, onDelete), which knows the day and redraws it.
import { html, useState, useEffect } from "./html.js";
import { CERTAINTY_LEVELS, CertaintySlider, FactorChip } from "./agent-chat.js";

const SPORT_KINDS = { jogging: "Joggen", gym: "Gym", light_cardio: "Leichtes Cardio", racing: "Rennsport" };
const KIND_TITLES = { meal: "Mahlzeit", snack: "Snack", carbs: "Kohlenhydrate", correction: "Korrektur", exercise: "Sport", basal: "Basal" };
const fmt = (value, digits = 1) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: digits });
const parse = text => Number(String(text ?? "").replace(",", ".").trim());
const blank = value => value == null || Number(value) === 0 ? "" : fmt(value);
const hours = minutes => fmt(minutes / 60) + " h";
const signed = value => (value > 0 ? "+" : value < 0 ? "−" : "±") + fmt(Math.abs(value), 0);

function Meter({ level, onClick }) {
  return html`<button type="button" class="meter" data-level=${level == null ? "" : CERTAINTY_LEVELS[level]} onClick=${onClick}
    aria-label=${"KH-Sicherheit ändern: " + (level == null ? "nicht gesetzt" : ["unsicher", "halbwegs", "sicher"][level])}>
    ${[0, 1, 2].map(index => html`<i class=${level != null && index <= level ? "on" : ""}></i>`)}</button>`;
}

const Row = ({ label, children, hint }) => html`<label class="card-row"><span>${label}</span>${children}${hint && html`<small>${hint}</small>`}</label>`;

function Segments({ value, options, onChange, label }) {
  return html`<div class="segments" role="group" aria-label=${label}>${options.map(([key, text]) => html`
    <button type="button" aria-pressed=${value === key ? "true" : "false"} onClick=${() => onChange(key)}>${text}</button>`)}</div>`;
}

function initial(item) {
  const sport = item.kind === "exercise";
  const effect = sport ? -Number(item.sport_effect_mgdl || 0) : 0;
  return {
    at: item.time, carbs: blank(item.carbs), insulin: blank(item.insulin),
    level: CERTAINTY_LEVELS.includes(item.carbs_certainty) ? CERTAINTY_LEVELS.indexOf(item.carbs_certainty) : null,
    fat: Boolean(item.fat_protein), fast: Boolean(item.fast),
    description: item.description || "", dish: item.dish?.name || "", place: item.place ? String(item.place.id) : "",
    notes: item.notes || "", session: null,
    sportKind: item.sport_kind || "jogging", duration: item.duration_minutes ? String(Number(item.duration_minutes)) : "",
    effectMode: !sport || !item.sport_effect_custom ? "standard" : effect === 0 ? "none" : "own",
    effect: item.sport_effect_custom && effect !== 0 ? String(effect) : "",
    minutesMode: item.sport_minutes_custom ? "own" : "standard",
    hours: item.sport_minutes_custom ? fmt(item.sport_minutes / 60) : "",
    units: item.kind === "basal" ? fmt(item.basal_units) : "",
  };
}

export function EntryCard({ item, dateLabel, factor, places, dishes, sportDefaults, agentInfo, api, toast, instant, onSave, onDelete, onClose }) {
  const [v, setV] = useState(() => initial(item));
  const [showCertainty, setShowCertainty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [photos, setPhotos] = useState([]);
  const set = patch => setV(current => ({ ...current, ...patch }));
  const kind = item.kind;
  const sport = kind === "exercise";
  const basal = kind === "basal";
  const food = !sport && !basal;
  const carbs = parse(v.carbs) || 0;
  const eats = food && carbs > 0;
  const title = item.title || KIND_TITLES[kind] || "Eintrag";

  // Photos of the agent's estimate head the card.
  useEffect(() => {
    if (!food || !item.event_id) return;
    let live = true;
    api("/api/agent/session?event_id=" + encodeURIComponent(item.event_id)).then(data => {
      const found = data.session;
      if (live && found?.photos) setPhotos(Array.from({ length: found.photos }, (_, index) =>
        "/api/agent/photo?session=" + encodeURIComponent(found.session_id) + "&n=" + index));
    }).catch(() => {});
    return () => { live = false; };
  }, []);

  function refine() {
    if (!agentInfo?.available) {
      toast("Essens-Agent ist nicht eingerichtet: in den Einstellungen Anbieter und Modell wählen", true);
      return;
    }
    window.mealChat.open({
      target: "edit", info: agentInfo, api, toast, eventId: item.event_id || null,
      initialText: v.description, previousCarbs: carbs || "",
      context: () => ({ at: instant(v.at), placeId: v.place || null }),
      onApply: result => {
        set({ carbs: String(result.carbs), level: CERTAINTY_LEVELS.indexOf(result.certainty), fat: result.fat_protein,
              fast: result.fast, dish: result.estimate.dish?.name || "", description: result.estimate.description,
              session: result.session });
      },
    });
  }

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await onSave({ ...v, carbs, insulin: parse(v.insulin) || 0, units: parse(v.units) || 0, duration: parse(v.duration) || 0,
        certainty: v.level == null ? undefined : CERTAINTY_LEVELS[v.level],
        effect: v.effectMode === "own" ? parse(v.effect) : v.effectMode === "none" ? 0 : null,
        minutes: v.minutesMode === "own" ? Math.round(parse(v.hours) * 60) : null });
    } catch (error) { toast(error.message, true); }
    setBusy(false);
  }

  async function remove() {
    if (!armed) { setArmed(true); return; }
    setBusy(true);
    try { await onDelete(); } catch (error) { toast(error.message, true); setArmed(false); }
    setBusy(false);
  }

  const defaults = sportDefaults[v.sportKind] || {};
  return html`<form class="entry-sheet" onSubmit=${save}>
    <div class="section-head card-head">
      <div><h2 id="edit-dialog-title">${title}</h2><span class="card-date">${dateLabel}</span></div>
      <button type="button" class="quiet" aria-label="Schließen" onClick=${onClose}>×</button>
    </div>
    <div class="card-body">
      ${photos.length > 0 && html`<div class=${"card-photos" + (photos.length > 1 ? " many" : "")}>
        ${photos.map((photo, index) => html`<img src=${photo} alt=${"Foto " + (index + 1)} />`)}</div>`}

      ${food && html`<div class=${"card-tiles" + (kind === "meal" ? "" : " two")}>
        <label class="tile"><small>KH</small><span class="tile-value"><input inputmode="decimal" aria-label="Kohlenhydrate in g"
          value=${v.carbs} placeholder="0" onInput=${event => set({ carbs: event.currentTarget.value })} /><small>g</small></span>
          ${eats && html`<${Meter} level=${v.level} onClick=${event => { event.preventDefault(); setShowCertainty(!showCertainty); }} />`}</label>
        <label class="tile"><small>Insulin</small><span class="tile-value"><input inputmode="decimal" aria-label="Insulin in IE"
          value=${v.insulin} placeholder="0" onInput=${event => set({ insulin: event.currentTarget.value })} /><small>IE</small></span></label>
        ${kind === "meal" && html`<div class="tile"><small>Faktor</small><span class="tile-value"><b>${factor == null ? "–" : fmt(factor, 2)}</b>
          <small>IE/KE</small></span></div>`}
      </div>
      ${eats && showCertainty && html`<div class="card-certainty"><${CertaintySlider} level=${v.level ?? 1} onChange=${level => set({ level })} /></div>`}`}

      ${eats && html`<div class="card-food">
        <textarea rows="4" maxlength="1000" placeholder="Was gab es?" aria-label="Beschreibung" value=${v.description}
          onInput=${event => set({ description: event.currentTarget.value })}></textarea>
        <div class="entry-chips">
          <${FactorChip} name="fat_protein" label="Fettreich" title="Fett- oder eiweißreiche Mahlzeit" checked=${v.fat} onChange=${fat => set({ fat })} />
          <${FactorChip} name="fast" label="Schnell" title="Wirkt schnell (z. B. Saft, Weißbrot)" checked=${v.fast} onChange=${fast => set({ fast })} />
          <button type="button" class="chip agent-chip card-refine" onClick=${refine}>Nachbessern</button>
        </div>
      </div>`}

      ${sport && html`<div class="card-list">
        <${Row} label="Sportart"><select value=${v.sportKind} onChange=${event => set({ sportKind: event.currentTarget.value })}>
          ${Object.entries(SPORT_KINDS).map(([key, name]) => html`<option value=${key}>${name}</option>`)}</select><//>
        <${Row} label="Start"><input type="time" step="60" required value=${v.at} onInput=${event => set({ at: event.currentTarget.value })} /><//>
        <${Row} label="Dauer"><span class="card-number"><input inputmode="numeric" required value=${v.duration}
          onInput=${event => set({ duration: event.currentTarget.value })} aria-label="Dauer in Minuten" /><small>min</small></span><//>
      </div>
      <div class="card-group">
        <div class="card-group-head"><b>Wirkung auf den BZ</b>
          <small>${v.effectMode === "standard" ? "Standard " + signed(-Number(defaults.effect || 0)) + " mg/dL"
            : v.effectMode === "none" ? "Kein Einfluss" : "− senkt, + hebt"}</small></div>
        <${Segments} label="Wirkung" value=${v.effectMode} onChange=${effectMode => set({ effectMode })}
          options=${[["standard", "Standard"], ["own", "Eigener"], ["none", "Keine"]]} />
        ${v.effectMode === "own" && html`<span class="card-number wide"><input inputmode="numeric" required aria-label="Wirkung in mg/dL"
          placeholder=${signed(-Number(defaults.effect || 0))} value=${v.effect} onInput=${event => set({ effect: event.currentTarget.value })} /><small>mg/dL</small></span>`}
        ${v.effectMode !== "none" && html`
          <div class="card-group-head"><b>Wirkdauer nach dem Ende</b>
            <small>${v.minutesMode === "standard" ? "Standard " + hours(defaults.minutes || 300) : "30 min bis 24 h"}</small></div>
          <${Segments} label="Wirkdauer" value=${v.minutesMode} onChange=${minutesMode => set({ minutesMode })}
            options=${[["standard", "Standard"], ["own", "Eigene"]]} />
          ${v.minutesMode === "own" && html`<span class="card-number wide"><input inputmode="decimal" required aria-label="Wirkdauer in Stunden"
            placeholder=${fmt((defaults.minutes || 300) / 60)} value=${v.hours} onInput=${event => set({ hours: event.currentTarget.value })} /><small>h</small></span>`}`}
      </div>`}

      ${basal && html`<div class="card-tiles one"><label class="tile"><small>Basal</small><span class="tile-value"><input inputmode="decimal"
        required aria-label="Basal in IE" value=${v.units} onInput=${event => set({ units: event.currentTarget.value })} /><small>IE</small></span></label></div>`}

      <div class="card-list">
        ${!sport && html`<${Row} label="Zeit"><input type="time" step="60" required value=${v.at} onInput=${event => set({ at: event.currentTarget.value })} /><//>`}
        ${eats && places.length > 0 && html`<${Row} label="Ort"><select value=${v.place} onChange=${event => set({ place: event.currentTarget.value })}>
          <option value="">Kein Ort</option>${places.map(place => html`<option value=${String(place.id)}>${place.name}</option>`)}</select><//>`}
        ${eats && html`<${Row} label="Gericht"><input type="text" maxlength="60" list="card-dish-options" autocomplete="off" placeholder="ohne"
          value=${v.dish} onInput=${event => set({ dish: event.currentTarget.value })} />
          <datalist id="card-dish-options">${dishes.map(dish => html`<option value=${dish.name}></option>`)}</datalist><//>`}
        <${Row} label="Notiz"><input type="text" maxlength="500" placeholder="optional" value=${v.notes}
          onInput=${event => set({ notes: event.currentTarget.value })} /><//>
      </div>
      <button type="button" class=${"card-delete" + (armed ? " armed" : "")} disabled=${busy} onClick=${remove}>
        ${armed ? "Wirklich löschen?" : "Eintrag löschen"}</button>
    </div>
    <div class="card-foot"><button class="primary" type="submit" disabled=${busy}>${busy ? "Speichert …" : "Speichern"}</button></div>
  </form>`;
}
