// "Mehr": profile and model settings. Wide screens show every panel; phones show a list of pages and
// one page at a time (app.js keeps which page is open and marks #settings-view for the CSS).
// Every value comes from /api/settings and its neighbours; saving sends it back and the server checks it.
import { html, useState, useEffect, useRef } from "./html.js";
import { api, post, toast, busy, num, gl, glNum, signedPercent, sportKinds, sportChange, minuteClock, clockMinute, toInstant, localInputTime,
  pickerMinimum, currentPosition, MINUTE, UNIT } from "./lib.js";
import { t, locale, lang, LANGUAGES, storedLanguage, setLanguage } from "./i18n.js";
import { ThemePanel } from "./theme.js";
import { glField, glInput, toMgdl, glValue, mmol, factorValue, factorToUnitsPerKe, gramsPerIe, MGDL_PER_MMOL } from "./units.js";

// The pages in the order they appear; `form` pages belong to the model form and its save button.
const page = id => [id, t("settings.pages." + id)];
export const SETTINGS_PAGES = [
  { group: t("settings.groups.general"), pages: ["nightscout", "timezone", "vacation", "notify", "agent", "places", "language", "theme"].map(page) },
  { group: t("settings.groups.profile"), pages: ["isf", "ic", "target"].map(page) },
  { group: t("settings.groups.model"), form: true, pages: ["dose", "meals", "sport"].map(page) },
];
export const FORM_PAGES = new Set(SETTINGS_PAGES.find(group => group.form).pages.map(([id]) => id));

const osmLink = (lat, lon) => "https://www.openstreetmap.org/?mlat=" + lat + "&mlon=" + lon + "#map=18/" + lat + "/" + lon;
const stamp = value => new Date(value).toLocaleString(locale);

function Panel({ id, page, title, unit, className = "", children }) {
  return html`<section class=${"panel " + className + (page === id ? " is-current" : "")} data-settings-page=${id}>
    <h2>${title}${unit && html` <span class="unit">${unit}</span>`}</h2>${children}</section>`;
}

export function SettingsIndex({ onOpen }) {
  return html`${SETTINGS_PAGES.map(group => html`<div class="settings-index-group"><h2>${group.group}</h2>
    ${group.pages.map(([id, title]) => html`<button type="button" class="settings-index-item" data-settings-open=${id} onClick=${() => onOpen(id)}>${title}</button>`)}</div>`)}`;
}

// ---- Allgemein ----
function Nightscout({ page, data, onReload }) {
  const [test, setTest] = useState("");
  const [status, setStatus] = useState(null);
  const profile = data.settings.nightscout_profile;
  const source = (data.profile_sync_error ? data.profile_sync_error + " · " : "") + (profile
    ? t("settings.ns.profile", { name: profile.profile_name, since: stamp(profile.start_at) }) + (profile.timezone ? " · " + profile.timezone : "")
    : t("settings.ns.noProfile")) + (data.profile_sync_at ? " · " + t("settings.ns.lastSync", { at: stamp(data.profile_sync_at) }) : "");
  async function testConnection() {
    setTest(t("settings.ns.testing"));
    try { setTest((await api("/api/test-connection")).message); } catch (error) { setTest(error.message); }
  }
  async function syncProfile(event) {
    const button = event.currentTarget;
    button.disabled = true;
    setStatus(t("settings.ns.syncing"));
    try {
      const result = await post("/api/profile/sync", {});
      setStatus(null);
      await onReload({ day: true });
      toast(result.message);
    } catch (error) {
      setStatus(error.message);
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }
  return html`<${Panel} id="nightscout" page=${page} title="Nightscout">
    <p id="nightscout-location" class="mono" hidden=${data.connected}>${data.connected ? "" : t("settings.ns.setVariables")}</p>
    <div class="button-row"><button id="test-connection" type="button" onClick=${testConnection}>${t("settings.ns.test")}</button>
      <button type="button" id="sync-profile" onClick=${syncProfile}>${t("settings.ns.sync")}</button></div>
    <p id="test-result" class="hint">${test}</p>
    <p id="isf-sync-status" class="hint">${status ?? source}</p><//>`;
}

function zoneOffsetMinutes(zone, at = new Date()) {
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
      .formatToParts(at).map(part => [part.type, part.value]));
    const wall = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute);
    return Math.round((wall - Math.floor(at.getTime() / 60000) * 60000) / 60000);
  } catch { return null; }
}

export function zoneText(zone) {
  const offset = zoneOffsetMinutes(zone);
  if (offset == null) return zone;
  const sign = offset < 0 ? "−" : "+";
  const hours = Math.floor(Math.abs(offset) / 60), minutes = Math.abs(offset) % 60;
  return zone + " (UTC" + sign + hours + (minutes ? ":" + String(minutes).padStart(2, "0") : "") + ")";
}

export function deviceZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch { return null; }
}

// The phone's clock differs from the app's: the diary and the settings offer the switch.
export const deviceZoneDiffers = travel => {
  const device = deviceZone();
  return Boolean(device && travel && zoneOffsetMinutes(device) !== zoneOffsetMinutes(travel.current));
};

function Timezone({ page, travel, onSwitchZone }) {
  const [at, setAt] = useState("");
  if (!travel) return html`<${Panel} id="timezone" page=${page} title=${t("settings.pages.timezone")} />`;
  const device = deviceZone();
  const differs = deviceZoneDiffers(travel);
  const last = travel.switches?.[travel.switches.length - 1];
  const since = last ? " " + t("settings.tz.since", { at: new Date(last.at).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" }) }) : "";
  const now = new Date();
  const run = (event, payload) => busy(event, async () => {
    if (at && !payload.undo) payload.at = new Date(at).toISOString();
    if (await onSwitchZone(payload)) setAt("");
  });
  return html`<${Panel} id="timezone" page=${page} title=${t("settings.pages.timezone")}>
    <p id="timezone-status" class="hint">${t("settings.tz.status", { zone: zoneText(travel.current), home: travel.current !== travel.home ? since + ", " + t("settings.tz.home", { zone: zoneText(travel.home) }) : "" })}${differs ? " " + t("settings.tz.device", { zone: zoneText(device) }) : ""}</p>
    <label>${t("settings.tz.validFrom")}<input type="datetime-local" id="timezone-at" value=${at} onInput=${event => setAt(event.currentTarget.value)}
      max=${localInputTime(now)} min=${localInputTime(new Date(now.getTime() - (travel.backdate_days ?? 14) * 24 * 60 * MINUTE))} /></label>
    <div class="timezone-actions">
      ${differs && device !== travel.home && html`<button type="button" id="timezone-device" onClick=${event => run(event, { zone: device })}>${t("settings.tz.toDevice", { zone: device })}</button>`}
      ${travel.current !== travel.home && html`<button type="button" id="timezone-home" class="quiet" onClick=${event => run(event, { zone: "" })}>${t("settings.tz.toHome", { zone: travel.home })}</button>`}
      ${travel.switches?.length > 0 && html`<button type="button" id="timezone-undo" class="quiet" onClick=${event => run(event, { undo: true })}>${t("settings.tz.undo")}</button>`}
    </div><//>`;
}

function Vacation({ page, vacation, today, onVacation }) {
  const [day, setDay] = useState(today);
  const [saving, setSaving] = useState(false);
  const dateText = value => new Date(value + "T12:00").toLocaleDateString(locale);
  const earliest = new Date(today + "T12:00");
  earliest.setDate(earliest.getDate() - (vacation?.backdate_days ?? 14));
  const past = (vacation?.periods || []).filter(period => period.end_day).map(period => dateText(period.start_day) + "–" + dateText(period.end_day));
  async function toggle(event) {
    const active = event.currentTarget.checked;
    setSaving(true);
    try {
      const data = await post("/api/vacation", { active, day: day || null });
      toast(data.active ? t("settings.vacation.started") : t("settings.vacation.ended"));
      setDay(today);
      await onVacation(data);
    } catch (error) {
      toast(error.message, true);
    } finally {
      setSaving(false);
    }
  }
  return html`<${Panel} id="vacation" page=${page} title=${t("settings.pages.vacation")}>
    <label><span id="vacation-day-label">${vacation?.active ? t("settings.vacation.firstBack") : t("settings.vacation.firstDay")}</span>
      <input type="date" id="vacation-day" value=${day} max=${today} min=${earliest.toLocaleDateString("sv-SE")} onInput=${event => setDay(event.currentTarget.value)} /></label>
    <label class="check-label"><input type="checkbox" id="vacation-toggle" checked=${Boolean(vacation?.active)} disabled=${saving} onChange=${toggle} /> ${t("settings.vacation.active")}</label>
    <p id="vacation-status" class="hint">${(vacation?.active
      ? t("settings.vacation.activeSince", { day: dateText(vacation.since) })
      : t("settings.vacation.weekdays")) + (past.length ? " " + t("settings.vacation.past", { list: past.join(", ") }) : "")}</p><//>`;
}

// Pause after the trigger, in minutes: low after eating, high after an injection.
const NOTIFY_PAUSES = { low: t("settings.notify.pauseLow"), high: t("settings.notify.pauseHigh") };
const NOTIFY_KINDS = ["basal", "split", "low", "high", "check"].map(kind => [kind, t("settings.notify.kinds." + kind)]);

function Notifications({ page }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(null);
  const load = () => api("/api/notifications").then(setData).catch(failure => setError(failure.message));
  useEffect(() => { load(); }, []);
  async function save(kind, body) {
    setSaving(kind);
    try { setData(await post("/api/notifications", { kind, ...body })); }
    catch (failure) { toast(failure.message, true); load(); }
    finally { setSaving(null); }
  }
  const sendTest = event => busy(event, async () => toast((await post("/api/notifications/test", {})).message));
  return html`<${Panel} id="notify" page=${page} title=${t("settings.pages.notify")}>
    <p id="notify-status" class="hint notify-status">${data ? (data.configured ? t("settings.notify.ready")
      : t("settings.notify.missing")) : error}</p>
    ${NOTIFY_KINDS.map(([kind, label]) => html`<div class="notify-option">
      <label class="check-label"><input type="checkbox" class="notify-toggle" data-kind=${kind} checked=${Boolean(data?.enabled[kind])}
        disabled=${!data || saving === kind} onChange=${event => save(kind, { enabled: event.currentTarget.checked })} /> ${label}</label>
      ${kind === "basal" && html`<p id="notify-basal-hint" class="hint">${data ? t("settings.notify.basalHint", { time: data.basal_after }) : ""}</p>`}
      ${NOTIFY_PAUSES[kind] && html`<label class="notify-pause">${NOTIFY_PAUSES[kind]}
        <input type="number" class="notify-pause-input" data-kind=${kind} min="0" max="180" step="5" inputmode="numeric"
          value=${data ? String(data.pauses[kind]) : ""} disabled=${!data || saving === kind}
          onChange=${event => save(kind, { pause_minutes: Number(event.currentTarget.value) })} /> min</label>`}
      <select class="notify-priority" data-kind=${kind} aria-label=${t("settings.notify.levelFor", { label })} value=${data ? String(data.priorities[kind]) : ""}
        disabled=${!data || saving === kind} onChange=${event => save(kind, { priority: Number(event.currentTarget.value) })}>
        ${(data?.priority_options || []).map(option => html`<option value=${String(option.value)}>${t("settings.notify.level", { label: option.label })}</option>`)}</select></div>`)}
    <button type="button" id="notify-test" disabled=${!data?.configured} onClick=${sendTest}>${t("settings.notify.test")}</button>
    <ul id="notify-recent" class="hint">${(data?.recent || []).map(item => html`<li>${new Date(item.sent_at).toLocaleString(locale, { dateStyle: "short", timeStyle: "short" })} · ${item.label}: ${item.message}</li>`)}</ul><//>`;
}

function Agent({ page, info, onInfo }) {
  const [models, setModels] = useState({ image: [], audio: [] });
  if (!info) return html`<${Panel} id="agent" page=${page} title=${t("settings.pages.agent")} className="agent-panel" />`;
  const ready = info.providers.filter(item => item.configured);
  const current = info.providers.find(item => item.id === info.provider);
  const kinds = { estimate: t("settings.agent.estimates"), transcribe: t("settings.agent.transcriptions") };
  async function save(body) {
    try { onInfo(await post("/api/agent/settings", body)); }
    catch (error) { toast(error.message, true); onInfo({ ...info }); }
  }
  const loadModels = event => busy(event, async () => {
    const provider = info.provider;
    const data = await api("/api/agent/models?provider=" + encodeURIComponent(provider));
    const audio = provider === "openrouter" || !info.transcribe_available ? data.models
      : (await api("/api/agent/models?provider=openrouter")).models;
    setModels({ image: data.models.filter(model => model.image !== false), audio: audio.filter(model => model.audio) });
    toast(t("settings.agent.modelsLoaded", { count: data.models.length }));
  });
  return html`<${Panel} id="agent" page=${page} title=${t("settings.pages.agent")} className="agent-panel">
    <p id="agent-settings-status" class="hint">${!ready.length ? t("settings.agent.noProvider") : info.available ? "" : t("settings.agent.pickModel")}</p>
    <label>${t("settings.agent.provider")}<select id="agent-provider" value=${info.provider || ""} disabled=${!ready.length} onChange=${event => save({ provider: event.currentTarget.value })}>
      ${info.providers.map(item => html`<option value=${item.id} disabled=${!item.configured}>${item.label}${item.configured ? "" : " " + t("settings.agent.noKey")}</option>`)}</select></label>
    <label>${t("settings.agent.model")}<input type="text" id="agent-model" maxlength="120" list="agent-model-options" autocomplete="off" autocapitalize="off" spellcheck="false"
      value=${current?.model || ""} disabled=${!current} onChange=${event => save({ ["model_" + info.provider]: event.currentTarget.value })} /></label>
    <datalist id="agent-model-options">${models.image.map(model => html`<option value=${model.id}>${model.name}</option>`)}</datalist>
    <label class="check-label"><input type="checkbox" id="agent-web-search" checked=${info.web_search} onChange=${event => save({ web_search: event.currentTarget.checked })} /> ${t("settings.agent.webSearch")}</label>
    <label>${t("settings.agent.voiceModel")} <span>${t("settings.agent.viaOpenRouter")}</span><input type="text" id="agent-transcribe-model" maxlength="120" list="agent-audio-options"
      autocomplete="off" autocapitalize="off" spellcheck="false" value=${info.transcribe_model} disabled=${!info.transcribe_available}
      onChange=${event => save({ transcribe_model: event.currentTarget.value })} /></label>
    <datalist id="agent-audio-options">${models.audio.map(model => html`<option value=${model.id}>${model.name}</option>`)}</datalist>
    <div class="button-row"><button type="button" id="agent-models-load" disabled=${!current} onClick=${loadModels}>${t("settings.agent.loadModels")}</button></div>
    <p id="agent-usage" class="hint">${info.usage_month.length ? t("settings.agent.thisMonth") + " " + info.usage_month.map(row => t("settings.agent.usageRow", { calls: row.calls, kind: kinds[row.kind] || row.kind, model: row.model,
      tokens: num((row.input_tokens + row.output_tokens) / 1000, 0) }) + (row.cost_usd != null ? ", " + num(row.cost_usd, 2) + " $" : "") + ")").join("; ") : ""}</p><//>`;
}

// Places: chips with the spots the user defines, and the fallback place for anywhere else.
function Place({ place, act, maxAccuracy }) {
  const [coords, setCoords] = useState("");
  const toggle = (action, key) => event => act({ action, id: place.id, [key]: event.currentTarget.checked });
  const here = event => busy(event, async () => {
    let position;
    try { position = await currentPosition(); }
    catch (error) { throw new Error(error?.code === 1 ? t("settings.places.denied") : error?.message || t("settings.places.failed")); }
    if (position.accuracy_m > maxAccuracy) throw new Error(t("settings.places.inaccurate", { meters: position.accuracy_m }));
    await act({ action: "add_spot", place_id: place.id, lat: position.lat, lon: position.lon });
  });
  const factor = place.factor;
  return html`<div class="place-item"><div class="place-head">
      <input type="text" class="place-name" maxlength="40" value=${place.name} aria-label=${t("settings.places.name")} onChange=${event => act({ action: "rename", id: place.id, name: event.currentTarget.value })} />
      <span class="hint">${t("common.entries", { count: place.count })}</span>
      <label class="check-label place-quick"><input type="checkbox" class="place-quick-toggle" checked=${place.quick} onChange=${toggle("quick", "quick")} /> ${t("settings.places.quick")}</label>
      <label class="check-label place-quick"><input type="checkbox" class="place-fallback-toggle" checked=${place.fallback} onChange=${toggle("fallback", "fallback")} /> ${t("settings.places.fallback")}</label>
      <label class="check-label place-quick"><input type="checkbox" class="place-adjust-toggle" checked=${place.adjust} onChange=${toggle("adjust", "adjust")} /> ${t("settings.places.adjust")}</label>
      <button type="button" class="quiet delete-treatment" onClick=${() => confirm(t("settings.places.confirmDelete", { name: place.name })) && act({ action: "delete", id: place.id })}>${t("common.delete")}</button></div>
    ${place.adjust && html`<p class="hint">${factor?.scale != null
      ? t("settings.places.factor", { value: signedPercent(factor.scale), count: factor.count, days: factor.days })
      : t("settings.places.factorPending", { min: factor?.min_meals ?? 3, days: factor?.days ?? 60, count: factor?.count ?? 0 })}</p>`}
    ${place.spots.length
      ? html`<ul class="place-spots">${place.spots.map((spot, index) => html`<li class="place-spot"><a href=${osmLink(spot.lat, spot.lon)} target="_blank" rel="noopener">${t("settings.places.spot", { n: index + 1 })}</a>
        <button type="button" class="quiet" onClick=${() => act({ action: "delete_spot", spot_id: spot.id })}>${t("settings.places.remove")}</button></li>`)}</ul>`
      : html`<p class="hint">${place.fallback ? t("settings.places.fallbackHint") : t("settings.places.noSpot")}</p>`}
    <div class="place-coords"><button type="button" class="quiet" onClick=${here}>+ ${t("settings.places.here")}</button>
      <input type="text" class="place-coords-input" inputmode="decimal" placeholder=${t("settings.places.coordsPlaceholder")} aria-label=${t("settings.places.coords")}
        value=${coords} onInput=${event => setCoords(event.currentTarget.value)} />
      <button type="button" class="quiet" onClick=${async () => { if (await act({ action: "add_spot", place_id: place.id, coordinates: coords })) setCoords(""); }}>${t("settings.places.add")}</button></div></div>`;
}

function Places({ page, onPlacesChanged }) {
  const [data, setData] = useState(null);
  const [name, setName] = useState("");
  useEffect(() => { api("/api/places").then(setData).catch(error => toast(error.message, true)); }, []);
  async function act(body) {
    try {
      setData(await post("/api/places", body));
      onPlacesChanged();
      return true;
    } catch (error) {
      toast(error.message, true);
      setData(current => current && { ...current });
      return false;
    }
  }
  return html`<${Panel} id="places" page=${page} title=${t("settings.pages.places")} className="places-panel">
    <div id="places-list" aria-live="polite">${data && (data.places.length
      ? data.places.map(place => html`<${Place} key=${place.id} place=${place} act=${act} maxAccuracy=${data.max_accuracy_m} />`)
      : html`<p class="hint">${t("settings.places.none")}</p>`)}</div>
    <form id="place-new" class="place-new" onSubmit=${async event => { event.preventDefault(); if (await act({ action: "create", name })) setName(""); }}>
      <input name="name" type="text" maxlength="40" placeholder=${t("settings.places.newPlaceholder")} aria-label=${t("settings.places.newName")} required
        value=${name} onInput=${event => setName(event.currentTarget.value)} />
      <button type="submit">${t("settings.places.create")}</button></form><//>`;
}

// Language of the page (German default, English): "automatic" follows the browser; a choice is stored on
// this device and the page reloads in that language (web/ui/i18n.js).
function Language({ page }) {
  const stored = storedLanguage();
  return html`<${Panel} id="language" page=${page} title=${t("settings.pages.language")}>
    <label>${t("settings.language.label")}<select id="language-select" value=${stored || ""}
      onChange=${event => setLanguage(event.currentTarget.value || null)}>
      <option value="">${t("settings.language.auto", { language: LANGUAGES.find(([code]) => code === lang)?.[1] || lang })}</option>
      ${LANGUAGES.map(([code, name]) => html`<option value=${code}>${name}</option>`)}</select></label>
    <p class="hint">${t("settings.language.hint")}</p><//>`;
}

// ---- Nightscout-Profil ----
// The schedules are edited here and saved to Nightscout; I:C shows IE/KE and keeps Nightscout's g/IE
// for rows that were not changed, so a save does not round them.
function Schedule({ id, page, title, unit, rows, kind, profileId, onReload, onSaved }) {
  const list = useRef(null);
  const [status, setStatus] = useState(null);
  const isIc = kind === "ic";
  async function save(event) {
    const button = event?.currentTarget;
    const inputs = [...list.current.querySelectorAll("input")];
    if (button?.disabled || !inputs.every(input => input.reportValidity())) return;
    if (button) button.disabled = true;
    setStatus(t("settings.schedule.saving"));
    try {
      const result = await post("/api/profile/" + kind, { profile_id: profileId,
        [isIc ? "ic_schedule" : "isf_schedule"]: inputs.map(input => {
          const start_minute = Number(input.dataset.startMinute);
          if (!isIc) return { start_minute, mgdl_per_unit: glucoseValue(input) };
          if (Number(input.value) === Number(input.dataset.originalDisplay)) return { start_minute, grams_per_unit: input.dataset.gramsPerUnit };
          return gramsPerIe ? { start_minute, grams_per_unit: input.value } : { start_minute, units_per_ke: String(factorToUnitsPerKe(input.value)) };
        }) });
      await onReload({ day: false });
      onSaved();
      setStatus(result.message);
      toast(result.message);
    } catch (error) {
      setStatus(error.message);
      toast(error.message, true);
    } finally {
      if (button) button.disabled = false;
    }
  }
  const onKeyDown = event => {
    if (event.key === "Enter" && event.target.matches("input")) { event.preventDefault(); save(); }
  };
  return html`<${Panel} id=${id} page=${page} title=${title} unit=${unit}>
    <div id=${kind + "-rows"} ref=${list} aria-live=${isIc ? null : "polite"} onKeyDown=${onKeyDown}>${rows.length
      ? html`<div class="isf-table"><div class="isf-table-head"><span>${t("settings.schedule.from")}</span><span>${isIc ? t("settings.pages.ic") + " (" + UNIT.factor + ")" : t("settings.pages.isf")}</span></div>
        ${rows.map(row => {
          const start = Number(row.start_minute);
          const time = minuteClock(start);
          if (!isIc) return html`<label class="ic-row"><span>${time}</span><input type="number" min="0.001" max="1000" step="any" required
            data-start-minute=${start} data-mgdl=${row.mgdl_per_unit} defaultValue=${glInput(row.mgdl_per_unit)} aria-label=${t("settings.schedule.isfLabel", { time, unit: UNIT.insulin })} /></label>`;
          const display = Number(factorValue(10 / Number(row.grams_per_unit)).toFixed(gramsPerIe ? 1 : 2));
          return html`<label class="ic-row"><span>${time}</span><input type="number" min=${pickerMinimum(display, 0.1, true)} max="1000" step="0.1" required
            data-start-minute=${start} data-grams-per-unit=${row.grams_per_unit} data-original-display=${display} defaultValue=${display}
            aria-label=${t("settings.schedule.icLabel", { time, factor: UNIT.factor })}
            onInput=${event => { const input = event.currentTarget, value = Number(input.value);
              input.min = String(input.value && Number.isFinite(value) ? pickerMinimum(value, 0.1, true) : 0.1); }} /></label>`;
        })}</div>`
      : html`<p class="hint">${isIc ? t("settings.schedule.noIc") : t("settings.schedule.noIsf")}</p>`}</div>
    <button type="button" id=${"save-" + kind} disabled=${!rows.length} onClick=${save}>${t("entry.saveNightscout")}</button>
    ${isIc ? html`<p id="ic-status" class="hint">${status}</p>` : status && html`<p class="hint">${status}</p>`}<//>`;
}

// ---- Modell ----
const SPORT_LABELS = Object.fromEntries(sportKinds.map(kind => [kind, t("settings.sportShort." + kind)]));
const Field = ({ label, unit, children }) => html`<label>${label}${unit && html` <span>${unit}</span>`}${children}</label>`;
const NumberInput = props => html`<input type="number" required ...${props} />`;
// A glucose setting (stored in mg/dL) in the display unit. An unchanged field sends its stored value back, so
// rounding to mmol/L never moves a setting nobody touched.
const GlucoseInput = ({ name, min, max, step, defaultValue }) => html`<${NumberInput} name=${name} ...${glField(min, max, step)}
  defaultValue=${glInput(defaultValue)} data-mgdl=${defaultValue ?? ""} />`;
const glucoseValue = input => input.value !== "" && input.dataset.mgdl !== "" && Number(input.value) === Number(glInput(input.dataset.mgdl))
  ? input.dataset.mgdl : String(toMgdl(input.value));
// An uncontrolled select that starts at the stored value (the form is reset by remounting it).
const Choice = ({ name, value, options }) => html`<select name=${name}>${options.map(([key, label]) =>
  html`<option value=${key} selected=${key === value}>${label}</option>`)}</select>`;

function ModelForm({ page, data, onReload }) {
  const s = data.settings;
  async function submit(event) {
    event.preventDefault();
    const f = event.currentTarget.elements;
    try {
      const payload = {
        window_minutes: Number(f.window_minutes.value),
        glucose_unit: f.glucose_unit.value,
        carb_unit: f.carb_unit.value,
        dose_target_mgdl: glucoseValue(f.dose_target_mgdl),
        dose_cautious_target_mgdl: glucoseValue(f.dose_cautious_target_mgdl),
        low_limit_mgdl: glucoseValue(f.low_limit_mgdl),
        high_limit_mgdl: glucoseValue(f.high_limit_mgdl),
        auto_method: f.auto_method.value,
        auto_percentile: f.auto_percentile.value,
        auto_lookback_days: Number(f.auto_lookback_days.value),
        auto_half_life_days: f.auto_half_life_days.value,
        auto_surcharge_mgdl: glucoseValue(f.auto_surcharge_mgdl),
        auto_fat_surcharge_mgdl: glucoseValue(f.auto_fat_surcharge_mgdl),
        fpe_surcharge_mgdl: glucoseValue(f.fpe_surcharge_mgdl),
        auto_vacation: f.auto_vacation.value,
        day_types: f.day_types.checked,
        basal_default_units: f.basal_default_units.value,
        split_min_grams: Number(f.split_min_grams.value),
        split_min_units: Number(f.split_min_units.value),
        split_first_percent: Number(f.split_first_percent.value),
        split_delay_minutes: Number(f.split_delay_minutes.value),
        breakfast_end_minute: clockMinute(f.breakfast_end.value),
        lunch_end_minute: clockMinute(f.lunch_end.value),
        sport_lookback_minutes: Number(f.sport_lookback_minutes.value),
        sport_effects_mgdl: Object.fromEntries(sportKinds.map(kind => [kind, sportChange(glucoseValue(f["sport_" + kind]))])),
        sport_durations_minutes: Object.fromEntries(sportKinds.map(kind => [kind, Number(f["sport_minutes_" + kind].value)])),
        effective_at: f.effective_date.value ? toInstant(f.effective_date.value, f.effective_time.value || "00:00") : null,
      };
      await post("/api/settings", payload);
      toast(t("settings.saved"));
      await onReload({ day: true });
    } catch (error) {
      toast(error.message, true);
    }
  }
  return html`<form id="settings-form" onSubmit=${submit}>
    <div class="settings-grid settings-cols">
      <${Panel} id="dose" page=${page} title=${t("settings.pages.dose")}>
        <${Field} label=${t("settings.model.glucoseUnit")}><${Choice} name="glucose_unit" value=${s.glucose_unit || "mgdl"} options=${[["mgdl", "mg/dL"], ["mmol", "mmol/L"]]} /><//>
        <${Field} label=${t("settings.model.carbUnit")}><${Choice} name="carb_unit" value=${s.carb_unit || "ke"} options=${["ke", "be", "g"].map(key => [key, t("settings.model.carbUnits." + key)])} /><//>
        <${Field} label=${t("settings.model.target")} unit=${UNIT.glucose}><${GlucoseInput} name="dose_target_mgdl" min=${60} max=${140} step=${1} defaultValue=${s.dose_target_mgdl} /><//>
        <${Field} label=${t("settings.model.cautiousTarget")} unit=${UNIT.glucose}><${GlucoseInput} name="dose_cautious_target_mgdl" min=${60} max=${250} step=${1} defaultValue=${s.dose_cautious_target_mgdl} /><//>
        <${Field} label=${t("settings.model.lowLimit")} unit=${UNIT.glucose}><${GlucoseInput} name="low_limit_mgdl" min=${54} max=${90} step=${1} defaultValue=${s.low_limit_mgdl ?? 70} /><//>
        <${Field} label=${t("settings.model.highLimit")} unit=${UNIT.glucose}><${GlucoseInput} name="high_limit_mgdl" min=${180} max=${400} step=${1} defaultValue=${s.high_limit_mgdl ?? 250} /><//>
        <${Field} label=${t("settings.model.method")}><${Choice} name="auto_method" value=${s.auto_method || "percentile"} options=${[["percentile", t("analysis.tuning.percentile")], ["surcharge", t("settings.model.methodSurcharge")]]} /><//>
        <${Field} label=${t("settings.model.mealSurcharge")} unit=${UNIT.glucose}><${GlucoseInput} name="auto_surcharge_mgdl" min=${5} max=${150} step=${5} defaultValue=${s.auto_surcharge_mgdl ?? 40} /><//>
        <${Field} label=${t("settings.model.fatSurcharge")} unit=${UNIT.glucose}><${GlucoseInput} name="auto_fat_surcharge_mgdl" min=${0} max=${200} step=${10} defaultValue=${s.auto_fat_surcharge_mgdl ?? 120} /><//>
        <${Field} label=${t("settings.model.fpeSurcharge")} unit=${UNIT.glucose}><${GlucoseInput} name="fpe_surcharge_mgdl" min=${5} max=${60} step=${1} defaultValue=${s.fpe_surcharge_mgdl ?? 20} /><//>
        <${Field} label=${t("settings.model.percentile")} unit="%"><${NumberInput} name="auto_percentile" min="25" max="50" step="0.5" defaultValue=${s.auto_percentile} /><//>
        <${Field} label=${t("settings.model.lookback")}><${Choice} name="auto_lookback_days" value=${String(s.auto_lookback_days)} options=${[8, 15, 30].map(days => [String(days), t("common.days", { count: days })])} /><//>
        <${Field} label=${t("analysis.tuning.half_life")} unit=${t("settings.model.daysUnit")}><${NumberInput} name="auto_half_life_days" min="1" max="28" step="0.5" defaultValue=${s.auto_half_life_days} /><//>
        <${Field} label=${t("settings.model.vacation")}><${Choice} name="auto_vacation" value=${s.auto_vacation || "separate"} options=${["separate", "vacation_only", "all"].map(key => [key, t("settings.model.vacationOptions." + key)])} /><//>
        <${Field} label=${t("settings.model.basal")} unit=${UNIT.insulin}><${NumberInput} name="basal_default_units" min="0" max="100" step="0.5" defaultValue=${s.basal_default_units ?? 0} /><//>
        <${Field} label=${t("settings.model.splitFrom")} unit=${t("settings.model.gramsCarbs")}><${NumberInput} name="split_min_grams" min="0" max="300" step="5" defaultValue=${s.split_min_grams ?? 80} /><//>
        <${Field} label=${t("settings.model.splitUnits")} unit=${UNIT.insulin}><${NumberInput} name="split_min_units" min="0" max="30" step="0.5" defaultValue=${s.split_min_units ?? 6.5} /><//>
        <${Field} label=${t("settings.model.splitFirst")} unit="%"><${NumberInput} name="split_first_percent" min="30" max="90" step="5" defaultValue=${s.split_first_percent ?? 60} /><//>
        <${Field} label=${t("settings.model.splitDelay")} unit="min"><${NumberInput} name="split_delay_minutes" min="30" max="300" step="15" defaultValue=${s.split_delay_minutes ?? 120} /><//>
      <//>
      <${Panel} id="meals" page=${page} title=${t("settings.pages.meals")}>
        <${Field} label=${t("settings.model.breakfastEnd")} unit="24 h"><input name="breakfast_end" type="time" step="60" required defaultValue=${minuteClock(s.breakfast_end_minute)} /><//>
        <${Field} label=${t("settings.model.lunchEnd")} unit="24 h"><input name="lunch_end" type="time" step="60" required defaultValue=${minuteClock(s.lunch_end_minute)} /><//>
        <${Field} label=${t("settings.model.window")} unit="min"><${NumberInput} name="window_minutes" min="60" max="360" step="1" defaultValue=${s.window_minutes} /><//>
        <label class="check-label"><input type="checkbox" name="day_types" defaultChecked=${s.day_types === true} /> ${t("settings.model.dayTypes")}</label>
        <p class="hint">${t("settings.model.dayTypesHint")}</p>
      <//>
      <${Panel} id="sport" page=${page} title=${t("settings.pages.sport")} unit=${t("settings.model.sportUnit")}>
        <div class="field-pair">${sportKinds.map(kind => html`<${Field} label=${SPORT_LABELS[kind]}><${GlucoseInput} name=${"sport_" + kind} min=${-500} max=${500} step=${1}
          defaultValue=${sportChange(s.sport_effects_mgdl[kind])} /><//>`)}</div>
        <h3 class="settings-subhead">${t("sport.durationTitle")} <span class="unit">min</span></h3>
        <div class="field-pair">${sportKinds.map(kind => html`<${Field} label=${SPORT_LABELS[kind]}><${NumberInput} name=${"sport_minutes_" + kind} min="30" max="1440" step="1"
          defaultValue=${s.sport_durations_minutes?.[kind] ?? 300} /><//>`)}</div>
        <${Field} label=${t("settings.model.sportLookback")} unit=${t("settings.model.minutesAfter")}><${NumberInput} name="sport_lookback_minutes" min="0" max="1440" step="1" defaultValue=${s.sport_lookback_minutes} /><//>
      <//>
    </div>
    <div class="panel settings-save">
      <${Field} label=${t("settings.model.validFrom")} unit=${t("common.optional")}><input name="effective_date" type="date" /><//>
      <${Field} label=${t("settings.model.clock")} unit="24 h"><input name="effective_time" type="time" step="60" /><//>
      <button type="submit" class="primary">${t("settings.model.save")}</button>
      <p id="settings-version" class="hint">${t("settings.model.version", { version: data.version })}</p>
    </div></form>`;
}

// The whole settings page. `data` is /api/settings; `loadCount` changes with every fresh load, which
// resets the model form to the stored values.
export function SettingsView({ data, loadCount, page, onPage, today, agentInfo, onAgentInfo, onReload, onSwitchZone, onVacation,
  onPlacesChanged, onProfileSaved }) {
  if (!data) return null;
  const settings = data.settings;
  return html`<div class="page-head"><div><h1>${t("settings.title")}</h1></div></div>
    <nav id="settings-index" class="settings-index" aria-label=${t("settings.settings")}><${SettingsIndex} onOpen=${onPage} /></nav>
    <button type="button" id="settings-back" class="quiet settings-back" hidden=${!page} onClick=${() => onPage(null)}>‹ ${t("settings.settings")}</button>
    <div class="settings-grid settings-top">
      <${Nightscout} page=${page} data=${data} onReload=${onReload} />
      <${Timezone} page=${page} travel=${data.travel} onSwitchZone=${onSwitchZone} />
      <${Vacation} key=${"vacation-" + loadCount} page=${page} vacation=${data.vacation} today=${today} onVacation=${onVacation} />
      <${Notifications} page=${page} />
      <${Agent} page=${page} info=${agentInfo} onInfo=${onAgentInfo} />
      <${Places} page=${page} onPlacesChanged=${onPlacesChanged} />
      <${Language} page=${page} />
      <${ThemePanel} page=${page} />
    </div>
    <h2 class="settings-heading">${t("settings.groups.profile")}</h2>
    <div class="settings-grid settings-cols" key=${"profile-" + loadCount}>
      <${Schedule} id="isf" kind="isf" page=${page} title=${t("settings.pages.isf")} unit=${t("settings.schedule.isfUnit", { unit: UNIT.insulin })} rows=${settings.isf_schedule || []}
        profileId=${settings.nightscout_profile?.profile_id} onReload=${onReload} onSaved=${onProfileSaved} />
      <${Schedule} id="ic" kind="ic" page=${page} title=${t("settings.pages.ic")} unit=${UNIT.factor} rows=${settings.ic_schedule || []}
        profileId=${settings.nightscout_profile?.profile_id} onReload=${onReload} onSaved=${onProfileSaved} />
      <${Panel} id="target" page=${page} title=${t("settings.pages.target")}><div id="target-rows">${(settings.target_schedule || []).length
        ? html`<div class="isf-table"><div class="isf-table-head"><span>${t("settings.schedule.from")}</span><span>${t("settings.pages.target")}</span></div>${settings.target_schedule.map(row =>
          html`<div class="isf-readonly-row"><span>${minuteClock(Number(row.start_minute))}</span><strong>${glNum(row.low_mgdl, 0)}–${gl(row.high_mgdl, 0)}</strong></div>`)}</div>`
        : html`<p class="hint">${t("settings.schedule.noTarget")}</p>`}</div><//>
    </div>
    <h2 class="settings-heading">${t("settings.groups.model")}</h2>
    <${ModelForm} key=${"model-" + loadCount} page=${page} data=${data} onReload=${onReload} />
    <form id="logout-form" class="logout-row" action="/logout" method="post" hidden=${!data.login_required}><button class="quiet" type="submit">${t("settings.logout")}</button></form>`;
}
