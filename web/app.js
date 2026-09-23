"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { day: new Date().toLocaleDateString("sv-SE"), view: "diary", settings: null, dayData: null, pendingMealId: null, pendingSportId: null };
const views = new Set(["diary", "analysis", "settings"]);
const names = { breakfast: "Frühstück", lunch: "Mittagessen", dinner: "Abendessen" };
const num = (value, digits = 2) => Number(value).toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
const signedChange = (value, digits = 1) => value == null ? "—" : (Number(value) > 0 ? "+" : "") + num(value, digits) + " mg/dL";
const timeOnly = (value) => new Date(value).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
const localInputTime = date => {
  const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
};

const toInstant = (date, clock) => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(clock)) throw new Error("Zeit bitte im 24-Stunden-Format HH:MM eingeben");
  const parsed = new Date(date + "T" + clock + ":00");
  if (!Number.isFinite(parsed.getTime()) || localInputTime(parsed) !== date + "T" + clock) throw new Error("Dieser lokale Zeitpunkt existiert nicht");
  return parsed.toISOString();
};

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, cache: "no-store", ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Anfrage fehlgeschlagen");
  return data;
}

let toastTimeout;
function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("visible");
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => node.classList.remove("visible"), 4500);
}

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
}

function locationState() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get("view");
  const requestedDay = params.get("date");
  return {
    view: views.has(requestedView) ? requestedView : "diary",
    day: validDay(requestedDay) ? requestedDay : new Date().toLocaleDateString("sv-SE"),
  };
}

function syncTabLinks(url = new URL(window.location.href)) {
  url.searchParams.set("date", state.day);
  $$(".tab").forEach(link => {
    const linkURL = new URL(url);
    linkURL.searchParams.set("view", link.dataset.view);
    link.href = linkURL.href;
  });
}

function updateLocation(replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", state.view);
  url.searchParams.set("date", state.day);
  syncTabLinks(url);
  window.history[replace ? "replaceState" : "pushState"]({ view: state.view, day: state.day }, "", url);
}

function view(name, options = {}) {
  if (!views.has(name)) return;
  const changed = state.view !== name;
  state.view = name;
  $$(".tab").forEach(link => {
    const active = link.dataset.view === name;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $$(".view").forEach(section => section.hidden = section.id !== name + "-view");
  if (changed && options.updateHistory !== false) updateLocation();
  if (name === "analysis") loadSummary();
}

function changeDay(day, options = {}) {
  if (!validDay(day) || day === state.day) return;
  state.day = day;
  $("#selected-day").value = day;
  setDefaultMealTime();
  setDefaultSportTime();
  if (options.updateHistory !== false) updateLocation(Boolean(options.replaceHistory));
  if (options.load !== false) loadDay();
}

function setDefaultMealTime() {
  const current = new Date();
  const selected = state.day;
  const today = current.toLocaleDateString("sv-SE");
  $("#meal-time").value = selected === today ? localInputTime(current).slice(11, 16) : "12:00";
}

function setDefaultSportTime() {
  const current = new Date();
  const today = current.toLocaleDateString("sv-SE");
  $("#sport-time").value = state.day === today ? localInputTime(current).slice(11, 16) : "12:00";
}

function setSportDuration() {
  const kind = $("#sport-kind").value;
  $("#sport-duration").value = kind === "gym" ? "60" : "30";
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  $("#connection").textContent = data.connected ? "Nightscout konfiguriert" : "Nightscout einrichten";
  $("#connection").classList.toggle("connected", data.connected);
  $("#nightscout-location").textContent = data.connected ? "Verbindung über .env konfiguriert" : "NIGHTSCOUT_URL und NIGHTSCOUT_CREDENTIAL in .env eintragen";
  $("#settings-version").textContent = "Einstellungsversion " + data.version;
  const form = $("#settings-form");
  form.elements.window_minutes.value = data.settings.window_minutes;
  renderIsfRows(data.settings.isf_schedule, data.settings.nightscout_profile, data.profile_sync_at, data.profile_sync_error);
}

function renderIsfRows(rows, profile, syncedAt, syncError) {
  const list = $("#isf-rows");
  if (!rows.length) {
    list.innerHTML = '<p class="hint">Kein Korrekturfaktor-Profil aus Nightscout im lokalen Cache.</p>';
  } else {
    list.innerHTML = '<div class="isf-table"><div class="isf-table-head"><span>Ab</span><span>Korrekturfaktor</span></div>' +
      rows.map(row => {
        const start = Number(row.start_minute);
        const hh = String(Math.floor(start / 60)).padStart(2, "0");
        const mm = String(start % 60).padStart(2, "0");
        return '<div class="isf-readonly-row"><span>' + hh + ':' + mm + '</span><strong>' +
          escapeHTML(num(row.mgdl_per_unit, 3)) + ' <small>mg/dL je IE</small></strong></div>';
      }).join("") + '</div>';
  }
  const source = profile
    ? 'Profil „' + escapeHTML(profile.profile_name) + '“ · gültig ab ' +
      escapeHTML(new Date(profile.start_at).toLocaleString("de-DE")) +
      (profile.timezone ? ' · ' + escapeHTML(profile.timezone) : '')
    : 'Nightscout-Profil nicht verfügbar.';
  const synced = syncedAt ? ' · zuletzt synchronisiert ' + escapeHTML(new Date(syncedAt).toLocaleString("de-DE")) : '';
  $("#isf-sync-status").textContent = (syncError ? syncError + ' · ' : '') + source + synced;
}

function chartFor(meal, entries) {
  const start = new Date(meal.at).getTime();
  const end = meal.calculation?.end ? new Date(meal.calculation.end).getTime() : start + Number(state.settings.window_minutes) * 60000;
  const points = entries.filter(point => {
    const at = new Date(point.at).getTime();
    return at >= start - 30 * 60000 && at <= end + 30 * 60000;
  }).map(point => ({ t: new Date(point.at).getTime(), g: Number(point.sgv) }));
  if (points.length < 2) return '<p class="hint">Keine CGM-Kurve verfügbar.</p>';
  const min = Math.min(...points.map(point => point.g)) - 15;
  const max = Math.max(...points.map(point => point.g)) + 15;
  const x = t => 24 + (t - (start - 30 * 60000)) / (end - start + 60 * 60000) * 472;
  const y = g => 104 - (g - min) / (max - min) * 80;
  const line = points.map(point => x(point.t).toFixed(1) + "," + y(point.g).toFixed(1)).join(" ");
  const x0 = x(start).toFixed(1);
  const x3 = x(end).toFixed(1);
  return '<svg class="glucose-chart" viewBox="0 0 520 130" role="img" aria-label="Glukoseverlauf um die Mahlzeit">' +
    '<line x1="24" y1="104" x2="496" y2="104" class="axis"/>' +
    '<line x1="' + x0 + '" y1="14" x2="' + x0 + '" y2="104" class="marker"/>' +
    '<line x1="' + x3 + '" y1="14" x2="' + x3 + '" y2="104" class="marker"/>' +
    '<polyline points="' + line + '" class="curve"/>' +
    '<text x="' + x0 + '" y="122" text-anchor="middle">Mahlzeit</text>' +
    '<text x="' + x3 + '" y="122" text-anchor="middle">+' + escapeHTML(meal.calculation?.actual_window_minutes || state.settings.window_minutes) + ' min</text>' +
    '</svg>';
}

function calculationHTML(result) {
  if (!result || !result.start) return '<div class="factor unavailable"><span>Insulinfaktor</span><strong>—</strong><small>' + escapeHTML(result?.reason || "Noch keine Berechnung") + '</small></div>';
  const valid = result.factor_units_per_ke != null;
  const badge = '<div class="factor' + (valid ? '' : ' unavailable') + '"><span>Insulinfaktor</span><strong>' +
    (valid ? escapeHTML(num(result.factor_units_per_ke, 3)) + ' <small>IE/KE</small>' : '—') +
    '</strong><small>' + escapeHTML(num(result.carbs_total)) + ' g KH im Mahlzeitenfenster · ' +
    result.carb_events.length + ' KH-Ereignis' + (result.carb_events.length === 1 ? '' : 'se') + '</small></div>';
  const endMinutes = num(result.actual_window_minutes, 1);
  const extended = result.end_reason === "after_last_injection"
    ? '<p class="calc-note">Endpunkt wegen einer späteren Injektion verschoben. Jede schnell wirksame Dosis im Fenster erhält ' + escapeHTML(num(result.window_minutes, 0)) + ' Minuten Beobachtungszeit.</p>' : '';
  const shortened = result.end_reason === "before_next_meal"
    ? '<p class="calc-note">Endpunkt auf den letzten CGM-Wert vor der nächsten Hauptmahlzeit um ' +
      escapeHTML(timeOnly(result.next_main_meal_at)) + ' verkürzt.</p>' : '';
  const reason = result.reason
    ? '<p class="calc-note">Kein Faktor: ' + escapeHTML(result.reason) + '.</p>' : '';
  const glucose = '<div class="calc-step"><span>Glukose von ' + escapeHTML(timeOnly(result.start)) + ' bis ' + escapeHTML(timeOnly(result.end)) +
    ' (+' + escapeHTML(endMinutes) + ' min)</span><b>' + escapeHTML(num(result.g0, 3)) + ' → ' +
    escapeHTML(num(result.g3, 3)) + ' mg/dL · Δ ' + escapeHTML(num(result.delta_mgdl, 3)) + '</b></div>';
  const insulin = '<div class="calc-step"><span>Schnell wirksames Insulin im Fenster</span><b>' +
    escapeHTML(num(result.rapid_units_in_window, 2)) + ' IE vollständig berücksichtigt · ' +
    escapeHTML(num(result.insulin_effect_mgdl, 3)) + ' mg/dL rechnerischer Insulineffekt</b></div>';
  const carbs = '<div class="calc-step"><span>KH × Korrekturfaktor</span><b>' +
    (result.extra_carb_count ? 'Σ(KH × KF) = ' : escapeHTML(num(result.carbs_total)) + ' g × ' + escapeHTML(num(result.meal_isf)) + ' mg/dL/IE = ') +
    escapeHTML(num(result.carbs_times_isf_total, 3)) + '</b></div>';
  const formula = valid
    ? '<p class="formula">IE/KE = 10 × (' + escapeHTML(num(result.delta_mgdl, 3)) + ' + ' +
      escapeHTML(num(result.insulin_effect_mgdl, 3)) + ') ÷ ' +
      escapeHTML(num(result.carbs_times_isf_total, 3)) + ' = ' + escapeHTML(num(result.factor_units_per_ke, 3)) + '</p>'
    : '';
  const profileInfo = result.nightscout_profile
    ? ' · aktuelles Nightscout-Profil „' + escapeHTML(result.nightscout_profile.profile_name) + '“'
    : '';
  const windowNote = result.extra_carb_count
    ? '<p class="calc-note">Alle ' + result.carb_events.length + ' KH-Ereignisse im Fenster sind summiert. Das Modell setzt voraus, dass auch spätere KH bis zum Endpunkt vollständig wirken.</p>'
    : '';
  const doseNames = { meal: "Mahlzeit", "meal-dose": "Mahlzeitenbolus", correction: "Korrektur", "Snack Bolus": "Snack-Bolus", "Meal Bolus": "Mahlzeitenbolus", "Correction Bolus": "Korrektur", Bolus: "Bolus" };
  const doseRows = result.doses.map(dose => '<tr><td>' + escapeHTML(timeOnly(dose.at)) + '</td><td>' +
    escapeHTML(dose.event_type === "Snack Bolus" ? "Snack-Bolus" : doseNames[dose.role] || dose.role) + '</td><td>' + escapeHTML(num(dose.units)) + ' IE</td><td>' +
    escapeHTML(num(dose.isf)) + '</td><td>' +
    escapeHTML(num(dose.effect_mgdl, 3)) + '</td></tr>').join("");
  const carbRows = result.carb_events.map(item => '<tr><td>' + escapeHTML(timeOnly(item.at)) + '</td><td>' +
    escapeHTML(item.kind === "meal" ? "Mahlzeit" : item.kind === "snack" ? "Snack" : "Weitere KH") + '</td><td>' +
    escapeHTML(num(item.carbs)) + ' g</td><td>' + escapeHTML(num(item.isf)) + '</td><td>' +
    escapeHTML(num(item.carbs_times_isf, 3)) + '</td></tr>').join("");
  return '<div class="calc-block">' + badge + '<div class="calc-steps">' + glucose + insulin + carbs + '</div>' +
    formula + extended + shortened + reason + windowNote +
    '<details class="math"><summary>Alle Dosen, KH und Rohwerte anzeigen</summary>' +
    '<h4>KH-Ereignisse</h4><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Art</th><th>KH</th><th>KF mg/dL/IE</th><th>KH × KF</th></tr></thead><tbody>' + carbRows + '</tbody></table></div>' +
    '<h4>Insulineffekt</h4><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Art</th><th>Dosis</th><th>KF mg/dL/IE</th><th>mg/dL</th></tr></thead><tbody>' + doseRows + '</tbody></table></div>' +
    '<p class="hint">Rohwerte: Δ ' + escapeHTML(result.delta_mgdl) + ' mg/dL · rechnerischer Insulineffekt ' +
      escapeHTML(result.insulin_effect_mgdl) + ' mg/dL · Summe ' + escapeHTML(result.denominator_mgdl) +
      ' mg/dL' + (valid ? ' · IE/KE ' + escapeHTML(result.factor_units_per_ke) + ' · Nightscout-I:C ' +
      escapeHTML(result.ratio_g_per_unit) + ' g/IE' : '') + '.</p>' +
    '<p class="hint">CGM-Stützpunkte: ' + escapeHTML(result.g0_sources.join(" / ")) + ' und ' +
      escapeHTML(result.g3_sources.join(" / ")) + '. Modell ' + escapeHTML(result.algorithm) +
      ' · Einstellungen ' + escapeHTML(result.settings_version) + profileInfo + '.</p>' +
    '<p class="hint">Annahmen: ' + result.assumptions.map(escapeHTML).join(" · ") + '</p></details></div>';
}

function mealHTML(meal, entries) {
  const eventName = event => event.eventType === "Snack Bolus" ? "Zwischenmahlzeit"
    : event.role === "meal" || event.eventType === "Meal Bolus" && !event.role ? "Mahlzeit"
    : event.role === "meal-dose" ? "Mahlzeitenbolus"
    : event.role === "correction" || event.eventType === "Correction Bolus" ? "Korrektur"
    : Number(event.carbs) ? "Weitere KH" : "Weiterer Bolus";
  const events = meal.events.map(event => '<li><span>' + escapeHTML(timeOnly(event.at)) + '</span><b>' + escapeHTML(eventName(event)) + '</b><span>' + (Number(event.carbs) ? escapeHTML(num(event.carbs)) + ' g KH ' : '') + (Number(event.insulin) ? escapeHTML(num(event.insulin)) + ' E' : '') + '</span></li>').join("");
  const totalCarbs = meal.calculation?.blockers?.length ? null : meal.calculation?.carbs_total;
  const carbLabel = totalCarbs != null ? num(totalCarbs) + " g KH im Fenster" : num(meal.carbs) + " g KH";
  return '<article class="meal-card">' +
    '<div class="meal-top"><div><span class="meal-type">' + escapeHTML(names[meal.meal]) + '</span><h3>' + escapeHTML(timeOnly(meal.at)) + ' <small>· ' + escapeHTML(carbLabel) + '</small></h3></div></div>' +
    calculationHTML(meal.calculation) +
    (meal.imported ? '<p class="hint">Aus Nightscout übernommen · Mahlzeitenart nach Uhrzeit zugeordnet</p>' : '') +
    (meal.notes ? '<p class="meal-note">' + escapeHTML(meal.notes) + '</p>' : '') +
    chartFor(meal, entries) +
    '<ul class="events">' + events + '</ul>' +
    (meal.imported ? '' : '<button type="button" class="add-dose" data-episode="' + escapeHTML(meal.id) + '">+ Injektion ergänzen</button>') +
    '<form class="dose-form" data-episode="' + escapeHTML(meal.id) + '" data-meal="' + escapeHTML(meal.meal) + '" hidden>' +
    '<label>Art <select name="role"><option value="meal-dose">Mahlzeitenbolus</option><option value="correction">Korrektur</option></select></label>' +
    '<label>Datum <input type="date" name="date" required></label>' +
    '<label>Uhrzeit (HH:MM, 24 h)<input type="text" name="at" inputmode="numeric" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" maxlength="5" placeholder="14:30" required></label>' +
    '<label>Insulin <span>E</span><input type="number" name="insulin" min="0.01" max="100" step="0.01" required></label>' +
    '<button type="submit" class="primary">In Nightscout speichern</button></form>' +
    '</article>';
}

function renderNight(night) {
  const card = $("#night-card");
  card.hidden = false;
  const startTime = night.start ? timeOnly(night.start) : null;
  const endTime = night.end ? timeOnly(night.end) : null;
  $("#night-window").textContent = startTime && endTime
    ? startTime + " bis " + endTime + " Uhr"
    : "Drei Stunden nach letzter Injektion bis Beginn Frühstück";
  const metric = (label, value, detail) =>
    '<div class="night-metric"><span>' + label + '</span><strong>' +
    (value == null ? '—' : escapeHTML(num(value, 1)) + ' <small>mg/dL</small>') +
    '<small>' + escapeHTML(detail) + '</small></div>';
  $("#night-values").innerHTML =
    metric("Startwert", night.start_glucose, startTime ? "3 h nach letzter Injektion · " + startTime : "3 h nach letzter Injektion") +
    metric("Endwert", night.end_glucose, endTime ? "Beginn Frühstück · " + endTime : "Beginn Frühstück") +
    '<div class="night-metric"><span>Veränderung</span><strong>' + escapeHTML(signedChange(night.delta_mgdl)) + '</strong><small>Endwert − Startwert</small></div>' +
    metric("Minimalwert", night.minimum_glucose, night.minimum_at ? "um " + timeOnly(night.minimum_at) : "im Nachtfenster");
  const reason = $("#night-reason");
  reason.hidden = !night.reason;
  reason.textContent = night.reason || "";
}

function renderDay(data) {
  state.dayData = data;
  $("#record-count").textContent = data.meals.length + (data.meals.length === 1 ? " Eintrag" : " Einträge");
  renderNight(data.night || { reason: "Nachtanalyse nicht verfügbar." });
  const banner = $("#banner");
  if (!data.connected) {
    banner.hidden = false;
    banner.textContent = "Nightscout ist noch nicht verbunden. Trage URL und Zugang in .env ein und starte den Server neu.";
  } else if (data.sync_error || data.profile_sync_error) {
    banner.hidden = false;
    banner.textContent = "Nightscout konnte nicht vollständig aktualisiert werden: " +
      [data.sync_error, data.profile_sync_error].filter(Boolean).join(" · ") + ". Lokale Daten werden angezeigt.";
  } else if (!state.settings.isf_schedule.length) {
    banner.hidden = false;
    banner.textContent = "Für die Analyse fehlt ein Korrekturfaktor-Profil aus Nightscout. Synchronisiere es in den Einstellungen.";
  } else {
    banner.hidden = true;
  }
  $("#meals").innerHTML = data.meals.length
    ? data.meals.map(meal => mealHTML(meal, data.entries)).join("")
    : '<div class="empty"><strong>Keine erfassten Mahlzeiten</strong><p>Trage links eine Mahlzeit ein oder wähle einen anderen Tag.</p></div>';
  const sportEvents = $("#sport-events");
  sportEvents.hidden = !data.exercises?.length;
  if (!sportEvents.hidden) {
    const rows = data.exercises.map(item => {
      const start = new Date(item.at);
      const end = new Date(start.getTime() + Number(item.duration_minutes || 0) * 60000);
      const dayOffset = end.toLocaleDateString("sv-SE") !== start.toLocaleDateString("sv-SE");
      const endLabel = timeOnly(end.toISOString()) + (dayOffset ? " (+1 Tag)" : "");
      const duration = Number(item.duration_minutes) > 0 ? num(item.duration_minutes, 0) + " min" : "Dauer unbekannt";
      return '<li><time>' + escapeHTML(timeOnly(item.at)) + '</time><strong>' + escapeHTML(item.label) + '</strong><span>' +
        escapeHTML(duration + (Number(item.duration_minutes) > 0 ? " · bis " + endLabel : "")) +
        (item.notes ? '<small>' + escapeHTML(item.notes) + '</small>' : '') + '</span></li>';
    }).join("");
    sportEvents.innerHTML = '<div class="section-head"><h2>Sport an diesem Tag</h2></div><ul>' + rows + '</ul>';
  }
  const other = $("#other-events");
  other.hidden = !data.other_events?.length;
  if (!other.hidden) {
    const rows = data.other_events.map(event => {
      const basal = Number(event.basal_units);
      const carbs = Number(event.carbs);
      const rapid = Number(event.insulin);
      const kind = basal ? "Basalinsulin" : event.eventType === "Snack Bolus" ? "Zwischenmahlzeit" : event.eventType === "Correction Bolus" ? "Korrektur" : "Weiterer Eintrag";
      const detail = [carbs ? num(carbs) + " g KH" : "", rapid ? num(rapid) + " IE schnell wirksam" : "", basal ? num(basal) + " IE lang wirksam" : ""].filter(Boolean).join(" · ");
      return '<li><time>' + escapeHTML(timeOnly(event.at)) + '</time><strong>' + escapeHTML(kind) + '</strong><span>' + escapeHTML(detail || event.notes) + '</span></li>';
    }).join("");
    other.innerHTML = '<h3>Weitere Einträge</h3><ul>' + rows + '</ul>';
  }
  $$(".add-dose").forEach(button => button.addEventListener("click", () => {
    const form = $('.dose-form[data-episode="' + button.dataset.episode + '"]');
    form.hidden = !form.hidden;
    if (!form.hidden) {
      const current = localInputTime(new Date());
      form.elements.date.value = current.slice(0, 10);
      form.elements.at.value = current.slice(11, 16);
      form.elements.insulin.focus();
    }
  }));
  $$(".dose-form").forEach(form => form.addEventListener("submit", saveDose));
}

async function loadDay() {
  $("#selected-day").value = state.day;
  $("#meals").innerHTML = '<div class="empty">Nightscout-Daten werden geladen …</div>';
  $("#night-card").hidden = true;
  try {
    renderDay(await api("/api/day?date=" + encodeURIComponent(state.day)));
  } catch (error) {
    $("#meals").innerHTML = '<div class="empty">Daten konnten nicht geladen werden.</div>';
    $("#night-card").hidden = true;
    toast(error.message, true);
  }
}

async function saveMeal(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    meal: form.elements.meal.value,
    at: toInstant(state.day, form.elements.at.value),
    carbs: form.elements.carbs.value,
    insulin: form.elements.insulin.value,
    notes: form.elements.notes.value,
    event_id: state.pendingMealId || crypto.randomUUID(),
  };
  state.pendingMealId = payload.event_id;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    const result = await api("/api/episode", { method: "POST", body: JSON.stringify(payload) });
    state.pendingMealId = null;
    state.day = result.day;
    updateLocation(true);
    form.elements.carbs.value = "";
    form.elements.insulin.value = "";
    form.elements.notes.value = "";
    toast("Mahlzeit in Nightscout gespeichert");
    await loadDay();
    form.elements.carbs.focus();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveSport(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    kind: form.elements.kind.value,
    at: toInstant(state.day, form.elements.at.value),
    duration_minutes: form.elements.duration_minutes.value,
    notes: form.elements.notes.value,
    event_id: state.pendingSportId || crypto.randomUUID(),
  };
  state.pendingSportId = payload.event_id;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    const result = await api("/api/exercise", { method: "POST", body: JSON.stringify(payload) });
    state.pendingSportId = null;
    state.day = result.day;
    updateLocation(true);
    form.elements.notes.value = "";
    toast("Sporteintrag in Nightscout gespeichert");
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveDose(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    episode_id: form.dataset.episode, meal: form.dataset.meal,
    role: form.elements.role.value, insulin: form.elements.insulin.value,
    at: toInstant(form.elements.date.value, form.elements.at.value),
    event_id: form.dataset.pendingId || crypto.randomUUID(),
  };
  form.dataset.pendingId = payload.event_id;
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    await api("/api/dose", { method: "POST", body: JSON.stringify(payload) });
    delete form.dataset.pendingId;
    toast("Injektion in Nightscout gespeichert");
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    window_minutes: Number(form.elements.window_minutes.value),
    effective_at: form.elements.effective_date.value ? toInstant(form.elements.effective_date.value, form.elements.effective_time.value || "00:00") : null,
  };
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    toast("Einstellungen gespeichert");
    await loadSettings();
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderNightSummary(night) {
  const metrics = [
    ["Startwert", "median_start_glucose"],
    ["Endwert", "median_end_glucose"],
    ["Veränderung", "median_delta_mgdl"],
    ["Minimalwert", "median_minimum_glucose"],
  ];
  $("#night-summary-count").textContent = night.count + " von " +
    (night.count + night.unavailable_count) + " Nächten mit vollständigen Werten";
  $("#night-summary-cards").innerHTML = metrics.map(([label, key]) =>
    '<div class="summary-card"><span>' + label + ' · Median</span><strong>' +
    (night[key] == null ? '—' : label === "Veränderung" ? escapeHTML(signedChange(night[key])) : escapeHTML(num(night[key], 1)) + ' <small>mg/dL</small>') +
    '</strong><p>Aus ' + night.count + ' ausgewerteten Nächten</p></div>'
  ).join("");
  const cell = (value, at) => value == null ? '—' : escapeHTML(num(value, 1)) + ' mg/dL' +
    (at ? '<small class="night-error">' + escapeHTML(timeOnly(at)) + ' Uhr</small>' : '');
  const rows = night.nights.map(item => {
    const label = new Date(item.day + "T12:00:00").toLocaleDateString("de-DE", {
      weekday: "short", day: "numeric", month: "short",
    });
    const reason = item.reason
      ? '<small class="night-error">' + escapeHTML(item.reason) + '</small>' : '';
    return '<tr><td>' + escapeHTML(label) + reason + '</td><td>' +
      cell(item.start_glucose, item.start) + '</td><td>' + cell(item.end_glucose, item.end) +
      '</td><td>' + escapeHTML(signedChange(item.delta_mgdl)) + '</td><td>' + cell(item.minimum_glucose, item.minimum_at) + '</td></tr>';
  }).join("");
  $("#night-records").innerHTML = rows
    ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Nacht</th><th>Startwert</th><th>Endwert</th><th>Veränderung</th><th>Minimalwert</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
    : '<div class="empty">Für diesen Zeitraum liegen keine Nächte vor.</div>';
}

async function loadSummary() {
  $("#summary").innerHTML = '<div class="empty">Analyse wird geladen …</div>';
  $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht wird geladen …</div>';
  $("#night-records").innerHTML = "";
  $("#calculations").innerHTML = "";
  try {
    const days = $("#summary-days").value;
    const data = await api("/api/summary?days=" + days);
    renderNightSummary(data.night || { count: 0, unavailable_count: Number(days), nights: [] });
    if (data.profile_sync_error) {
      $("#sync-progress").textContent = "Korrekturfaktor-Profil nicht aktualisiert: " + data.profile_sync_error;
    }
    const breakfastSubcards = () => '<div class="breakfast-subcards">' + ["work", "free"].map(dayType => {
      const subset = data.groups.breakfast.by_day_type[dayType];
      return '<div class="breakfast-subcard"><span>' + (dayType === "work" ? "Arbeit" : "Frei") + '</span>' +
        '<strong>' + (subset.count ? escapeHTML(num(subset.mean_units_per_ke, 3)) : '—') +
        ' <small>IE/KE</small></strong>' +
        '<small>Median: ' + (subset.count ? escapeHTML(num(subset.median_units_per_ke, 3)) + ' IE/KE' : '—') +
        ' · ' + subset.count + ' Werte</small></div>';
    }).join("") + '</div><p class="breakfast-subnote">Mo–Fr: Arbeit · Sa–So: Frei. Einzelne Tage kannst du unten ändern.</p>';
    $("#summary").innerHTML = ["breakfast", "lunch", "dinner"].map(kind => {
      const row = data.groups[kind];
      const hasValues = row.count > 0;
      const value = field => hasValues ? escapeHTML(num(row[field], 3)) + ' <small>IE/KE</small>' : "—";
      const typical = row.q1_units_per_ke != null
        ? '<div class="summary-metric"><span>Mittlere 50 % der berücksichtigten Werte</span><strong>' +
          escapeHTML(num(row.q1_units_per_ke, 3)) + '–' + escapeHTML(num(row.q3_units_per_ke, 3)) +
          ' <small>IE/KE</small></strong></div>'
        : '<p class="summary-note">Für den Bereich der mittleren 50 % sind mindestens 5 berücksichtigte Mahlzeiten nötig.</p>';
      const range = hasValues
        ? '<p class="summary-range">Berücksichtigter Bereich: ' + escapeHTML(num(row.min_units_per_ke, 3)) +
          '–' + escapeHTML(num(row.max_units_per_ke, 3)) + ' IE/KE</p>'
        : '';
      return '<div class="summary-card"><span class="meal-type">' + escapeHTML(names[kind]) + '</span>' +
        '<div class="summary-primary"><span>Durchschnitt</span><strong>' + value("mean_units_per_ke") + '</strong></div>' +
        '<div class="summary-metric"><span>Median</span><strong>' + value("median_units_per_ke") + '</strong></div>' +
        (hasValues ? typical : '') + range +
        '<p>' + row.count + ' von ' + row.valid_count + ' berechenbaren Mahlzeiten berücksichtigt · ' +
        row.excluded_count + ' ausgeklammert · ' + row.blocked_count + ' nicht berechenbar</p>' +
        (kind === "breakfast" ? breakfastSubcards() : '') + '</div>';
    }).join("");
    const calculations = ["breakfast", "lunch", "dinner"]
      .flatMap(kind => data.groups[kind].calculations.map(item => ({ ...item, kind })))
      .sort((a, b) => b.at.localeCompare(a.at));
    $("#calculation-count").textContent = calculations.length + (calculations.length === 1 ? " Mahlzeit" : " Mahlzeiten");
    $("#calculations").innerHTML = calculations.length
      ? '<div class="table-wrap"><table class="analysis-table"><thead><tr><th>Uhrzeit</th><th>Mahlzeit</th><th>Insulinfaktor</th><th>KH</th><th>Insulin</th><th>Glukoseänderung</th><th>Endpunkt</th><th>Ausklammern</th><th></th></tr></thead><tbody>' +
        (() => {
          let previousDay = "";
          return calculations.map(item => {
            const newDay = item.day !== previousDay;
            previousDay = item.day;
            const dayHeading = newDay
              ? '<tr class="analysis-day-row"><th colspan="9" scope="colgroup">' +
                escapeHTML(new Date(item.day + "T12:00:00").toLocaleDateString("de-DE", {
                  weekday: "long", day: "numeric", month: "long", year: "numeric",
                })) + ' <label class="day-type-label">Tag: <select class="day-type" data-day="' +
                escapeHTML(item.day) + '"><option value="work"' + (item.day_type === "work" ? " selected" : "") +
                '>Arbeit</option><option value="free"' + (item.day_type === "free" ? " selected" : "") +
                '>Frei</option></select></label></th></tr>'
              : '';
            const factor = item.units_per_ke != null
              ? escapeHTML(num(item.units_per_ke, 3)) + ' IE/KE' +
                (item.strong_outlier ? ' <span class="outlier-badge">Stark abweichend</span>' : '')
              : '<span class="summary-unavailable">Nicht berechenbar: ' + escapeHTML(item.reason || "Kein Faktor") + '</span>';
            const excluded = item.units_per_ke != null && item.excluded;
            return dayHeading + '<tr class="' + (excluded ? "analysis-excluded " : "") +
              (item.strong_outlier ? "analysis-outlier" : "") + '">' +
              '<td>' + escapeHTML(timeOnly(item.at)) + '</td>' +
            '<td>' + escapeHTML(names[item.kind]) + '</td><td><b>' + factor + '</b></td>' +
            '<td>' + (item.carbs_total == null ? "—" : escapeHTML(num(item.carbs_total)) + ' g') + '</td>' +
            '<td>' + (item.rapid_units_in_window == null ? "—" : escapeHTML(num(item.rapid_units_in_window)) + ' IE') + '</td>' +
            '<td>' + escapeHTML(signedChange(item.delta_mgdl)) + '</td>' +
            '<td>' + (item.end ? escapeHTML(timeOnly(item.end)) : "—") + '</td>' +
            '<td>' + (item.units_per_ke == null ? "—" : '<label class="exclude-label"><input type="checkbox" class="exclude-meal" data-meal-id="' +
              escapeHTML(item.id) + '" data-day="' + escapeHTML(item.day) + '"' + (excluded ? " checked" : "") +
              '> <span>Ausklammern</span></label>') + '</td>' +
              '<td><button type="button" class="quiet open-meal-day" data-day="' + escapeHTML(item.day) + '">Tagebuch</button></td></tr>';
          }).join("");
        })() + '</tbody></table></div>'
      : '<div class="empty">Für diesen Zeitraum sind keine Mahlzeiten im lokalen Datenbestand.</div>';
  } catch (error) {
    $("#summary").innerHTML = '<div class="empty">Analyse nicht verfügbar.</div>';
    $("#night-summary-cards").innerHTML = '<div class="empty">Nachtübersicht nicht verfügbar.</div>';
    $("#night-records").innerHTML = "";
    $("#calculations").innerHTML = "";
      toast(error.message, true);
  }
}

async function saveAnalysisExclusion(event) {
  const input = event.target;
  if (!input.matches(".exclude-meal")) return;
  input.disabled = true;
  try {
    await api("/api/analysis/exclusion", {
      method: "POST",
      body: JSON.stringify({ meal_id: input.dataset.mealId, day: input.dataset.day, excluded: input.checked }),
    });
    await loadSummary();
  } catch (error) {
    input.checked = !input.checked;
    input.disabled = false;
    toast(error.message, true);
  }
}

async function saveDayType(event) {
  const input = event.target;
  if (!input.matches(".day-type")) return;
  input.disabled = true;
  try {
    await api("/api/analysis/day-type", {
      method: "POST",
      body: JSON.stringify({ day: input.dataset.day, day_type: input.value }),
    });
    await loadSummary();
  } catch (error) {
    input.disabled = false;
    toast(error.message, true);
    await loadSummary();
  }
}

async function syncRange() {
  const button = $("#sync-range");
  button.disabled = true;
  $("#sync-progress").textContent = "Synchronisiere …";
  try {
    const data = await api("/api/sync", { method: "POST", body: JSON.stringify({ days: Number($("#summary-days").value) }) });
    $("#sync-progress").textContent = data.synced + " Tage synchronisiert" +
      (data.errors.length ? " · " + data.errors.length + " Fehler" : "") +
      (data.profile_sync_error ? " · Korrekturfaktor: " + data.profile_sync_error : "");
    await loadSummary();
  } catch (error) {
    $("#sync-progress").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const initialLocation = locationState();
  state.day = initialLocation.day;
  state.view = initialLocation.view;
  $("#selected-day").value = state.day;
  setDefaultMealTime();
  setDefaultSportTime();
  setSportDuration();
  view(state.view, { updateHistory: false });
  updateLocation(true);
  $$(".tab").forEach(link => link.addEventListener("click", event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    view(link.dataset.view);
  }));
  window.addEventListener("popstate", () => {
    const nextLocation = locationState();
    const dayChanged = state.day !== nextLocation.day;
    state.day = nextLocation.day;
    syncTabLinks();
    $("#selected-day").value = state.day;
    setDefaultMealTime();
    setDefaultSportTime();
    view(nextLocation.view, { updateHistory: false });
    if (dayChanged || nextLocation.view === "diary") loadDay();
  });
  $("#prev-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() - 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#next-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() + 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#selected-day").addEventListener("change", event => changeDay(event.target.value));
  $("#refresh-day").addEventListener("click", loadDay);
  $("#meal-form").addEventListener("submit", saveMeal);
  $("#sport-form").addEventListener("submit", saveSport);
  $("#sport-kind").addEventListener("change", setSportDuration);
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#sync-profile").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    $("#isf-sync-status").textContent = "Nightscout-Profil wird synchronisiert …";
    try {
      const result = await api("/api/profile/sync", { method: "POST", body: JSON.stringify({}) });
      await loadSettings();
      await loadDay();
      toast(result.message);
    } catch (error) {
      $("#isf-sync-status").textContent = error.message;
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#summary-days").addEventListener("change", loadSummary);
  $("#sync-range").addEventListener("click", syncRange);
  $("#calculations").addEventListener("change", saveAnalysisExclusion);
  $("#calculations").addEventListener("change", saveDayType);
  $("#calculations").addEventListener("click", event => {
    const button = event.target.closest(".open-meal-day");
    if (!button) return;
    state.day = button.dataset.day;
    setDefaultMealTime();
    view("diary");
    loadDay();
  });
  $("#test-connection").addEventListener("click", async () => {
    $("#test-result").textContent = "Prüfe …";
    try { const result = await api("/api/test-connection"); $("#test-result").textContent = result.message; }
    catch (error) { $("#test-result").textContent = error.message; }
  });
  document.addEventListener("keydown", event => {
    if (event.altKey && ["f", "m", "a"].includes(event.key.toLowerCase())) {
      event.preventDefault();
      view("diary");
      $("#meal-kind").value = ({ f: "breakfast", m: "lunch", a: "dinner" })[event.key.toLowerCase()];
      $("#meal-time").focus();
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("#diary-view").hidden) {
      event.preventDefault();
      $("#meal-form").requestSubmit();
    }
  });
  try {
    await loadSettings();
    await loadDay();
  } catch (error) {
    toast(error.message, true);
  }
});
