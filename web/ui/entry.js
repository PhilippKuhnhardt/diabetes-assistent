// The entry form: a meal with carbs and insulin (or a correction alone), sport or basal. What it shows
// beyond the typed values comes from the server (/api/entry/preview): snack assignment, closing the
// previous meal, split, basal chip, place, day type and the dose help (web/ui/dose.js). One call
// (/api/entry) saves the entry, the basal riding along and the chosen dose, which the server re-checks.
// app.js mounts the form, passes the day and settings, and talks to it through `control`
// (refresh, tick, choose, applyAgent, submit), since the minute tick, "+" and the agent live outside.
import { html, useRef, useState, useEffect } from "./html.js";
import { api, post, toast, num, timeOnly, names, sportNames, sportChange, localInputTime, toInstant, currentPosition } from "./lib.js";
import { DoseHelp, sliderUnits, firstPart } from "./dose.js";
import { dayTypeSources } from "./day.js";

const ENTRY_TIME_HOLD_MS = 10 * 60000;
const POSITION_MAX_AGE_MS = 2 * 60000;
// KH-Sicherheit is a three-stop slider (unsure, roughly known, weighed) that starts in the middle.
const CERTAINTY_LEVELS = ["unsure", "known", "weighed"];
const CERTAINTY_SLIDER_LABELS = ["KH unsicher", "KH halbwegs", "KH sicher"];
const positionHints = { denied: "Standort nicht freigegeben, Ort bitte selbst wählen",
  failed: "Standort nicht ermittelt, Ort bitte selbst wählen" };
const nowTime = () => localInputTime(new Date()).slice(11, 16);

// A basal dose with its own event id, kept until it is saved so a retry cannot store it twice.
export async function saveBasal(at, units, notes = "", pending = {}) {
  const event_id = pending.id || crypto.randomUUID();
  pending.id = event_id;
  const result = await post("/api/basal", { at, units, event_id, notes });
  pending.id = null;
  return result;
}

export const deleteBasal = id => post("/api/basal/delete", { id });

const doseShown = option => ({ units: option.units, later_units: option.later_units ?? null });
const sameDose = (a, b) => Number(a.units) === Number(b.units) &&
  (a.later_units == null ? b.later_units == null : b.later_units != null && Number(a.later_units) === Number(b.later_units));

// Which meal surcharge the dose uses while both are compared (user request 2026-11-19): the static one
// (80, +120 with "Fettreich") or the one calculated from the agent's fat and protein. The last choice stays.
const SURCHARGE_MODE_KEY = "entrySurchargeMode";
function storedSurchargeMode() {
  try { return localStorage.getItem(SURCHARGE_MODE_KEY) === "fpe" ? "fpe" : "static"; } catch (_) { return "static"; }
}
function storeSurchargeMode(mode) {
  try { localStorage.setItem(SURCHARGE_MODE_KEY, mode); } catch (_) { /* Private mode: the choice holds for this form only. */ }
}

function blank(kind = "meal") {
  return {
    kind, at: nowTime(), timeTouched: false, timeOpen: false, noteOpen: false, fingerOpen: false,
    carbs: "", insulin: "", finger: "", notes: "", certainty: 1, certaintyUnset: false, fat: false, fast: false,
    withBasal: true, basalUnits: "", sportKind: "jogging", duration: "", sportEffect: "", sportEffectTouched: false,
    entryAs: null, split: null, closeMeal: false, place: undefined, agent: null,
    position: null, positionAt: 0, positionPending: false, positionError: null,
    preview: null, busy: false, doseChoice: null, slider: null, sliderKey: null, doseNote: "", doseChanged: {},
    saving: false, dayTypeSaving: false, surchargeMode: storedSurchargeMode(),
  };
}

export function EntryForm({ day, today, settings, agentInfo, connected, control, onPreview, onSaved, onReloadDay, onDayView, onGlucose }) {
  const store = useRef(null);
  if (!store.current) store.current = blank();
  const [, redraw] = useState(0);
  const props = useRef(null);
  props.current = { day, today, settings, agentInfo, connected, onPreview, onSaved, onReloadDay, onDayView, onGlucose };
  const refs = { carbs: useRef(null), insulin: useRef(null), basal: useRef(null), time: useRef(null), finger: useRef(null),
    notes: useRef(null), sport: useRef(null), form: useRef(null) };
  const timer = useRef(null), requestId = useRef(0), activityAt = useRef(0), doseSeen = useRef(null), pending = useRef({ event: null, basal: {} });
  const f = store.current;
  const set = patch => {
    store.current = { ...store.current, ...(typeof patch === "function" ? patch(store.current) : patch) };
    redraw(n => n + 1);
  };

  const basalDefault = () => {
    const units = Number(props.current.settings?.basal_default_units ?? 17);
    return Number.isFinite(units) && units > 0 ? units : 0;
  };
  const sportDefault = kind => sportChange(props.current.settings?.sport_effects_mgdl?.[kind]);
  const isToday = () => props.current.day === props.current.today;
  const fingerActive = s => s.kind === "meal" && isToday();
  const current = (s = store.current) => s.preview && s.preview.day === props.current.day && s.preview.mode === s.kind ? s.preview : null;
  const doseHelp = (s = store.current) => { const dose = current(s)?.dose; return dose?.options ? dose : null; };
  const placeId = (s = store.current) => {
    if (!(Number(s.carbs) > 0)) return null;
    // "none" tells the server that no place was chosen on purpose (no place factor).
    if (s.place === "none") return "none";
    return s.place ?? current(s)?.place?.place_id ?? null;
  };
  const certaintyValue = s => s.certaintyUnset ? null : CERTAINTY_LEVELS[s.certainty];

  // "jetzt" follows the clock; a typed time holds while the form is used and expires 10 min after the last touch.
  function syncTime({ preview = true } = {}) {
    let s = store.current;
    if (s.timeTouched && Date.now() - activityAt.current > ENTRY_TIME_HOLD_MS) s = { ...s, timeTouched: false, timeOpen: false };
    if (!s.timeTouched) {
      const time = nowTime();
      const changed = s.at !== time;
      s = { ...s, at: time };
      if (changed && preview) schedule(0);
    }
    if (s !== store.current) set(s);
    return s;
  }

  function previewRequest() {
    const s = syncTime({ preview: false });
    const { day } = props.current;
    let at = null;
    try { at = toInstant(day, s.at); } catch (_) { /* The time field is incomplete. */ }
    const carbs = Number(s.carbs) > 0;
    return {
      day, at, mode: s.kind, carbs: s.carbs, as: s.entryAs || null,
      finger_mgdl: fingerActive(s) ? s.finger || null : null,
      split: s.split ?? null, close_meal: Boolean(s.closeMeal),
      // Saving re-checks the shown options with these, so the preview must send the same marks.
      carbs_certainty: carbs ? certaintyValue(s) : null,
      fat_protein: carbs && s.fat, fast: carbs && s.fast,
      // The agent's fat and protein estimate offers the calculated surcharge next to the static one.
      fat_g: carbs && s.agent?.fat_g != null ? s.agent.fat_g : null,
      protein_g: carbs && s.agent?.protein_g != null ? s.agent.protein_g : null,
      surcharge_mode: carbs ? s.surchargeMode : null,
      // A position only rides along while the entry is "now" and only picks the chip; the server checks that again.
      position: !s.timeTouched && s.position ? s.position : null,
      place_id: placeId(s),
    };
  }

  // What the user typed, without the clock: a preview for the same input that shows other doses was
  // recalculated from newer data (CGM, entries, profile), and those options are marked.
  const inputKey = (body, s) => JSON.stringify({ ...body, at: s.timeTouched ? body.at : null });

  // The doses seen for this input stay the reference until the input changes or an option is tapped.
  function changedDoses(help, key) {
    const shown = Object.fromEntries(help.options.map(option => [option.variant, doseShown(option)]));
    if (!doseSeen.current || doseSeen.current.key !== key) {
      doseSeen.current = { key, doses: shown };
      return {};
    }
    const changed = {};
    for (const [variant, dose] of Object.entries(shown)) {
      const before = doseSeen.current.doses[variant];
      if (before && !sameDose(before, dose)) changed[variant] = before;
    }
    return changed;
  }

  // A chosen suggestion fills the insulin field and follows recalculations until the field is edited;
  // the slider fills it while it follows (with a split: the part given now).
  function settleDose(s) {
    const help = doseHelp(s);
    let next = { ...s };
    if (help?.slider) {
      const key = current(s).inputKey;
      // New input (other carbs, marks, place): the slider starts again at the recommendation.
      if (next.sliderKey !== key) {
        next.sliderKey = key;
        if (next.slider?.follow !== false) next.slider = null;
      }
      if (next.slider?.follow !== false) {
        const units = sliderUnits(help.slider, next.slider, false);
        next.insulin = String(firstPart(help.slider, units) ?? units);
      }
    }
    const option = help?.options.find(item => item.variant === next.doseChoice);
    if (next.doseChoice && help && !option?.savable) next.doseChoice = null;
    if (next.doseChoice && option) next.insulin = String(Number(option.units));
    return next;
  }

  async function refreshPreview() {
    clearTimeout(timer.current);
    const id = ++requestId.current;
    const body = previewRequest();
    if (body.mode === "meal" && !doseHelp() && !store.current.doseNote && body.day === props.current.today) set({ doseNote: "Empfehlung wird berechnet …" });
    try {
      const data = await post("/api/entry/preview", body);
      if (id !== requestId.current) return null;
      const preview = { ...data, mode: body.mode, inputKey: inputKey(body, store.current), loadedAt: Date.now() };
      set(s => {
        const next = { ...s, preview, busy: false, doseNote: data.dose?.message || "" };
        const help = doseHelp(next);
        next.doseChanged = help ? changedDoses(help, preview.inputKey) : {};
        return settleDose(next);
      });
      props.current.onPreview(preview);
      return preview;
    } catch (error) {
      if (id === requestId.current) {
        set({ preview: null, busy: false, doseNote: body.mode === "meal" ? error.message : "" });
        props.current.onPreview(null);
      }
      return null;
    }
  }

  function schedule(delay = 350) {
    clearTimeout(timer.current);
    requestId.current++;
    if (store.current.kind === "meal" && !store.current.busy) set({ busy: true });
    timer.current = setTimeout(refreshPreview, delay);
  }

  function requestPosition() {
    const s = store.current;
    if (s.kind !== "meal" || !(Number(s.carbs) > 0) || s.timeTouched || !isToday() || s.positionPending) return;
    if (s.position && Date.now() - s.positionAt < POSITION_MAX_AGE_MS) return;
    set({ positionPending: true });
    currentPosition()
      .then(position => set({ position, positionAt: Date.now(), positionError: null }))
      .catch(error => set({ positionError: error?.code === 1 ? "denied" : "failed" }))
      .finally(() => { set({ positionPending: false }); schedule(0); });
  }

  function setKind(kind) {
    set(s => {
      const next = { ...s, kind };
      if (kind === "basal" && !next.basalUnits && basalDefault() > 0) next.basalUnits = String(basalDefault());
      return next;
    });
    refs.carbs.current?.setCustomValidity("");
    schedule(0);
  }

  function reset() {
    pending.current = { event: null, basal: {} };
    // A saved entry changes what still acts: the next suggestions are new, not "changed" (user report 2026-11-15).
    doseSeen.current = null;
    refs.carbs.current?.setCustomValidity("");
    const next = { ...blank(store.current.kind), preview: store.current.preview };
    if (next.kind === "basal" && basalDefault() > 0) next.basalUnits = String(basalDefault());
    store.current = next;
    set({});
    schedule(0);
  }

  // Undo deletes the just-saved Nightscout entry and puts its values back into the form for correction.
  async function undoEntry(id, restore) {
    try {
      await post("/api/treatment/delete", { id, day: restore.day });
      if (props.current.day === restore.day) {
        await props.current.onReloadDay();
        const s = store.current;
        // Only an untouched form takes it back; the slider's suggested IE do not count as typed.
        if (s.kind === "meal" && !s.carbs && (!s.insulin || s.slider?.follow !== false)) {
          activityAt.current = Date.now();
          set({ at: restore.time, timeTouched: true, carbs: restore.carbs, insulin: restore.insulin, notes: restore.notes,
            doseChoice: null, slider: restore.insulin === "" ? null : { units: Number(restore.insulin), follow: false } });
          await refreshPreview();
        }
      }
      toast("Eintrag aus Nightscout entfernt");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function saveBasalEntry(s) {
    const units = Number(s.basalUnits);
    if (!(units > 0 && units <= 100 && Number.isInteger(units * 2))) {
      refs.basal.current.setCustomValidity("Bitte Basal zwischen 0,5 und 100 IE in 0,5er-Schritten eintragen.");
      refs.basal.current.reportValidity();
      return;
    }
    let at;
    try { at = toInstant(props.current.day, s.at); }
    catch (error) { toast(error.message, true); return; }
    set({ saving: true });
    try {
      const result = await saveBasal(at, units, s.notes, pending.current.basal);
      reset();
      await props.current.onSaved(result, null);
      const undo = async () => {
        try {
          await deleteBasal(result.id);
          if (props.current.day === result.day) await props.current.onReloadDay();
          toast("Basal entfernt");
        } catch (error) {
          toast(error.message, true);
        }
      };
      toast("Basal " + num(units, 1) + " IE " + (result.nightscout_synced ? "gespeichert" : "lokal gespeichert, Nightscout folgt"),
        !result.nightscout_synced && Boolean(props.current.connected), { label: "Rückgängig", run: undo });
    } catch (error) {
      toast(error.message, true);
    } finally {
      set({ saving: false });
    }
  }

  // One call saves the entry, the basal riding along and the confirmed sport; the server decides
  // meal or snack and re-checks a tapped suggestion against the current one.
  async function save() {
    if (store.current.saving) return;
    const s = syncTime({ preview: false });
    const mode = s.kind;
    if (mode === "basal") return saveBasalEntry(s);
    const carbs = mode === "meal" ? Number(s.carbs || 0) : 0;
    const insulin = mode === "meal" ? Number(s.insulin || 0) : 0;
    const finger = mode === "meal" && fingerActive(s) ? Number(s.finger || 0) : 0;
    if (mode === "meal" && carbs <= 0 && insulin <= 0 && !finger) {
      refs.carbs.current.setCustomValidity("Bitte Kohlenhydrate, Insulin oder beides eintragen.");
      refs.carbs.current.reportValidity();
      return;
    }
    if (mode === "exercise" && !refs.form.current.reportValidity()) return;
    let at;
    try { at = toInstant(props.current.day, s.at); }
    catch (error) { toast(error.message, true); return; }
    const help = doseHelp(s);
    const basalUnits = entryBasalUnits(s);
    const chosenOption = mode === "meal" && s.doseChoice && help ? help.options.find(option => option.variant === s.doseChoice) : null;
    const chosen = chosenOption?.units ?? null;
    const sportEffect = sportEffectValue(s);
    const payload = {
      ...previewRequest(), at, carbs: s.carbs, insulin: s.insulin, with_basal: basalUnits > 0,
      sport_kind: s.sportKind,
      // Only a changed value is stored on the session; the default keeps following the setting.
      sport_effect_mgdl: mode === "exercise" && sportEffect !== "" && Number(sportEffect) !== sportDefault(s.sportKind) ? sportEffect : null,
      duration_minutes: s.duration || 0, notes: s.notes,
      carbs_certainty: carbs > 0 ? certaintyValue(s) : null,
      fat_protein: carbs > 0 && s.fat, fast: carbs > 0 && s.fast,
      event_id: pending.current.event || crypto.randomUUID(),
      basal_event_id: basalUnits > 0 ? pending.current.basal.id || crypto.randomUUID() : null,
      dose_check: chosen != null && Number(chosen) === insulin ? help.check : null,
      dose_variant: s.doseChoice || null,
      dose_shown: chosenOption && Number(chosenOption.units) === insulin ? doseShown(chosenOption) : null,
      // A split meal dosed on the slider: the total, of which the insulin field holds the first part.
      dose_total_units: mode === "meal" && help?.slider?.first_percent != null && s.slider?.follow !== false
        ? sliderUnits(help.slider, s.slider) : null,
      // What the agent's estimate named; the carbs themselves are whatever the form holds now.
      ...(mode === "meal" && carbs > 0 && s.agent ? {
        dish_name: s.agent.dish_name, description: s.agent.description, agent_session: s.agent.session_id } : {}),
    };
    pending.current.event = payload.event_id;
    pending.current.basal.id = payload.basal_event_id;
    set({ saving: true });
    try {
      let result;
      try {
        result = await post("/api/entry", payload);
      } catch (error) {
        if (error.code === "dose_changed") {
          // The tapped dose is no longer current: the new one is shown marked and has to be tapped again,
          // so an old dose cannot be confirmed by accident.
          set(state => ({ doseChoice: null, insulin: Number(state.insulin) === Number(chosen) ? "" : state.insulin }));
          await refreshPreview();
          document.getElementById("dose-result")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        } else if (payload.dose_check) await refreshPreview();
        throw error;
      }
      const restore = { day: result.day, time: s.at, carbs: s.carbs, insulin: s.insulin, notes: s.notes };
      reset();
      await props.current.onSaved(result, result.day_view);
      const basal = result.basal;
      const message = (result.kind === "exercise" ? "Sport" : [result.finger_mgdl != null ? "BZ " + num(result.finger_mgdl, 0) + " mg/dL" : "",
        carbs > 0 ? num(carbs, 0) + " g" : "", insulin > 0 ? num(insulin, 2) + " IE" : ""]
        .filter(Boolean).join(" + ")) + (basal ? " + Basal " + num(basal.units, 1) + " IE" : "") + " gespeichert" +
        (result.split_units ? " · 2. Teil " + num(result.split_units, 1) + " IE vorgemerkt" : "") +
        (basal && !basal.nightscout_synced ? " · Basal vorerst nur lokal" : "") +
        (result.basal_error ? " · Basal nicht gespeichert: " + result.basal_error : "");
      const undo = async () => {
        if (basal) await deleteBasal(basal.id).catch(error => toast(error.message, true));
        if (result.id) await undoEntry(result.id, restore);
        else if (basal) await props.current.onReloadDay();
      };
      toast(message, Boolean(result.basal_error), result.id || basal ? { label: "Rückgängig", run: undo } : null);
    } catch (error) {
      toast(error.message, true);
    } finally {
      set({ saving: false });
    }
  }

  // The server offers the usual basal dose with an entry before breakfast end while the day has none.
  const previewBasalUnits = s => s.kind === "meal" && current(s) ? Number(current(s).basal_units) : 0;
  const entryBasalUnits = s => previewBasalUnits(s) > 0 && s.withBasal ? previewBasalUnits(s) : 0;
  // A sport entry starts with its kind's setting; a typed value stays until the entry is saved.
  const sportEffectValue = s => s.sportEffectTouched ? s.sportEffect : String(sportDefault(s.sportKind));

  function openAgent() {
    const { agentInfo } = props.current;
    if (!agentInfo?.available) {
      toast("Essens-Agent ist nicht eingerichtet: in den Einstellungen Anbieter und Modell wählen", true);
      return;
    }
    if (!window.mealChat) { toast("Seite neu laden, der Chat ist noch nicht geladen", true); return; }
    props.current.onGlucose();
    window.mealChat.open({
      info: agentInfo, api, toast, previousCarbs: store.current.carbs, sessionId: store.current.agent?.session_id || null,
      context: () => {
        let at = null;
        try { at = toInstant(props.current.day, store.current.at); } catch (_) { /* Without a valid time the server takes now. */ }
        const id = placeId();
        return { at, placeId: id === "none" ? null : id };
      },
      onApply: applyAgent,
    });
  }

  // The agent only fills the form; the dose help calculates as always.
  function applyAgent({ session, estimate, carbs, certainty, fat_protein, fast }) {
    const index = CERTAINTY_LEVELS.indexOf(certainty);
    set({ carbs: String(carbs), certainty: index < 0 ? 1 : index, certaintyUnset: certainty !== undefined && index < 0,
      fat: Boolean(fat_protein), fast: Boolean(fast),
      agent: { session_id: session, dish_name: estimate.dish?.name || "", description: estimate.description,
        fat_g: estimate.fat_g ?? null, protein_g: estimate.protein_g ?? null,
        range: estimate.carbs_low_g + "–" + estimate.carbs_high_g + " g" } });
    requestPosition();
    schedule(0);
  }

  async function chooseDayType(value) {
    set({ dayTypeSaving: true });
    try {
      await post("/api/analysis/day-type", { day: props.current.day, day_type: value });
      await refreshPreview();
    } catch (error) {
      toast(error.message, true);
    } finally {
      set({ dayTypeSaving: false });
    }
  }

  // A tapped suggestion saves carbs and that dose directly in Nightscout; the toast offers undo.
  function chooseDose(variant) {
    if (!doseHelp() || store.current.saving) return;
    const help = doseHelp();
    doseSeen.current = doseSeen.current && { ...doseSeen.current,
      doses: Object.fromEntries(help.options.map(option => [option.variant, doseShown(option)])) };
    refs.carbs.current?.setCustomValidity("");
    set(s => settleDose({ ...s, doseChoice: variant, doseChanged: {} }));
    if (store.current.insulin === "") return;
    save();
  }

  function slide(units) {
    refs.carbs.current?.setCustomValidity("");
    set(s => settleDose({ ...s, slider: { units, follow: true }, doseChoice: null }));
  }

  // Own IE: the slider shows where they land (as a total without a split) and stops filling the field;
  // an emptied field hands the dose back to the slider.
  function typeInsulin(value) {
    refs.carbs.current?.setCustomValidity("");
    const help = doseHelp();
    const own = Number(value);
    set(s => ({ insulin: value, doseChoice: null,
      slider: value === "" ? null : { units: help?.slider?.first_percent == null && own >= 0 ? own : s.slider?.units, follow: false } }));
  }

  // The outside world (minute tick, "+", the agent, Ctrl+Enter) reaches the form through `control`.
  if (control) Object.assign(control, {
    refresh: () => { if (!document.hidden && isToday() && !store.current.saving) refreshPreview(); },
    preview: refreshPreview,
    schedule,
    tick: () => syncTime({ preview: false }),
    sync: () => syncTime(),
    release: () => { if (store.current.timeTouched) set({ timeTouched: false, timeOpen: false }); },
    reset,
    choose(kind, focus) {
      setKind(kind === "exercise" || kind === "basal" ? kind : "meal");
      const target = { meal: refs.carbs, manual: refs.carbs, exercise: refs.sport, basal: refs.basal }[focus];
      setTimeout(() => target?.current?.focus());
    },
    openAgent,
    submit: save,
    idle: () => !["carbs", "insulin", "finger", "notes", "duration"].some(name => store.current[name]),
  });

  useEffect(() => { schedule(0); }, [day]);

  // ---- Drawing ----
  const preview = current(f);
  const help = doseHelp(f);
  const meal = f.kind === "meal";
  const choice = meal && help?.surcharge_choice ? help.surcharge_choice : null;
  const assignment = meal ? preview?.assignment : null;
  const close = assignment?.close;
  const split = meal ? preview?.split : null;
  const place = meal ? preview?.place : null;
  const dayType = meal ? preview?.day_type : null;
  const basalOffer = previewBasalUnits(f);
  const fingerOpen = fingerActive(f) && (f.fingerOpen || Boolean(f.finger) || Boolean(preview?.dose?.finger_offer));
  const timeOpen = f.timeOpen || day !== today;
  const noteOpen = f.noteOpen || Boolean(f.notes);
  // The manual entry ("KE & IE") shows its fields right away, not only once carbs are typed (user request 2026-11-18).
  const quality = meal && (document.getElementById("entry-drawer")?.dataset.quick === "manual" || Number(f.carbs) > 0);
  const selectedPlace = placeId(f);
  const chips = place ? place.options.filter(option => option.quick || option.id === selectedPlace) : [];
  const more = place ? place.options.filter(option => !chips.includes(option)) : [];
  // Only what needs a look gets a line: no places yet, no position, or a position that is no place yet.
  const placeHint = !place ? "" : !place.options.length ? "Noch keine Orte, in den Einstellungen anlegen"
    : f.place !== undefined || f.positionPending || place.status === "matched" ? ""
    : place.status === "none" && f.positionError ? positionHints[f.positionError]
    : place.status === "unknown" ? place.hint : "";
  const submitLabel = (() => {
    if (f.kind === "exercise") return "Sport in Nightscout speichern";
    if (f.kind === "basal") {
      const units = Number(f.basalUnits);
      return Number.isFinite(units) && units > 0 ? "Basal " + num(units, 1) + " IE speichern" : "Basal speichern";
    }
    const parts = [Number(f.carbs) > 0 ? num(f.carbs, 0) + " g" : "", Number(f.insulin) > 0 ? num(f.insulin, 2) + " IE" : ""].filter(Boolean);
    const basal = entryBasalUnits(f);
    if (parts.length && basal > 0) parts.push("Basal " + num(basal, 1) + " IE");
    return parts.length ? parts.join(" + ") + " speichern" : "In Nightscout speichern";
  })();
  const touch = () => { activityAt.current = Date.now(); };
  const typeCarbs = value => {
    refs.carbs.current.setCustomValidity("");
    set({ carbs: value });
    requestPosition();
    schedule();
  };
  const toggle = (patch, delay = 0) => { set(patch); schedule(delay); };
  const certaintyIndex = f.certainty;

  return html`<form id="entry-form" ref=${refs.form} onSubmit=${event => { event.preventDefault(); save(); }}
    onFocusIn=${() => syncTime()} onInputCapture=${touch} onChangeCapture=${touch} onPointerDownCapture=${touch} onKeyDownCapture=${touch}>
    <div class="entry-switch" role="group" aria-label="Eintragsart">${[["meal", "Essen & Insulin"], ["exercise", "Sport"], ["basal", "Basal"]].map(([kind, label]) =>
      html`<button type="button" class="entry-mode" data-entry-mode=${kind} aria-pressed=${String(f.kind === kind)} onClick=${event => {
        setKind(kind);
        if (event.isTrusted) control?.onManualKind?.();
      }}>${label}</button>`)}</div>
    <input type="hidden" name="kind" id="entry-kind" value=${f.kind} />
    <div class="entry-chips">
      <button type="button" id="entry-time-chip" class="chip" aria-controls="entry-time-field" aria-expanded=${String(timeOpen)} hidden=${timeOpen}
        onClick=${() => { set({ timeOpen: true }); setTimeout(() => refs.time.current?.focus()); }}>Zeit: ${(f.timeTouched ? "" : "jetzt · ") + (f.at || "—")}</button>
      ${assignment && html`<button type="button" id="entry-assign-chip" class="chip" title="Zuordnung umschalten" disabled=${Boolean(assignment.own_meal_taken)}
        onClick=${() => toggle({ entryAs: assignment.snack ? "meal" : "snack" })}>Zählt als: ${(assignment.snack
          ? (assignment.meal ? "Snack zu " + names[assignment.meal] + " " + timeOnly(assignment.at) : "Snack ohne Mahlzeit")
          : "eigenes " + names[assignment.own_meal]) + (assignment.own_meal_taken ? " (" + names[assignment.own_meal] + " heute schon eingetragen)" : " ⇄")}</button>`}
      ${close && html`<button type="button" id="entry-close-chip" class="chip" title="Vorige Mahlzeit hier abschließen" aria-pressed=${String(Boolean(close.active))}
        onClick=${() => toggle({ closeMeal: !close.active })}>${close.active
          ? names[close.meal] + " " + timeOnly(close.at) + " endet " + timeOnly(close.end) + " ✓"
          : names[close.meal] + " " + timeOnly(close.at) + " hier abschließen"}</button>`}
      ${split && html`<button type="button" id="entry-split-chip" class="chip" title="Split-Bolus ein- oder ausschalten" aria-pressed=${String(Boolean(split.active))}
        onClick=${() => toggle({ split: !split.active })}>${split.active
          ? "Split " + split.first_percent + "/" + (100 - split.first_percent) + " · 2. Teil " + timeOnly(split.due_at) : "Ohne Split"}</button>`}
      ${basalOffer > 0 && html`<label id="entry-basal-option" class="chip chip-toggle"><input type="checkbox" name="with_basal" checked=${f.withBasal}
        onChange=${event => set({ withBasal: event.currentTarget.checked })} /> <span id="entry-basal-hint">+ Basal ${num(basalOffer, 1)} IE</span></label>`}
    </div>
    <label id="entry-time-field" hidden=${!timeOpen}>Zeitpunkt (24 h)<input type="time" name="at" id="entry-time" step="60" required ref=${refs.time}
      value=${f.at} onInput=${event => { activityAt.current = Date.now(); set({ at: event.currentTarget.value, timeTouched: true }); schedule(0); }} /></label>
    <section id="entry-meal-fields" class="entry-card" aria-label="Essen" hidden=${!meal}>
      <p class="entry-card-title">Essen</p>
      <div class="entry-chips agent-entry">
        <button type="button" id="entry-agent-open" class="chip agent-chip" onClick=${openAgent}>${f.agent ? "Nachbessern" : "Essen einschätzen"}</button>
        ${f.agent && html`<span id="entry-agent-summary" class="agent-summary">${(f.agent.dish_name ? "Gericht: " + f.agent.dish_name : "Ohne Gericht") + " · geschätzt " + f.agent.range}</span>
          <button type="button" id="entry-agent-clear" class="chip quiet-chip" aria-label="Einschätzung verwerfen" onClick=${() => set({ agent: null })}>×</button>`}
      </div>
      <div class="entry-carbs-row">
        <label id="entry-carbs-field">Kohlenhydrate <span>g</span><input name="carbs" type="number" min="0" max="500" step="1" inputmode="decimal"
          ref=${refs.carbs} value=${f.carbs} disabled=${!meal} onInput=${event => typeCarbs(event.currentTarget.value)} /></label>
        <div id="entry-factors" class="entry-chips entry-factor-chips" hidden=${!quality}>
          <label class="chip chip-toggle factor-chip" title="Fett- oder eiweißreiche Mahlzeit"><input type="checkbox" name="fat_protein" aria-label="Fettreich"
            checked=${f.fat} onChange=${event => toggle({ fat: event.currentTarget.checked })} /><span class="factor-icon factor-fat_protein" aria-hidden="true"></span><span>Fettreich</span></label>
          <label class="chip chip-toggle factor-chip" title="Wirkt schnell (z. B. Saft, Weißbrot)"><input type="checkbox" name="fast" aria-label="Schnell"
            checked=${f.fast} onChange=${event => set({ fast: event.currentTarget.checked })} /><span class="factor-icon factor-fast" aria-hidden="true"></span><span>Schnell</span></label>
        </div>
      </div>
      <div id="entry-quality" class="entry-chips entry-quality-row" hidden=${!quality}>
        <span class="certainty-slider" data-level=${CERTAINTY_LEVELS[certaintyIndex]}><input type="range" name="certainty_level" min="0" max="2" step="1"
          value=${certaintyIndex} aria-label="KH-Sicherheit: unsicher, halbwegs sicher, sicher (gewogen oder genormt)"
          onInput=${event => set({ certainty: Number(event.currentTarget.value), certaintyUnset: false })}
          onChange=${() => schedule(0)} /><output class="certainty-label">${CERTAINTY_SLIDER_LABELS[certaintyIndex]}</output></span>
      </div>
      ${place && html`<div id="entry-place" class="entry-place">
        <div id="entry-place-chips" class="entry-chips" role="group" aria-label="Ort">${chips.map(option =>
          html`<button type="button" class="chip place-chip" data-place-id=${option.id} aria-pressed=${String(option.id === selectedPlace)}
            onClick=${() => toggle({ place: selectedPlace === option.id ? "none" : option.id })}>${option.name}</button>`)}
          ${more.length > 0 && html`<select class="chip place-more" aria-label="Weitere Orte" value=""
            onChange=${event => { if (event.currentTarget.value) toggle({ place: Number(event.currentTarget.value) }); }}>
            <option value="">Weitere …</option>${more.map(option => html`<option value=${option.id}>${option.name}</option>`)}</select>`}</div>
        ${placeHint && html`<p id="entry-place-hint" class="hint">Ort: ${placeHint}</p>`}</div>`}
    </section>
    <label id="entry-basal-field" hidden=${f.kind !== "basal"}>Basal <span>IE</span><input name="basal_units" type="number" min="0.5" max="100" step="0.5" inputmode="decimal"
      ref=${refs.basal} value=${f.basalUnits} disabled=${f.kind !== "basal"} required=${f.kind === "basal"}
      onInput=${event => { event.currentTarget.setCustomValidity(""); set({ basalUnits: event.currentTarget.value }); }} /></label>
    <label id="entry-sport-field" hidden=${f.kind !== "exercise"}>Sportart
      <select name="sport_kind" ref=${refs.sport} disabled=${f.kind !== "exercise"} onChange=${event => set({ sportKind: event.currentTarget.value })}>${Object.entries(sportNames).map(([kind, label]) =>
        html`<option value=${kind} selected=${f.sportKind === kind}>${label}</option>`)}</select></label>
    <label id="entry-duration-field" hidden=${f.kind !== "exercise"}>Dauer <span>Minuten</span><input name="duration_minutes" type="number" min="1" max="1440" step="1"
      value=${f.duration} disabled=${f.kind !== "exercise"} required=${f.kind === "exercise"} onInput=${event => set({ duration: event.currentTarget.value })} /></label>
    <label id="entry-sport-effect-field" hidden=${f.kind !== "exercise"}>Einfluss <span>mg/dL, − senkt</span><input name="sport_effect_mgdl" type="number" min="-500" max="500" step="1"
      value=${sportEffectValue(f)} disabled=${f.kind !== "exercise"} onInput=${event => set({ sportEffect: event.currentTarget.value, sportEffectTouched: true })} /></label>
    <section id="entry-influences" class="entry-card" aria-label="Einflüsse" hidden=${!dayType}>
      <p class="entry-card-title">Einflüsse</p>
      ${dayType && html`<div id="entry-day-type" class="entry-day-type">
        <div class="entry-switch" role="group" aria-label="Tagesart für das Frühstück">${[["work", "Arbeitstag"], ["free", "Freier Tag"]].map(([value, label]) =>
          html`<button type="button" class="day-type-option" data-day-type=${value} aria-pressed=${String(dayType.value === value)} disabled=${f.dayTypeSaving}
            onClick=${() => chooseDayType(value)}>${label}</button>`)}</div>
        <p id="entry-day-type-hint" class="hint">${dayTypeSources[dayType.source] || ""}</p></div>`}
    </section>
    <section id="entry-dose" class="entry-card entry-dose-card" aria-label="Dosis" hidden=${!meal}>
      <label id="entry-finger-field" hidden=${!fingerOpen}>BZ blutig gemessen <span>mg/dL</span><input name="finger_mgdl" type="number" min="20" max="600" step="1" inputmode="numeric"
        ref=${refs.finger} value=${f.finger} disabled=${!fingerActive(f)} onInput=${event => { refs.carbs.current.setCustomValidity(""); set({ finger: event.currentTarget.value }); schedule(); }} /></label>
      ${choice && html`<div id="entry-surcharge" class="entry-surcharge">
        <div class="entry-switch" role="group" aria-label="Zuschlag für Fett und Eiweiß">${[
          ["static", "Fest " + num(choice.static_mgdl, 0) + " mg/dL" + (choice.static_fat ? " (fettreich)" : "")],
          ["fpe", "Berechnet " + num(choice.fpe_mgdl, 0) + " mg/dL"]].map(([mode, label]) =>
          html`<button type="button" class="surcharge-option" data-surcharge-mode=${mode} aria-pressed=${String(choice.mode === mode)}
            onClick=${() => { storeSurchargeMode(mode); toggle({ surchargeMode: mode }); }}>${label}</button>`)}</div>
        <p id="entry-surcharge-hint" class="hint">Fett ${num(choice.fat_g, 0)} g, Eiweiß ${num(choice.protein_g, 0)} g = ${num(choice.fpe, 1)} FPE × ${num(choice.per_fpe_mgdl, 0)} mg/dL, wirkt bis ${num(choice.fpe_minutes / 60, 1)} h${choice.mode === "fpe" ? " · Split: KH jetzt, Zuschlag später" : ""}</p></div>`}
      <div id="dose-result" class="help-result" aria-live="polite" aria-busy=${meal && f.busy ? "true" : null}>
        <${DoseHelp} help=${help} note=${f.doseNote} changed=${f.doseChanged} choice=${f.doseChoice} chosen=${f.slider} onChoose=${chooseDose} onSlide=${slide} /></div>
      <label id="entry-insulin-field">Eigene IE<input name="insulin" type="number" min="0" max="100" step="0.5" inputmode="decimal"
        ref=${refs.insulin} value=${f.insulin} disabled=${!meal} onInput=${event => typeInsulin(event.currentTarget.value)} /></label>
    </section>
    <label id="entry-note-field" hidden=${!noteOpen}>Notiz <span>optional</span><input name="notes" type="text" maxlength="500" ref=${refs.notes}
      value=${f.notes} onInput=${event => set({ notes: event.currentTarget.value })} /></label>
    <div class="entry-chips"><button type="button" id="entry-finger-chip" class="chip quiet-chip" aria-controls="entry-finger-field" aria-expanded=${String(fingerOpen)}
      hidden=${!fingerActive(f) || fingerOpen} onClick=${() => { set({ fingerOpen: true }); setTimeout(() => refs.finger.current?.focus()); }}>+ BZ blutig</button><button type="button"
      id="entry-note-chip" class="chip quiet-chip" aria-controls="entry-note-field" aria-expanded=${String(noteOpen)} hidden=${noteOpen}
      onClick=${() => { set({ noteOpen: true }); setTimeout(() => refs.notes.current?.focus()); }}>+ Notiz</button></div>
    <div class="form-actions entry-actions"><button class="primary" type="submit" id="entry-submit" disabled=${f.saving}>${submitLabel}</button></div>
  </form>`;
}
