// The classic page logic, moving screen by screen into Preact components in web/ui/ (AGENTS.md).
// Shared helpers and the API call live in web/ui/lib.js.
import { html, render } from "./ui/html.js";
import { entryCard, quickAdd } from "./ui/main.js";
import { AnalysisView } from "./ui/analysis.js";
import { DayReview, entryLabel } from "./ui/day.js";
import { Overview, ActiveCard } from "./ui/today.js";
import { EntryForm, deleteBasal } from "./ui/entry.js";
import { glucosePoints } from "./ui/charts.js";
import { SettingsView, FORM_PAGES, zoneText, deviceZone, deviceZoneDiffers } from "./ui/settings.js";
import { sportNames, api, post, appUpdateWaiting, toast, num, timeOnly, toInstant, trendSymbol, UNIT } from "./ui/lib.js";
import { t, locale, translateDocument } from "./ui/i18n.js";

// The static texts of index.html follow the language before anything renders.
translateDocument();

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = { day: new Date().toLocaleDateString("sv-SE"), view: "diary", analysisTab: "werte", dayChosenAt: 0, settings: null, dayData: null, pendingMealId: null, pendingSportId: null, entryTimeTouched: false };
const views = new Set(["diary", "analysis", "settings"]);

const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
// Nothing typed and no dialog open: a reload loses nothing (the URL keeps view and tab).
function entryFormIdle() {
  return (entry.idle?.() ?? true) && !$$("dialog").some(dialog => dialog.open);
}

// Called when the page comes back to the foreground: a waiting update reloads at once when
// nothing would be lost, otherwise the toast offers it.
function reloadForUpdate() {
  if (!appUpdateWaiting() || document.hidden) return false;
  if (entryFormIdle()) {
    window.location.reload();
    return true;
  }
  if (!state.updateOffered) {
    state.updateOffered = true;
    toast(t("app.updateAvailable"), false, { label: t("app.reload"), run: () => window.location.reload() });
  }
  return false;
}

const glucoseStatuses = new Set(["in_range", "outside", "low", "high", "stale", "no_target", "unavailable"]);
let glucoseRefresh = null;

function loadCurrentGlucose() {
  if (glucoseRefresh) return glucoseRefresh;
  glucoseRefresh = (async () => {
    const widget = $("#current-glucose");
    try {
      const data = await api("/api/glucose/current");
      const status = glucoseStatuses.has(data.status) ? data.status : "unavailable";
      widget.dataset.status = status;
      $("#glucose-value").textContent = data.glucose_mgdl == null
        ? "—" : num(data.glucose_mgdl, 1) + " mg/dL";
      const delta = data.trend_delta_mgdl;
      $("#glucose-trend").textContent = data.fresh && delta != null
        ? trendSymbol(data.trend_direction) + " " + (Number(delta) > 0 ? "+" : "") + num(delta, 0) +
          " mg/dL / " + num(data.trend_minutes, 0) + " min"
        : data.status === "stale" ? t("glucose.trendStale") : t("glucose.trendUnavailable");
      const meta = status === "stale"
        ? t("glucose.stale") + (data.glucose_at ? " · " + t("common.clock", { time: timeOnly(data.glucose_at) }) : "")
        : data.glucose_mgdl == null ? t("glucose.noValue") : "";
      $("#glucose-meta").textContent = meta;
      $("#glucose-meta").hidden = !meta;
      if (data.glucose_at) catchUpDay(new Date(data.glucose_at).getTime());
    } catch (error) {
      widget.dataset.status = "unavailable";
      $("#glucose-value").textContent = "—";
      $("#glucose-trend").textContent = t("glucose.trendUnavailable");
      $("#glucose-meta").textContent = t("glucose.cgmUnavailable");
      $("#glucose-meta").hidden = false;
    }
    publishGlucose();
  })().finally(() => { glucoseRefresh = null; });
  return glucoseRefresh;
}

// A dose decision always needs the glucose in sight (user feedback 2026-11-18): on phones the top bar is
// one line (logo, value) and full-screen views open below it; the meal chat on wider screens is a
// modal over the dimmed page and shows a copy (web/ui/glucose.js).
function publishGlucose() {
  const stale = $("#glucose-meta").hidden ? "" : $("#glucose-meta").textContent;
  const glucose = { value: $("#glucose-value").textContent, status: $("#current-glucose").dataset.status,
    trend: stale || $("#glucose-trend").textContent };
  window.currentGlucose = glucose;
  document.dispatchEvent(new CustomEvent("glucose:update", { detail: glucose }));
}

// Full-screen views start below the top bar: its height as --topbar-h.
function trackTopbarHeight() {
  const bar = $(".topbar");
  const set = () => document.documentElement.style.setProperty("--topbar-h", bar.getBoundingClientRect().height + "px");
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
}
document.addEventListener("DOMContentLoaded", trackTopbarHeight);

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
}

// The URL carries only the view, never a day: an old link or a reloaded tab must not open an old day
// by accident (user request 2026-10-16). A chosen day lives in this page only; a reload, a new visit or
// DAY_HOLD_MS without touching the page brings back today.
const DAY_HOLD_MS = 30 * 60000;

function locationState() {
  const params = new URLSearchParams(window.location.search);
  const requestedView = params.get("view");
  return {
    view: views.has(requestedView) ? requestedView : "diary",
    analysisTab: ["automatik", "gerichte"].includes(params.get("tab")) ? params.get("tab") : "werte",
  };
}

function updateLocation(replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set("view", state.view);
  url.searchParams.delete("date");
  if (state.view === "analysis" && state.analysisTab !== "werte") url.searchParams.set("tab", state.analysisTab);
  else url.searchParams.delete("tab");
  window.history[replace ? "replaceState" : "pushState"]({ view: state.view }, "", url);
}

function markDayActivity() {
  state.dayChosenAt = Date.now();
}

// Back to today once the page was left untouched for DAY_HOLD_MS while showing another day, which also
// covers a page left open past midnight. Never while someone is working on the page.
function returnToToday() {
  const today = todayDay();
  if (state.day === today || Date.now() - state.dayChosenAt < DAY_HOLD_MS) return;
  changeDay(today);
}

function view(name, options = {}) {
  if (!views.has(name)) return;
  const changed = state.view !== name;
  state.view = name;
  $$(".view").forEach(section => section.hidden = section.id !== name + "-view");
  if (changed && options.updateHistory !== false) updateLocation();
  if (name === "analysis") refreshAnalysis();
  if (changed) entry.release?.();
  if (name === "diary") entry.sync?.();
  if (name === "settings" && changed) showSettingsPage(null);
  if (changed) closeEntryDrawer();
  updateNav();
}

// Analysis (web/ui/analysis.js): mounted the first time it is opened; every opening, and every change
// elsewhere that it shows, fetches the summary again.
let analysisReload = 0;

function renderAnalysis() {
  if (!analysisReload) return;
  render(html`<${AnalysisView} tab=${state.analysisTab} onTab=${analysisTab} reloadKey=${analysisReload}
    getSettings=${() => state.settings} onSettings=${settings => { state.settings = settings; }}
    onSettingsChanged=${loadSettings} onOpenMeal=${openMealDay} />`, $("#analysis-view"));
}

function refreshAnalysis() {
  if (state.view !== "analysis") return;
  analysisReload += 1;
  renderAnalysis();
}

// Analysis sub-pages: values of the chosen period, how the Automatik would have done, the dishes.
function analysisTab(name, options = {}) {
  const changed = state.analysisTab !== name;
  state.analysisTab = name;
  renderAnalysis();
  if (changed && options.updateHistory !== false) updateLocation(true);
}

// A meal is corrected on its day: the analysis row opens "Tag durchgehen" at that meal.
async function openMealDay(day, mealId) {
  state.day = day;
  state.dayChosenAt = Date.now();
  state.modeDay = state.day;
  state.diaryMode = "review";
  $("#selected-day").value = state.day;
  view("diary");
  await loadDay();
  $("#meal-" + CSS.escape(mealId))?.scrollIntoView({ block: "start" });
}

function changeDay(day, options = {}) {
  if (!validDay(day) || day === state.day) return;
  resetEntryForm();
  state.day = day;
  state.dayChosenAt = Date.now();
  $("#selected-day").value = day;
  renderEntry();
  if (options.load !== false) loadDay();
}

// An entry is always "now" unless the time was changed on purpose. That manual time ends with the
// next saved entry, a view or day change, or after 10 minutes without touching the form.
// The zone the app calculates with right now (home or the latest travel switch, user request 2026-11-07).
function appZone() {
  return state.travel?.current || state.settings?.timezone || "Europe/Berlin";
}

// The diary's timezone banner: shown while the device's clock differs from the app's.
function renderTimezone() {
  const travel = state.travel;
  const banner = $("#timezone-banner");
  const differs = deviceZoneDiffers(travel);
  banner.hidden = !differs;
  if (!differs) return;
  const device = deviceZone();
  banner.innerHTML = escapeHTML(t("app.tzBanner", { device: zoneText(device), app: zoneText(travel.current) })) + " " +
    '<button type="button" class="banner-action" data-timezone-switch="' + escapeHTML(device) + '">' +
    escapeHTML(t("app.tzSwitch", { zone: device })) + "</button>";
}

// Switches the zone the app calculates with (settings or the banner); true when it worked.
async function switchTimezone(payload) {
  try {
    state.travel = await api("/api/timezone", { method: "POST", body: JSON.stringify(payload) });
    if (state.settingsData) state.settingsData = { ...state.settingsData, travel: state.travel };
    renderTimezone();
    renderSettings();
    toast(payload.undo ? t("app.tzUndone") : t("app.tzNow", { zone: state.travel.current }));
    if (todayDay() !== state.day) changeDay(todayDay()); else await loadDay();
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

// Settings (web/ui/settings.js) render from /api/settings; the rest of the page reads state.settings
// (dose defaults, sport effects) and state.travel (the zone the app calculates with).
let settingsLoads = 0;

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings;
  state.settingsData = data;
  state.travel = data.travel || null;
  settingsLoads += 1;
  $("#connection").textContent = data.connected ? "" : t("app.setupNightscout");
  $("#connection").hidden = data.connected;
  renderTimezone();
  renderSettings();
  await loadAgent();
}

function renderSettings() {
  if (!state.settingsData) return;
  render(html`<${SettingsView} data=${state.settingsData} loadCount=${settingsLoads} page=${state.settingsPage || null}
    onPage=${showSettingsPage} today=${todayDay()} agentInfo=${agent.info}
    onAgentInfo=${info => { agent.info = info; renderSettings(); renderEntry(); }}
    onReload=${async ({ day }) => { await loadSettings(); if (day) await loadDay(); }}
    onSwitchZone=${switchTimezone}
    onVacation=${async vacation => { state.settingsData = { ...state.settingsData, vacation }; renderSettings(); await loadDay(); }}
    onPlacesChanged=${() => { if (state.dayData) loadDay(); schedulePreview(0); }}
    onProfileSaved=${refreshAnalysis} />`, $("#settings-view"));
}

// Overview above the entry form: today the last hours up to now, other days the whole day.
function overviewHours() {
  try { return Number(localStorage.getItem("overviewHours")) || 3; } catch { return 3; }
}

// The manual entry on phones covers "Heute", so it shows "Wirkt noch" itself (web/ui/today.js ActiveCard).
function renderEntryActive() {
  const host = $("#entry-active-host");
  if (!host) return;
  const show = $("#entry-drawer").dataset.quick === "manual" && state.day === todayDay() && state.preview?.on_board;
  render(show ? html`<${ActiveCard} id=${null} active=${state.preview.on_board} />` : null, host);
}

// "Heute" (web/ui/today.js): chart, "Wirkt noch" from the forecast, the last three hours.
function renderDayOverview() {
  renderEntryActive();
  render(html`<${Overview} data=${state.dayData} day=${state.day} today=${todayDay()} active=${state.preview?.on_board}
    hours=${overviewHours()} onEdit=${editEntry} onHours=${hours => { try { localStorage.setItem("overviewHours", hours); } catch {} renderDayOverview(); }} />`,
    $("#overview-host"));
}

// Today opens "Jetzt", other days open "Tag durchgehen"; a tap on the switch overrides it for that day.
function applyDiaryMode() {
  if (state.modeDay !== state.day) {
    state.modeDay = state.day;
    state.diaryMode = state.day === todayDay() ? "now" : "review";
  }
  $("#diary-view").dataset.mode = state.diaryMode;
  updateNav();
}

function renderVisibleCharts() {
  if (!state.dayData) return;
  renderDayOverview();
}


function renderDay(data) {
  state.dayData = data;
  renderEntry();
  const banner = $("#banner");
  const errors = [data.sync_error, data.profile_sync_error].filter(Boolean);
  banner.hidden = data.connected && !errors.length;
  banner.textContent = !data.connected
    ? t("app.notConnected")
    : errors.length ? t("app.syncFailed", { errors: errors.join(" · ") }) : "";
  state.dayStatus = "ready";
  applyDiaryMode();
  renderReview();
  renderVisibleCharts();
}

// Verlauf (web/ui/day.js): week strip, the day's meal and sport cards, the window editor.
function renderReview() {
  render(html`<${DayReview} data=${state.dayData} day=${state.day} today=${todayDay()} status=${state.dayStatus} reload=${loadDay}
    onEdit=${editEntry} onAnalysisChanged=${refreshAnalysis} onDay=${historyDay} onRefresh=${() => loadDay({ refresh: true })} />`, $(".review-panel"));
}

// Verlauf's week strip picks a day and stays in Verlauf, also for today.
function historyDay(day) {
  if (day === state.day) return;
  changeDay(day, { load: false });
  state.modeDay = day;
  state.diaryMode = "review";
  applyDiaryMode();
  loadDay();
}

// The server reads its local copy, which the background sync keeps current; refresh asks Nightscout first.
// Only the latest request is shown: a quick change of day (a swipe through the weeks and back) can have an
// earlier day's answer arrive last, which would otherwise show that day under the chosen one.
let dayRequest = 0;

async function loadDay({ refresh = false } = {}) {
  const request = ++dayRequest;
  state.dayLoadedAt = Date.now();
  $("#selected-day").value = state.day;
  // Another day shows "loading"; reloading the same day keeps the list (and what is unfolded) in place.
  if (state.dayData?.day !== state.day) {
    state.dayStatus = "loading";
    renderReview();
  }
  try {
    const data = await api("/api/day?date=" + encodeURIComponent(state.day) + (refresh ? "&refresh=1" : ""));
    if (request !== dayRequest) return;
    renderDay(data);
    loadReviewPending();
    await updatePreview();
  } catch (error) {
    if (request !== dayRequest) return;
    state.dayStatus = "error";
    renderReview();
    toast(error.message, true);
  }
}

// The red dot on "Verlauf" (user request 2026-11-24): finished days (from the server's start date up to
// yesterday) with an entry not yet checked, oldest first; a tap on "Verlauf" opens the first of them.
let reviewPending = [];

async function loadReviewPending() {
  try { reviewPending = (await api("/api/review/pending")).days || []; } catch (_) { return; }
  const link = $('.appnav-item[data-nav="history"]');
  if (!link) return;
  const count = reviewPending.length;
  link.classList.toggle("has-pending", count > 0);
  if (count) link.setAttribute("aria-label", t("app.historyPending", { count }));
  else link.removeAttribute("aria-label");
}

// The header reads Nightscout directly, the day the local copy. On the iPhone home screen the page
// stays alive for hours without a reload, so today's day follows on its own once the header is ahead
// of the chart. The copy catches up with each background round, hence the pause between attempts.
const DAY_CATCH_UP_MS = 2 * 60000;
let dayCatchUp = null;

function catchUpDay(latestAt) {
  const data = state.dayData;
  if (document.hidden || dayCatchUp || !data || state.day !== todayDay() || data.day !== state.day) return;
  if (Date.now() - (state.dayLoadedAt || 0) < DAY_CATCH_UP_MS) return;
  const points = glucosePoints(data.entries || []);
  if (points.length && points[points.length - 1].t >= latestAt) return;
  // Someone typing in the list keeps it as it is; the next attempt follows.
  if ($("#timeline").contains(document.activeElement)) return;
  const day = state.day;
  state.dayLoadedAt = Date.now();
  dayCatchUp = (async () => {
    try {
      const fresh = await api("/api/day?date=" + encodeURIComponent(day));
      if (day !== state.day || $("#timeline").contains(document.activeElement)) return;
      // Folded and unfolded blocks stay as the user left them.
      renderDay(fresh);
      updatePreview();
    } catch {
      // Quietly retried with the next glucose value.
    }
  })().finally(() => { dayCatchUp = null; });
}

function todayDay() {
  return new Date().toLocaleDateString("sv-SE", { timeZone: appZone() });
}

// The entry form (web/ui/entry.js). It reaches the server for its preview and saving itself; `entry` is
// how the page talks to it (the minute tick, "+", Ctrl+Enter, a changed day), filled in by the form.
const entry = {
  onManualKind: () => {
    // Switching the kind by hand (wide screens) drops the title the "+" choice gave.
    $("#entry-title").textContent = t("entry.addTitle");
    delete $("#entry-drawer").dataset.quick;
    renderEntryActive();
  },
};

function renderEntry() {
  render(html`<${EntryForm} day=${state.day} today=${todayDay()} settings=${state.settings} agentInfo=${agent.info}
    connected=${Boolean(state.dayData?.connected)} control=${entry} onGlucose=${loadCurrentGlucose}
    onPreview=${preview => { state.preview = preview; renderDayOverview(); }}
    onReloadDay=${loadDay}
    onSaved=${async (result, dayView) => {
      state.day = result.day;
      updateLocation(true);
      $("#selected-day").value = state.day;
      closeEntryDrawer();
      if ($("#edit-dialog").open) closeEditDialog();
      if (dayView) renderDay(dayView);
      else await loadDay();
    }} />`, $("#entry-form-host"));
}

const updatePreview = () => entry.preview?.();
const schedulePreview = (delay = 350) => entry.schedule?.(delay);
// Today's preview follows new data on its own; the minute tick and the return to the foreground call this.
const refreshTodayPreview = () => entry.refresh?.();

function closeEditDialog() {
  state.editing = null;
  entryCard.close();
}

function resetEntryForm() {
  entry.reset?.();
  closeEntryDrawer();
  if ($("#edit-dialog").open) closeEditDialog();
}

// A saved entry opens as a card (web/ui/entry-card.js); here what it needs from the day and how it saves.
function editEntry(id) {
  const item = state.dayData?.timeline.find(row => row.id === id);
  if (!item || item.kind === "other" || item.kind === "finger") return;
  state.editing = item;
  const meal = item.kind === "meal" ? state.dayData.meals.find(row => row.source_id === item.id) : null;
  const factor = meal?.calculation?.factor_units_per_ke;
  const sportDefaults = Object.fromEntries(Object.keys(sportNames).map(kind => [kind, {
    effect: state.settings?.sport_effects_mgdl?.[kind] ?? 0,
    minutes: state.settings?.sport_durations_minutes?.[kind] ?? 300 }]));
  // The session's own default (from the day) wins for its current kind.
  if (item.kind === "exercise" && item.sport_kind) sportDefaults[item.sport_kind] = {
    effect: item.sport_effect_default_mgdl, minutes: item.sport_minutes_default };
  entryCard.open({
    item: { ...item, time: timeOnly(item.at), title: entryLabel(item, state.dayData) },
    dateLabel: new Date(state.day + "T12:00:00").toLocaleDateString(locale, { weekday: "short", day: "numeric", month: "long" }) +
      " · " + timeOnly(item.at),
    factor: factor == null ? null : Number(factor),
    places: state.dayData.place_options || [], dishes: state.dayData.dish_options || [],
    sportDefaults, agentInfo: agent.info,
    instant: time => { try { return toInstant(state.day, time); } catch (_) { return null; } },
    onSave: values => saveEditedEntry(item, values),
    onDelete: () => deleteEditedEntry(item),
    onClosed: () => { state.editing = null; },
  });
}

// The card sends what it shows; the server derives the kind and checks everything (diary.edit_entry).
async function saveEditedEntry(item, values) {
  const basal = item.kind === "basal";
  if (basal) state.pendingBasal = state.pendingBasal || crypto.randomUUID();
  const result = await post("/api/entry/edit", { ...values, id: item.id, original_day: state.day, original_kind: item.kind,
    at: toInstant(state.day, values.at), ...(basal ? { event_id: state.pendingBasal } : {}) });
  if (basal) state.pendingBasal = null;
  return finishEdit(result.day, basal ? t("app.basalSaved") : t("app.changeSaved"), result.day_view);
}

async function finishEdit(day, message, refreshed = null) {
  state.day = day;
  $("#selected-day").value = state.day;
  updateLocation(true);
  if (refreshed) renderDay(refreshed);
  else await loadDay();
  await updatePreview();
  refreshAnalysis();
  closeEditDialog();
  toast(message);
}

async function deleteEditedEntry(item) {
  if (item.kind === "basal") {
    await deleteBasal(item.id);
    toast(t("app.basalDeleted"));
  } else {
    await api("/api/treatment/delete", { method: "POST", body: JSON.stringify({ id: item.id, day: state.day }) });
    toast(t("common.deletedFromNightscout"));
  }
  closeEditDialog();
  await loadDay();
}

document.addEventListener("DOMContentLoaded", async () => {
  const initialLocation = locationState();
  state.day = todayDay();
  state.view = initialLocation.view;
  analysisTab(initialLocation.analysisTab, { updateHistory: false });
  $("#selected-day").value = state.day;
  renderEntry();
  view(state.view, { updateHistory: false });
  updateLocation(true);
  window.addEventListener("popstate", () => {
    const nextLocation = locationState();
    analysisTab(nextLocation.analysisTab, { updateHistory: false });
    view(nextLocation.view, { updateHistory: false });
    if (nextLocation.view === "diary") loadDay();
  });
  $("#prev-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() - 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#next-day").addEventListener("click", () => { const d = new Date(state.day + "T12:00"); d.setDate(d.getDate() + 1); changeDay(d.toLocaleDateString("sv-SE")); });
  $("#selected-day").addEventListener("change", event => changeDay(event.target.value));
  $("#refresh-day").addEventListener("click", () => loadDay({ refresh: true }));
  $("#timezone-banner").addEventListener("click", event => {
    const button = event.target.closest("[data-timezone-switch]");
    if (button) switchTimezone({ zone: button.dataset.timezoneSwitch });
  });
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("#diary-view").hidden) {
      event.preventDefault();
      entry.submit?.();
    }
  });
  const initialGlucose = loadCurrentGlucose();
  try {
    await loadSettings();
    await Promise.all([loadDay(), initialGlucose]);
  } catch (error) {
    toast(error.message, true);
  }
  setInterval(() => { if (!document.hidden) loadCurrentGlucose(); }, 60000);
  // New CGM values arrive every minute and "Wirkt noch" ages, so today's suggestion follows on its own
  // and a tapped suggestion rarely meets a newer one on saving.
  setInterval(refreshTodayPreview, 60000);
  // A form left open keeps showing "jetzt" with the current clock, and an old manual time expires.
  setInterval(() => { if (!document.hidden) entry.tick?.(); }, 30000);
  setInterval(returnToToday, 60000);
  document.addEventListener("pointerdown", markDayActivity, true);
  document.addEventListener("keydown", markDayActivity, true);
  // A home-screen app has no reload button: coming back to the foreground refreshes everything shown
  // and picks up a deployed update.
  const resume = async () => {
    if (document.hidden) return;
    if (reloadForUpdate()) return;
    renderTimezone();
    renderSettings();
    returnToToday();
    entry.tick?.();
    refreshTodayPreview();
    loadReviewPending();
    await loadCurrentGlucose();
    reloadForUpdate();
  };
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("pageshow", event => { if (event.persisted) resume(); });
  window.addEventListener("focus", loadCurrentGlucose);
});

// ---- Meal agent (user request 2026-11-16): photos and a description in, carbs and a dish out. ----
// The agent only fills the form; the dose help calculates as always. One chat serves the entry form
// and the edit dialog ("Tag durchgehen", where it continues an earlier estimate: "Nachbessern").
const agent = { info: null };

async function loadAgent() {
  try { agent.info = await api("/api/agent"); } catch (_) { agent.info = null; }
  renderSettings();
  renderEntry();
}

// ---- Navigation and quick entry (concept 2026-11-18) ----
// Phones and tablets: tab bar at the bottom (Heute, Verlauf, +, Analyse, Mehr), the entry as a
// full-screen sheet opened from "+", settings as one page at a time. Wide screens: a rail on the left,
// the entry stays in the page. The breakpoint matches the CSS.
const compactLayout = () => matchMedia("(max-width: 950px)").matches;
const QUICK_TITLES = { meal: t("quick.meal"), manual: t("quick.manual"), exercise: t("quick.exercise"), basal: t("quick.basal") };

function updateNav() {
  const current = state.view === "diary" ? (state.diaryMode === "review" ? "history" : "today") : state.view;
  $$(".appnav-item").forEach(link => {
    const active = link.dataset.nav === current;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

function navigate(target) {
  if (target === "today" || target === "history") {
    view("diary");
    if (target === "today" && state.day !== todayDay()) changeDay(todayDay());
    if (target === "history" && reviewPending.length && !reviewPending.includes(state.day)) changeDay(reviewPending[0]);
    state.modeDay = state.day;
    state.diaryMode = target === "today" ? "now" : "review";
    applyDiaryMode();
    renderVisibleCharts();
    if (target === "history" && !compactLayout()) $(".review-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    else window.scrollTo({ top: 0 });
  } else {
    view(target);
    window.scrollTo({ top: 0 });
  }
}

// Each choice says what it would do: the correction dose now, whether today's basal is still open.
function quickAddHints() {
  const today = state.day === todayDay();
  const hints = { meal: agent.info?.available ? t("app.hints.mealAgent") : t("app.hints.meal"), manual: t("app.hints.manual"),
    exercise: t("app.hints.exercise"), basal: "" };
  const preview = state.preview;
  const suggested = today && preview && preview.day === state.day && preview.mode === "meal" && !(Number(preview.carbs_g) > 0)
    ? (preview.options || []).find(option => option.suggested) : null;
  if (suggested && Number(suggested.units) > 0) hints.manual = t("app.hints.correction", { units: num(suggested.units, 1), unit: UNIT.insulin });
  if (today && state.dayData) {
    const doses = state.dayData.basal || [];
    hints.basal = doses.length ? t("app.hints.basal", { units: num(doses[doses.length - 1].units, 1), unit: UNIT.insulin, time: timeOnly(doses[doses.length - 1].at) }) : t("app.hints.basalOpen");
  }
  return hints;
}

function openQuickAdd() {
  quickAdd.open({ hints: quickAddHints(), onChoose: chooseEntry });
}

function chooseEntry(kind) {
  if (state.view !== "diary") view("diary");
  $("#entry-title").textContent = QUICK_TITLES[kind] || t("entry.addTitle");
  // "KE & IE" is the manual entry: no "Essen einschätzen" there, and it shows what still acts (user feedback 2026-11-18).
  $("#entry-drawer").dataset.quick = kind;
  renderEntryActive();
  openEntryDrawer();
  const agentChat = kind === "meal" && agent.info?.available;
  entry.choose?.(kind, agentChat ? null : kind);
  if (agentChat) entry.openAgent?.();
}

function openEntryDrawer() {
  if (compactLayout()) {
    loadCurrentGlucose();
    document.body.classList.add("entry-open");
    $("#entry-drawer").scrollTop = 0;
  } else {
    $("#entry-drawer").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function closeEntryDrawer() {
  document.body.classList.remove("entry-open");
  const title = $("#entry-title");
  if (title) title.textContent = t("entry.addTitle");
  delete $("#entry-drawer")?.dataset.quick;
  renderEntryActive();
  renderEntry();
}

// Settings on phones: a list of pages, one page at a time (the CSS reads data-page and page-in-form).
function showSettingsPage(id) {
  const root = $("#settings-view");
  state.settingsPage = id;
  if (id) root.dataset.page = id;
  else delete root.dataset.page;
  root.classList.toggle("page-in-form", FORM_PAGES.has(id));
  renderSettings();
  window.scrollTo({ top: 0 });
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".appnav-item").forEach(link => link.addEventListener("click", event => {
    event.preventDefault();
    navigate(link.dataset.nav);
  }));
  $("#nav-add").addEventListener("click", openQuickAdd);
  $("#entry-back").addEventListener("click", () => { closeEntryDrawer(); openQuickAdd(); });
  $("#entry-close").addEventListener("click", closeEntryDrawer);
  updateNav();
});

// Tables become cards on phones (concept 2026-11-18, user report: tables are bad on mobile): every
// cell carries its column's name, which the phone layout shows above the value (styles.css).
function labelTables(root) {
  // Preact adds rows to a table that is already on the page, so a row counts for its table too.
  const own = root.closest?.(".analysis-table");
  const tables = own ? [own] : root.querySelectorAll?.(".analysis-table") || [];
  for (const table of tables) {
    const heads = [...table.querySelectorAll("thead th")].map(cell => cell.textContent.replace(/\s+/g, " ").trim());
    for (const row of table.querySelectorAll("tbody tr")) {
      let column = 0;
      for (const cell of row.children) {
        if (cell.tagName === "TD" && cell.colSpan === 1 && heads[column] && !cell.dataset.label) cell.dataset.label = heads[column];
        column += cell.colSpan || 1;
      }
    }
  }
}
new MutationObserver(records => {
  for (const record of records) for (const node of record.addedNodes) if (node.nodeType === 1) labelTables(node);
}).observe(document.documentElement, { childList: true, subtree: true });
