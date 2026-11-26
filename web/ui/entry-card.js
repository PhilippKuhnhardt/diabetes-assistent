// One saved entry as a card (concept 2026-11-18), instead of the old "Eintrag bearbeiten" form.
// Meals, snacks, carbs and corrections show their numbers as tiles (KH with a small certainty
// meter, IE, the meal's factor), what was eaten, the meal marks and a grouped list for time, place,
// dish and note; "Nachbessern" continues the agent chat. Sport has its kind, start, duration, effect
// (standard, own value, none) and effect duration; basal its units. Saving and deleting stay in
// app.js (onSave, onDelete), which knows the day and redraws it.
import { html, useState, useEffect } from "./html.js";
import { api, toast, num, factorNum, sportNames, UNIT } from "./lib.js";
import { t } from "./i18n.js";
import { mealChat } from "./main.js";
import { CERTAINTY_LEVELS, CertaintySlider, FactorChips, SurchargeChoice } from "./meal-fields.js";
import { SportEffectFields, SportShares, sportFields, sportValues } from "./sport.js";

const KIND_TITLES = Object.fromEntries(["meal", "snack", "carbs", "correction", "exercise", "basal"].map(kind => [kind, t("card.kinds." + kind)]));
const fmt = (value, digits = 1) => num(value, digits);
const parse = text => Number(String(text ?? "").replace(",", ".").trim());
const blank = value => value == null || Number(value) === 0 ? "" : fmt(value);

function Meter({ level, onClick }) {
  return html`<button type="button" class="meter" data-level=${level == null ? "" : CERTAINTY_LEVELS[level]} onClick=${onClick}
    aria-label=${t("card.certaintyChange", { level: level == null ? t("card.certaintyUnset") : t("card.certaintyLevels." + CERTAINTY_LEVELS[level]) })}>
    ${[0, 1, 2].map(index => html`<i class=${level != null && index <= level ? "on" : ""}></i>`)}</button>`;
}

const Row = ({ label, children, hint }) => html`<label class="card-row"><span>${label}</span>${children}${hint && html`<small>${hint}</small>`}</label>`;

function initial(item) {
  const sport = item.kind === "exercise";
  return {
    at: item.time, carbs: blank(item.carbs), insulin: blank(item.insulin),
    level: CERTAINTY_LEVELS.includes(item.carbs_certainty) ? CERTAINTY_LEVELS.indexOf(item.carbs_certainty) : null,
    fat: Boolean(item.fat_protein), fast: Boolean(item.fast),
    description: item.description || "", dish: item.dish?.name || "", place: item.place ? String(item.place.id) : "",
    notes: item.notes || "", session: null,
    // Static or calculated fat/protein surcharge (app/fpe.py); the calculated one needs the agent's estimate.
    surchargeMode: item.surcharge ? "fpe" : "static",
    sportKind: item.sport_kind || "jogging", duration: item.duration_minutes ? String(Number(item.duration_minutes)) : "",
    sportFx: sportFields(sport ? item : null),
    units: item.kind === "basal" ? fmt(item.basal_units) : "",
  };
}

export function EntryCard({ item, dateLabel, factor, places, dishes, sportDefaults, agentInfo, instant, onSave, onDelete, onClose }) {
  const [v, setV] = useState(() => initial(item));
  const [showCertainty, setShowCertainty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [photos, setPhotos] = useState([]);
  const [fpe, setFpe] = useState(null);
  const set = patch => setV(current => ({ ...current, ...patch }));
  const kind = item.kind;
  const sport = kind === "exercise";
  const basal = kind === "basal";
  const food = !sport && !basal;
  const carbs = parse(v.carbs) || 0;
  const eats = food && carbs > 0;
  const title = item.title || KIND_TITLES[kind] || t("card.entry");

  // Photos of the agent's estimate head the card.
  useEffect(() => {
    if (!food || !item.event_id) return;
    let live = true;
    api("/api/agent/session?event_id=" + encodeURIComponent(item.event_id)).then(data => {
      const found = data.session;
      if (live) setFpe(found?.fpe || null);
      if (live && found?.photos) setPhotos(Array.from({ length: found.photos }, (_, index) =>
        "/api/agent/photo?session=" + encodeURIComponent(found.session_id) + "&n=" + index));
    }).catch(() => {});
    return () => { live = false; };
  }, []);

  function refine() {
    if (!agentInfo?.available) {
      toast(t("entry.agentMissing"), true);
      return;
    }
    mealChat.open({
      info: agentInfo, eventId: item.event_id || null, sessionId: v.session || null,
      initialText: v.description, previousCarbs: carbs || "",
      context: () => ({ at: instant(v.at), placeId: v.place || null }),
      onApply: result => {
        set({ carbs: String(result.carbs), portion: result.portion ?? 1, level: CERTAINTY_LEVELS.indexOf(result.certainty), fat: result.fat_protein,
              fast: result.fast, dish: result.estimate.dish?.name || "", description: result.estimate.description,
              session: result.session });
        // A new estimate brings new fat and protein: show the calculated surcharge it gives.
        api("/api/agent/session?session_id=" + encodeURIComponent(result.session) + "&portion=" + (result.portion ?? 1)).then(data => setFpe(data.session?.fpe || null)).catch(() => {});
      },
    });
  }

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await onSave({
        at: v.at, notes: v.notes,
        ...(sport ? {
          sport_kind: v.sportKind, duration_minutes: parse(v.duration) || 0,
          // The signed glucose change as shown (− lowers); null: the kind's setting.
          sport_effect_change_mgdl: sportValues(v.sportFx).change,
          sport_minutes: sportValues(v.sportFx).minutes,
        } : basal ? { units: parse(v.units) || 0 } : {
          carbs, insulin: parse(v.insulin) || 0,
          carbs_certainty: v.level == null ? item.carbs_certainty ?? null : CERTAINTY_LEVELS[v.level],
          fat_protein: v.fat, fast: v.fast, place_id: v.place || null, dish_name: v.dish, description: v.description,
          // Any carb entry whose estimate gives a calculated surcharge can switch (meal or snack alike,
          // user request 2026-11-21); otherwise the saved one stays.
          surcharge_mode: fpe ? v.surchargeMode : null,
          agent_session: v.session, ...(v.portion != null ? { portion: v.portion } : {}),
        }),
      });
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
      <button type="button" class="quiet" aria-label=${t("common.close")} onClick=${onClose}>×</button>
    </div>
    <div class="card-body">
      ${photos.length > 0 && html`<div class=${"card-photos" + (photos.length > 1 ? " many" : "")}>
        ${photos.map((photo, index) => html`<img src=${photo} alt=${t("chat.photo", { n: index + 1 })} />`)}</div>`}

      ${food && html`<div class=${"card-tiles" + (kind === "meal" ? "" : " two")}>
        <label class="tile"><small>${t("card.carbs")}</small><span class="tile-value"><input inputmode="decimal" aria-label=${t("card.carbsIn")}
          value=${v.carbs} placeholder="0" onInput=${event => set({ carbs: event.currentTarget.value })} /><small>g</small></span>
          ${eats && html`<${Meter} level=${v.level} onClick=${event => { event.preventDefault(); setShowCertainty(!showCertainty); }} />`}</label>
        <label class="tile"><small>${t("card.insulin")}</small><span class="tile-value"><input inputmode="decimal" aria-label=${t("dose.insulinIn", { unit: UNIT.insulin })}
          value=${v.insulin} placeholder="0" onInput=${event => set({ insulin: event.currentTarget.value })} /><small>${UNIT.insulin}</small></span></label>
        ${kind === "meal" && html`<div class="tile"><small>${t("card.factor")}</small><span class="tile-value"><b>${factor == null ? "–" : factorNum(factor)}</b>
          <small>${UNIT.factor}</small></span></div>`}
      </div>
      ${eats && showCertainty && html`<div class="card-certainty"><${CertaintySlider} level=${v.level ?? 1} onChange=${level => set({ level })} /></div>`}`}

      ${eats && html`<div class="card-food">
        <textarea rows="4" maxlength="1000" placeholder=${t("card.whatWasIt")} aria-label=${t("card.description")} value=${v.description}
          onInput=${event => set({ description: event.currentTarget.value })}></textarea>
        <div class="entry-chips">
          <${FactorChips} fat=${v.fat} fast=${v.fast} onFat=${fat => set({ fat })} onFast=${fast => set({ fast })} />
          <button type="button" class="chip agent-chip card-refine" onClick=${refine}>${t("entry.refine")}</button>
        </div>
      </div>`}

      ${eats && fpe && html`<${SurchargeChoice} mode=${v.surchargeMode} calculated=${fpe} fat=${v.fat}
        staticMgdl=${fpe.static_mgdl?.[v.fat ? "fat" : "plain"]} onChange=${surchargeMode => set({ surchargeMode })} />`}

      ${sport && html`<div class="card-list">
        <${Row} label=${t("entry.sportKind")}><select value=${v.sportKind} onChange=${event => set({ sportKind: event.currentTarget.value })}>
          ${Object.entries(sportNames).map(([key, name]) => html`<option value=${key}>${name}</option>`)}</select><//>
        <${Row} label=${t("card.start")}><input type="time" step="60" required value=${v.at} onInput=${event => set({ at: event.currentTarget.value })} /><//>
        <${Row} label=${t("window.duration")}><span class="card-number"><input inputmode="numeric" required value=${v.duration}
          onInput=${event => set({ duration: event.currentTarget.value })} aria-label=${t("card.durationIn")} /><small>min</small></span><//>
      </div>
      <${SportEffectFields} fields=${v.sportFx} defaults=${defaults} onChange=${sportFx => set({ sportFx })} />
      ${item.sport_shares && html`<${SportShares} item=${item} />`}`}

      ${basal && html`<div class="card-tiles one"><label class="tile"><small>${t("entryKind.basal")}</small><span class="tile-value"><input inputmode="decimal"
        required aria-label=${t("card.basalIn", { unit: UNIT.insulin })} value=${v.units} onInput=${event => set({ units: event.currentTarget.value })} /><small>${UNIT.insulin}</small></span></label></div>`}

      <div class="card-list">
        ${!sport && html`<${Row} label=${t("card.time")}><input type="time" step="60" required value=${v.at} onInput=${event => set({ at: event.currentTarget.value })} /><//>`}
        ${eats && places.length > 0 && html`<${Row} label=${t("common.place")}><select value=${v.place} onChange=${event => set({ place: event.currentTarget.value })}>
          <option value="">${t("card.noPlace")}</option>${places.map(place => html`<option value=${String(place.id)}>${place.name}</option>`)}</select><//>`}
        ${eats && html`<${Row} label=${t("common.dish")}><input type="text" maxlength="60" list="card-dish-options" autocomplete="off" placeholder=${t("card.none")}
          value=${v.dish} onInput=${event => set({ dish: event.currentTarget.value })} />
          <datalist id="card-dish-options">${dishes.map(dish => html`<option value=${dish.name}></option>`)}</datalist><//>`}
        <${Row} label=${t("common.note")}><input type="text" maxlength="500" placeholder=${t("common.optional")} value=${v.notes}
          onInput=${event => set({ notes: event.currentTarget.value })} /><//>
      </div>
      <button type="button" class=${"card-delete" + (armed ? " armed" : "")} disabled=${busy} onClick=${remove}>
        ${armed ? t("common.reallyDelete") : t("card.delete")}</button>
    </div>
    <div class="card-foot"><button class="primary" type="submit" disabled=${busy}>${busy ? t("common.saving") : t("common.save")}</button></div>
  </form>`;
}
