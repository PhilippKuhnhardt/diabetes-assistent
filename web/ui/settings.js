// "Mehr": profile and model settings. Wide screens show every panel; phones show a list of pages and
// one page at a time (app.js keeps which page is open and marks #settings-view for the CSS).
// Every value comes from /api/settings and its neighbours; saving sends it back and the server checks it.
import { html, useState, useEffect, useRef } from "./html.js";
import { api, post, toast, busy, num, signedPercent, sportKinds, sportChange, minuteClock, clockMinute, toInstant, localInputTime,
  pickerMinimum, currentPosition, MINUTE } from "./lib.js";

// The pages in the order they appear; `form` pages belong to the model form and its save button.
export const SETTINGS_PAGES = [
  { group: "Allgemein", pages: [["nightscout", "Nightscout"], ["timezone", "Zeitzone"], ["vacation", "Urlaub"],
    ["notify", "Benachrichtigungen"], ["agent", "Essens-Agent"], ["places", "Orte"]] },
  { group: "Nightscout-Profil", pages: [["isf", "Korrekturfaktor"], ["ic", "Insulinfaktor"], ["target", "Zielbereich"]] },
  { group: "Modell", form: true, pages: [["dose", "Spritzhilfe"], ["meals", "Mahlzeiten"], ["sport", "Sport"]] },
];
export const FORM_PAGES = new Set(SETTINGS_PAGES.find(group => group.form).pages.map(([id]) => id));

const osmLink = (lat, lon) => "https://www.openstreetmap.org/?mlat=" + lat + "&mlon=" + lon + "#map=18/" + lat + "/" + lon;
const stamp = value => new Date(value).toLocaleString("de-DE");

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
    ? "Profil „" + profile.profile_name + "“ · gültig ab " + stamp(profile.start_at) + (profile.timezone ? " · " + profile.timezone : "")
    : "Nightscout-Profil nicht verfügbar.") + (data.profile_sync_at ? " · zuletzt synchronisiert " + stamp(data.profile_sync_at) : "");
  async function testConnection() {
    setTest("Prüfe …");
    try { setTest((await api("/api/test-connection")).message); } catch (error) { setTest(error.message); }
  }
  async function syncProfile(event) {
    const button = event.currentTarget;
    button.disabled = true;
    setStatus("Nightscout-Profil wird synchronisiert …");
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
    <p id="nightscout-location" class="mono" hidden=${data.connected}>${data.connected ? "" : "NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL setzen"}</p>
    <div class="button-row"><button id="test-connection" type="button" onClick=${testConnection}>Verbindung prüfen</button>
      <button type="button" id="sync-profile" onClick=${syncProfile}>Profil synchronisieren</button></div>
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
  if (!travel) return html`<${Panel} id="timezone" page=${page} title="Zeitzone" />`;
  const device = deviceZone();
  const differs = deviceZoneDiffers(travel);
  const last = travel.switches?.[travel.switches.length - 1];
  const since = last ? " seit " + new Date(last.at).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "";
  const now = new Date();
  const run = (event, payload) => busy(event, async () => {
    if (at && !payload.undo) payload.at = new Date(at).toISOString();
    if (await onSwitchZone(payload)) setAt("");
  });
  return html`<${Panel} id="timezone" page=${page} title="Zeitzone">
    <p id="timezone-status" class="hint">Die App rechnet mit ${zoneText(travel.current)}${travel.current !== travel.home ? since + ", Heimat ist " + zoneText(travel.home) : ""}.${differs ? " Dein Gerät ist auf " + zoneText(device) + "." : ""}</p>
    <label>Gilt ab (leer = jetzt)<input type="datetime-local" id="timezone-at" value=${at} onInput=${event => setAt(event.currentTarget.value)}
      max=${localInputTime(now)} min=${localInputTime(new Date(now.getTime() - (travel.backdate_days ?? 14) * 24 * 60 * MINUTE))} /></label>
    <div class="timezone-actions">
      ${differs && device !== travel.home && html`<button type="button" id="timezone-device" onClick=${event => run(event, { zone: device })}>Auf Gerätezeit umstellen: ${device}</button>`}
      ${travel.current !== travel.home && html`<button type="button" id="timezone-home" class="quiet" onClick=${event => run(event, { zone: "" })}>Zurück auf Heimatzeit: ${travel.home}</button>`}
      ${travel.switches?.length > 0 && html`<button type="button" id="timezone-undo" class="quiet" onClick=${event => run(event, { undo: true })}>Letzten Wechsel zurücknehmen</button>`}
    </div><//>`;
}

function Vacation({ page, vacation, today, onVacation }) {
  const [day, setDay] = useState(today);
  const [saving, setSaving] = useState(false);
  const dateText = value => new Date(value + "T12:00").toLocaleDateString("de-DE");
  const earliest = new Date(today + "T12:00");
  earliest.setDate(earliest.getDate() - (vacation?.backdate_days ?? 14));
  const past = (vacation?.periods || []).filter(period => period.end_day).map(period => dateText(period.start_day) + "–" + dateText(period.end_day));
  async function toggle(event) {
    const active = event.currentTarget.checked;
    setSaving(true);
    try {
      const data = await post("/api/vacation", { active, day: day || null });
      toast(data.active ? "Urlaub aktiviert: jeder Tag zählt als frei" : "Urlaub beendet");
      setDay(today);
      await onVacation(data);
    } catch (error) {
      toast(error.message, true);
    } finally {
      setSaving(false);
    }
  }
  return html`<${Panel} id="vacation" page=${page} title="Urlaub">
    <label><span id="vacation-day-label">${vacation?.active ? "Erster Tag zurück" : "Erster Urlaubstag"}</span>
      <input type="date" id="vacation-day" value=${day} max=${today} min=${earliest.toLocaleDateString("sv-SE")} onInput=${event => setDay(event.currentTarget.value)} /></label>
    <label class="check-label"><input type="checkbox" id="vacation-toggle" checked=${Boolean(vacation?.active)} disabled=${saving} onChange=${toggle} /> Urlaub aktiv</label>
    <p id="vacation-status" class="hint">${(vacation?.active
      ? "Aktiv seit " + dateText(vacation.since) + ". Jeder Tag zählt als frei, Mahlzeiten sind als Urlaub markiert."
      : "Mo–Fr zählt als Arbeitstag.") + (past.length ? " Frühere Urlaube: " + past.join(", ") + "." : "")}</p><//>`;
}

// Pause after the trigger, in minutes (Philipp 2026-11-22): low after eating, high after an injection.
const NOTIFY_PAUSES = { low: "Pause nach dem Essen", high: "Pause nach einer Spritze" };
const NOTIFY_KINDS = [["basal", "Basal vergessen"], ["split", "2. Split-Teil fällig"], ["low", "Tief droht"], ["high", "Hoch droht"], ["check", "BZ prüfen"]];

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
  return html`<${Panel} id="notify" page=${page} title="Benachrichtigungen">
    <p id="notify-status" class="hint notify-status">${data ? (data.configured ? "Pushover ist eingerichtet."
      : "Pushover ist nicht eingerichtet: PUSHOVER_APP_TOKEN und PUSHOVER_USER_KEY im Deployment setzen.") : error}</p>
    ${NOTIFY_KINDS.map(([kind, label]) => html`<div class="notify-option">
      <label class="check-label"><input type="checkbox" class="notify-toggle" data-kind=${kind} checked=${Boolean(data?.enabled[kind])}
        disabled=${!data || saving === kind} onChange=${event => save(kind, { enabled: event.currentTarget.checked })} /> ${label}</label>
      ${kind === "basal" && html`<p id="notify-basal-hint" class="hint">${data ? "Wenn bis " + data.basal_after + " (Frühstücksende) heute noch kein Basal erfasst ist." : ""}</p>`}
      ${NOTIFY_PAUSES[kind] && html`<label class="notify-pause">${NOTIFY_PAUSES[kind]}
        <input type="number" class="notify-pause-input" data-kind=${kind} min="0" max="180" step="5" inputmode="numeric"
          value=${data ? String(data.pauses[kind]) : ""} disabled=${!data || saving === kind}
          onChange=${event => save(kind, { pause_minutes: Number(event.currentTarget.value) })} /> min</label>`}
      <select class="notify-priority" data-kind=${kind} aria-label=${"Stufe für " + label} value=${data ? String(data.priorities[kind]) : ""}
        disabled=${!data || saving === kind} onChange=${event => save(kind, { priority: Number(event.currentTarget.value) })}>
        ${(data?.priority_options || []).map(option => html`<option value=${String(option.value)}>Stufe: ${option.label}</option>`)}</select></div>`)}
    <button type="button" id="notify-test" disabled=${!data?.configured} onClick=${sendTest}>Testmeldung senden</button>
    <ul id="notify-recent" class="hint">${(data?.recent || []).map(item => html`<li>${new Date(item.sent_at).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })} · ${item.label}: ${item.message}</li>`)}</ul><//>`;
}

function Agent({ page, info, onInfo }) {
  const [models, setModels] = useState({ image: [], audio: [] });
  if (!info) return html`<${Panel} id="agent" page=${page} title="Essens-Agent" className="agent-panel" />`;
  const ready = info.providers.filter(item => item.configured);
  const current = info.providers.find(item => item.id === info.provider);
  const kinds = { estimate: "Einschätzungen", transcribe: "Transkriptionen" };
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
    toast(data.models.length + " Modelle geladen, im Feld Modell auswählen oder tippen");
  });
  return html`<${Panel} id="agent" page=${page} title="Essens-Agent" className="agent-panel">
    <p id="agent-settings-status" class="hint">${!ready.length ? "Noch kein Anbieter eingerichtet." : info.available ? "" : "Bitte ein Modell wählen."}</p>
    <label>Anbieter<select id="agent-provider" value=${info.provider || ""} disabled=${!ready.length} onChange=${event => save({ provider: event.currentTarget.value })}>
      ${info.providers.map(item => html`<option value=${item.id} disabled=${!item.configured}>${item.label}${item.configured ? "" : " (kein Schlüssel)"}</option>`)}</select></label>
    <label>Modell<input type="text" id="agent-model" maxlength="120" list="agent-model-options" autocomplete="off" autocapitalize="off" spellcheck="false"
      value=${current?.model || ""} disabled=${!current} onChange=${event => save({ ["model_" + info.provider]: event.currentTarget.value })} /></label>
    <datalist id="agent-model-options">${models.image.map(model => html`<option value=${model.id}>${model.name}</option>`)}</datalist>
    <label class="check-label"><input type="checkbox" id="agent-web-search" checked=${info.web_search} onChange=${event => save({ web_search: event.currentTarget.checked })} /> Websuche erlauben (Nährwerte von Produkten und Restaurants)</label>
    <label>Modell für Sprachaufnahmen <span>über OpenRouter</span><input type="text" id="agent-transcribe-model" maxlength="120" list="agent-audio-options"
      autocomplete="off" autocapitalize="off" spellcheck="false" value=${info.transcribe_model} disabled=${!info.transcribe_available}
      onChange=${event => save({ transcribe_model: event.currentTarget.value })} /></label>
    <datalist id="agent-audio-options">${models.audio.map(model => html`<option value=${model.id}>${model.name}</option>`)}</datalist>
    <div class="button-row"><button type="button" id="agent-models-load" disabled=${!current} onClick=${loadModels}>Modelle des Anbieters laden</button></div>
    <p id="agent-usage" class="hint">${info.usage_month.length ? "Diesen Monat: " + info.usage_month.map(row => row.calls + " " + (kinds[row.kind] || row.kind) + " mit " + row.model +
      " (" + num((row.input_tokens + row.output_tokens) / 1000, 0) + " Tsd. Token" + (row.cost_usd != null ? ", " + num(row.cost_usd, 2) + " $" : "") + ")").join("; ") : ""}</p><//>`;
}

// Places: chips with the spots Philipp defines, and the fallback place for anywhere else.
function Place({ place, act, maxAccuracy }) {
  const [coords, setCoords] = useState("");
  const toggle = (action, key) => event => act({ action, id: place.id, [key]: event.currentTarget.checked });
  const here = event => busy(event, async () => {
    let position;
    try { position = await currentPosition(); }
    catch (error) { throw new Error(error?.code === 1 ? "Standort nicht freigegeben" : error?.message || "Standort nicht ermittelt"); }
    if (position.accuracy_m > maxAccuracy) throw new Error("Standort zu ungenau (± " + position.accuracy_m + " m). Bitte draußen oder mit WLAN erneut versuchen.");
    await act({ action: "add_spot", place_id: place.id, lat: position.lat, lon: position.lon });
  });
  const factor = place.factor;
  return html`<div class="place-item"><div class="place-head">
      <input type="text" class="place-name" maxlength="40" value=${place.name} aria-label="Name des Orts" onChange=${event => act({ action: "rename", id: place.id, name: event.currentTarget.value })} />
      <span class="hint">${place.count}${place.count === 1 ? " Eintrag" : " Einträge"}</span>
      <label class="check-label place-quick"><input type="checkbox" class="place-quick-toggle" checked=${place.quick} onChange=${toggle("quick", "quick")} /> Schnellauswahl</label>
      <label class="check-label place-quick"><input type="checkbox" class="place-fallback-toggle" checked=${place.fallback} onChange=${toggle("fallback", "fallback")} /> Standard woanders</label>
      <label class="check-label place-quick"><input type="checkbox" class="place-adjust-toggle" checked=${place.adjust} onChange=${toggle("adjust", "adjust")} /> Ortsfaktor</label>
      <button type="button" class="quiet delete-treatment" onClick=${() => confirm("Ort „" + place.name + "“ löschen? Seine Einträge verlieren den Ort.") && act({ action: "delete", id: place.id })}>Löschen</button></div>
    ${place.adjust && html`<p class="hint">${factor?.scale != null
      ? "Ortsfaktor " + signedPercent(factor.scale) + " auf Faktor und Zuschlag, aus " + factor.count + " Hauptmahlzeiten der letzten " + factor.days + " Tage."
      : "Ortsfaktor ab " + (factor?.min_meals ?? 3) + " Hauptmahlzeiten hier in den letzten " + (factor?.days ?? 60) + " Tagen (bisher " + (factor?.count ?? 0) + ")."}</p>`}
    ${place.spots.length
      ? html`<ul class="place-spots">${place.spots.map((spot, index) => html`<li class="place-spot"><a href=${osmLink(spot.lat, spot.lon)} target="_blank" rel="noopener">Standort ${index + 1}</a>
        <button type="button" class="quiet" onClick=${() => act({ action: "delete_spot", spot_id: spot.id })}>Entfernen</button></li>`)}</ul>`
      : html`<p class="hint">${place.fallback ? "Vorausgewählt, wenn du an keinem Standort bist." : "Ohne Standort: nur von Hand wählbar."}</p>`}
    <div class="place-coords"><button type="button" class="quiet" onClick=${here}>+ Aktueller Standort</button>
      <input type="text" class="place-coords-input" inputmode="decimal" placeholder="oder Koordinaten: 49.0069, 8.4037" aria-label="Koordinaten des Standorts"
        value=${coords} onInput=${event => setCoords(event.currentTarget.value)} />
      <button type="button" class="quiet" onClick=${async () => { if (await act({ action: "add_spot", place_id: place.id, coordinates: coords })) setCoords(""); }}>Hinzufügen</button></div></div>`;
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
  return html`<${Panel} id="places" page=${page} title="Orte" className="places-panel">
    <div id="places-list" aria-live="polite">${data && (data.places.length
      ? data.places.map(place => html`<${Place} key=${place.id} place=${place} act=${act} maxAccuracy=${data.max_accuracy_m} />`)
      : html`<p class="hint">Noch keine Orte.</p>`)}</div>
    <form id="place-new" class="place-new" onSubmit=${async event => { event.preventDefault(); if (await act({ action: "create", name })) setName(""); }}>
      <input name="name" type="text" maxlength="40" placeholder="Neuer Ort, z. B. Kantine" aria-label="Name des neuen Orts" required
        value=${name} onInput=${event => setName(event.currentTarget.value)} />
      <button type="submit">Anlegen</button></form><//>`;
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
    setStatus("Profil-Zeitplan wird in Nightscout gespeichert …");
    try {
      const result = await post("/api/profile/" + kind, { profile_id: profileId,
        [isIc ? "ic_schedule" : "isf_schedule"]: inputs.map(input => {
          const start_minute = Number(input.dataset.startMinute);
          if (!isIc) return { start_minute, mgdl_per_unit: input.value };
          return Number(input.value) === Number(input.dataset.originalDisplay)
            ? { start_minute, grams_per_unit: input.dataset.gramsPerUnit } : { start_minute, units_per_ke: input.value };
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
      ? html`<div class="isf-table"><div class="isf-table-head"><span>Ab</span><span>${isIc ? "Insulinfaktor (IE/KE)" : "Korrekturfaktor"}</span></div>
        ${rows.map(row => {
          const start = Number(row.start_minute);
          const time = minuteClock(start);
          if (!isIc) return html`<label class="ic-row"><span>${time}</span><input type="number" min="0.001" max="1000" step="any" required
            data-start-minute=${start} defaultValue=${row.mgdl_per_unit} aria-label=${"Korrekturfaktor ab " + time + " in mg/dL je IE"} /></label>`;
          const display = Number((10 / Number(row.grams_per_unit)).toFixed(2));
          return html`<label class="ic-row"><span>${time}</span><input type="number" min=${pickerMinimum(display, 0.1, true)} max="1000" step="0.1" required
            data-start-minute=${start} data-grams-per-unit=${row.grams_per_unit} data-original-display=${display} defaultValue=${display}
            aria-label=${"Insulinfaktor ab " + time + " in IE je KE"}
            onInput=${event => { const input = event.currentTarget, value = Number(input.value);
              input.min = String(input.value && Number.isFinite(value) ? pickerMinimum(value, 0.1, true) : 0.1); }} /></label>`;
        })}</div>`
      : html`<p class="hint">${isIc ? "Kein I:C-Zeitplan aus Nightscout verfügbar." : "Kein Korrekturfaktor-Profil aus Nightscout im lokalen Cache."}</p>`}</div>
    <button type="button" id=${"save-" + kind} disabled=${!rows.length} onClick=${save}>In Nightscout speichern</button>
    ${isIc ? html`<p id="ic-status" class="hint">${status}</p>` : status && html`<p class="hint">${status}</p>`}<//>`;
}

// ---- Modell ----
const SPORT_LABELS = { jogging: "Joggen", gym: "Gym", light_cardio: "Cardio", racing: "Rennsport" };
const Field = ({ label, unit, children }) => html`<label>${label}${unit && html` <span>${unit}</span>`}${children}</label>`;
const NumberInput = props => html`<input type="number" required ...${props} />`;
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
        dose_target_mgdl: f.dose_target_mgdl.value,
        dose_cautious_target_mgdl: f.dose_cautious_target_mgdl.value,
        dose_low_target_mgdl: f.dose_low_target_mgdl.value,
        auto_method: f.auto_method.value,
        auto_percentile: f.auto_percentile.value,
        auto_lookback_days: Number(f.auto_lookback_days.value),
        auto_half_life_days: f.auto_half_life_days.value,
        auto_surcharge_mgdl: f.auto_surcharge_mgdl.value,
        auto_fat_surcharge_mgdl: f.auto_fat_surcharge_mgdl.value,
        fpe_surcharge_mgdl: f.fpe_surcharge_mgdl.value,
        auto_vacation: f.auto_vacation.value,
        basal_default_units: f.basal_default_units.value,
        split_min_grams: Number(f.split_min_grams.value),
        split_min_units: Number(f.split_min_units.value),
        split_first_percent: Number(f.split_first_percent.value),
        split_delay_minutes: Number(f.split_delay_minutes.value),
        breakfast_end_minute: clockMinute(f.breakfast_end.value),
        lunch_end_minute: clockMinute(f.lunch_end.value),
        sport_lookback_minutes: Number(f.sport_lookback_minutes.value),
        sport_effects_mgdl: Object.fromEntries(sportKinds.map(kind => [kind, sportChange(f["sport_" + kind].value)])),
        sport_durations_minutes: Object.fromEntries(sportKinds.map(kind => [kind, Number(f["sport_minutes_" + kind].value)])),
        effective_at: f.effective_date.value ? toInstant(f.effective_date.value, f.effective_time.value || "00:00") : null,
      };
      await post("/api/settings", payload);
      toast("Einstellungen gespeichert");
      await onReload({ day: true });
    } catch (error) {
      toast(error.message, true);
    }
  }
  return html`<form id="settings-form" onSubmit=${submit}>
    <div class="settings-grid settings-cols">
      <${Panel} id="dose" page=${page} title="Spritzhilfe">
        <${Field} label="Zielwert" unit="mg/dL"><${NumberInput} name="dose_target_mgdl" min="60" max="140" step="1" defaultValue=${s.dose_target_mgdl} /><//>
        <${Field} label="Vorsichtiger Zielwert" unit="mg/dL"><${NumberInput} name="dose_cautious_target_mgdl" min="60" max="250" step="1" defaultValue=${s.dose_cautious_target_mgdl} /><//>
        <${Field} label="Tieferer Zielwert" unit="mg/dL"><${NumberInput} name="dose_low_target_mgdl" min="60" max="140" step="1" defaultValue=${s.dose_low_target_mgdl ?? 100} /><//>
        <${Field} label="Automatik-Methode"><${Choice} name="auto_method" value=${s.auto_method || "percentile"} options=${[["percentile", "Perzentil"], ["surcharge", "Perzentil + Mahlzeitzuschlag"]]} /><//>
        <${Field} label="Mahlzeitzuschlag" unit="mg/dL"><${NumberInput} name="auto_surcharge_mgdl" min="5" max="150" step="5" defaultValue=${s.auto_surcharge_mgdl ?? 40} /><//>
        <${Field} label="Fett-Zuschlag" unit="mg/dL"><${NumberInput} name="auto_fat_surcharge_mgdl" min="0" max="200" step="10" defaultValue=${s.auto_fat_surcharge_mgdl ?? 120} /><//>
        <${Field} label="Zuschlag je FPE (berechnet)" unit="mg/dL"><${NumberInput} name="fpe_surcharge_mgdl" min="5" max="60" step="1" defaultValue=${s.fpe_surcharge_mgdl ?? 20} /><//>
        <${Field} label="Automatik-Perzentil" unit="%"><${NumberInput} name="auto_percentile" min="25" max="50" step="0.5" defaultValue=${s.auto_percentile} /><//>
        <${Field} label="Automatik-Zeitraum"><${Choice} name="auto_lookback_days" value=${String(s.auto_lookback_days)} options=${[["8", "8 Tage"], ["15", "15 Tage"], ["30", "30 Tage"]]} /><//>
        <${Field} label="Halbwertszeit" unit="Tage"><${NumberInput} name="auto_half_life_days" min="1" max="28" step="0.5" defaultValue=${s.auto_half_life_days} /><//>
        <${Field} label="Urlaub in der Automatik"><${Choice} name="auto_vacation" value=${s.auto_vacation || "separate"} options=${[["separate", "Im Urlaub alle Mahlzeiten, danach ohne Urlaub"],
          ["vacation_only", "Im Urlaub nur Urlaubsmahlzeiten, danach ohne Urlaub"], ["all", "Urlaub zählt wie jeder andere Tag"]]} /><//>
        <${Field} label="Basal zum Frühstück" unit="IE"><${NumberInput} name="basal_default_units" min="0" max="100" step="0.5" defaultValue=${s.basal_default_units ?? 0} /><//>
        <${Field} label="Split-Bolus ab" unit="g KH"><${NumberInput} name="split_min_grams" min="0" max="300" step="5" defaultValue=${s.split_min_grams ?? 80} /><//>
        <${Field} label="Split nur ab" unit="IE"><${NumberInput} name="split_min_units" min="0" max="30" step="0.5" defaultValue=${s.split_min_units ?? 6.5} /><//>
        <${Field} label="Split: Anteil jetzt" unit="%"><${NumberInput} name="split_first_percent" min="30" max="90" step="5" defaultValue=${s.split_first_percent ?? 60} /><//>
        <${Field} label="Split: 2. Teil nach" unit="min"><${NumberInput} name="split_delay_minutes" min="30" max="300" step="15" defaultValue=${s.split_delay_minutes ?? 120} /><//>
      <//>
      <${Panel} id="meals" page=${page} title="Mahlzeiten">
        <${Field} label="Frühstück endet um" unit="24 h"><input name="breakfast_end" type="time" step="60" required defaultValue=${minuteClock(s.breakfast_end_minute)} /><//>
        <${Field} label="Mittagessen endet um" unit="24 h"><input name="lunch_end" type="time" step="60" required defaultValue=${minuteClock(s.lunch_end_minute)} /><//>
        <${Field} label="Auswertung nach" unit="min"><${NumberInput} name="window_minutes" min="60" max="360" step="1" defaultValue=${s.window_minutes} /><//>
      <//>
      <${Panel} id="sport" page=${page} title="Sport" unit="BZ-Wirkung in mg/dL">
        <div class="field-pair">${sportKinds.map(kind => html`<${Field} label=${SPORT_LABELS[kind]}><${NumberInput} name=${"sport_" + kind} min="-500" max="500" step="1"
          defaultValue=${sportChange(s.sport_effects_mgdl[kind])} /><//>`)}</div>
        <h3 class="settings-subhead">Wirkdauer nach dem Ende <span class="unit">min</span></h3>
        <div class="field-pair">${sportKinds.map(kind => html`<${Field} label=${SPORT_LABELS[kind]}><${NumberInput} name=${"sport_minutes_" + kind} min="30" max="1440" step="1"
          defaultValue=${s.sport_durations_minutes?.[kind] ?? 300} /><//>`)}</div>
        <${Field} label="Sport-Auswertung: Mahlzeiten bis" unit="min danach"><${NumberInput} name="sport_lookback_minutes" min="0" max="1440" step="1" defaultValue=${s.sport_lookback_minutes} /><//>
      <//>
    </div>
    <div class="panel settings-save">
      <${Field} label="Gültig ab" unit="optional"><input name="effective_date" type="date" /><//>
      <${Field} label="Uhrzeit" unit="24 h"><input name="effective_time" type="time" step="60" /><//>
      <button type="submit" class="primary">Einstellungen speichern</button>
      <p id="settings-version" class="hint">Einstellungsversion ${data.version}</p>
    </div></form>`;
}

// The whole settings page. `data` is /api/settings; `loadCount` changes with every fresh load, which
// resets the model form to the stored values.
export function SettingsView({ data, loadCount, page, onPage, today, agentInfo, onAgentInfo, onReload, onSwitchZone, onVacation,
  onPlacesChanged, onProfileSaved }) {
  if (!data) return null;
  const settings = data.settings;
  return html`<div class="page-head"><div><h1>Profil & Modell</h1></div></div>
    <nav id="settings-index" class="settings-index" aria-label="Einstellungen"><${SettingsIndex} onOpen=${onPage} /></nav>
    <button type="button" id="settings-back" class="quiet settings-back" hidden=${!page} onClick=${() => onPage(null)}>‹ Einstellungen</button>
    <div class="settings-grid settings-top">
      <${Nightscout} page=${page} data=${data} onReload=${onReload} />
      <${Timezone} page=${page} travel=${data.travel} onSwitchZone=${onSwitchZone} />
      <${Vacation} key=${"vacation-" + loadCount} page=${page} vacation=${data.vacation} today=${today} onVacation=${onVacation} />
      <${Notifications} page=${page} />
      <${Agent} page=${page} info=${agentInfo} onInfo=${onAgentInfo} />
      <${Places} page=${page} onPlacesChanged=${onPlacesChanged} />
    </div>
    <h2 class="settings-heading">Nightscout-Profil</h2>
    <div class="settings-grid settings-cols" key=${"profile-" + loadCount}>
      <${Schedule} id="isf" kind="isf" page=${page} title="Korrekturfaktor" unit="mg/dL je IE" rows=${settings.isf_schedule || []}
        profileId=${settings.nightscout_profile?.profile_id} onReload=${onReload} onSaved=${onProfileSaved} />
      <${Schedule} id="ic" kind="ic" page=${page} title="Insulinfaktor" unit="IE je KE" rows=${settings.ic_schedule || []}
        profileId=${settings.nightscout_profile?.profile_id} onReload=${onReload} onSaved=${onProfileSaved} />
      <${Panel} id="target" page=${page} title="Zielbereich"><div id="target-rows">${(settings.target_schedule || []).length
        ? html`<div class="isf-table"><div class="isf-table-head"><span>Ab</span><span>Zielbereich</span></div>${settings.target_schedule.map(row =>
          html`<div class="isf-readonly-row"><span>${minuteClock(Number(row.start_minute))}</span><strong>${num(row.low_mgdl, 0)}–${num(row.high_mgdl, 0)} mg/dL</strong></div>`)}</div>`
        : html`<p class="hint">Kein Zielbereich im aktuellen Nightscout-Profil verfügbar.</p>`}</div><//>
    </div>
    <h2 class="settings-heading">Modell</h2>
    <${ModelForm} key=${"model-" + loadCount} page=${page} data=${data} onReload=${onReload} />
    <form id="logout-form" class="logout-row" action="/logout" method="post" hidden=${!data.login_required}><button class="quiet" type="submit">Abmelden</button></form>`;
}
