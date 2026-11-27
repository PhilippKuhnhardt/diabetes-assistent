// Entries whose save got no answer (no network, timeout, proxy error). They are kept on this device until
// they are sent or discarded by hand, never sent on their own: until then their insulin is missing from
// "Wirkt noch" and every dose, and the list says so. Sending again reuses the entry's event id, so an entry
// that did reach the server before the answer was lost is not stored twice (services/diary.ensure_not_duplicate).
import { html, useEffect, useState } from "./html.js";
import { t } from "./i18n.js";
import { post, toast, num, timeOnly, UNIT } from "./lib.js";

const KEY = "unsentEntries";
const CHANGE = "unsent:change";

export function unsentEntries() {
  try {
    const list = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(list) ? list.filter(item => item?.path && item?.payload?.event_id) : [];
  } catch (_) {
    return [];
  }
}

function store(list) {
  try {
    if (list.length) localStorage.setItem(KEY, JSON.stringify(list));
    else localStorage.removeItem(KEY);
  } catch (_) { /* Private mode: the form still holds the entry. */ }
  window.dispatchEvent(new Event(CHANGE));
}

// Keeps an entry whose save got no answer; the same event id replaces an older copy.
export function keepUnsent(path, payload) {
  store([...unsentEntries().filter(item => item.payload.event_id !== payload.event_id), { path, payload, kept_at: Date.now() }]);
}

export function dropUnsent(eventId) {
  if (eventId && unsentEntries().some(item => item.payload.event_id === eventId)) {
    store(unsentEntries().filter(item => item.payload.event_id !== eventId));
  }
}

function describe(payload) {
  const parts = [];
  if (Number(payload.carbs) > 0) parts.push(num(payload.carbs, 0) + " g");
  if (Number(payload.insulin) > 0) parts.push(num(payload.insulin, 2) + " " + UNIT.insulin);
  if (payload.units != null) parts.push(t("unsent.basal", { units: num(payload.units, 1), unit: UNIT.insulin }));
  else if (payload.with_basal) parts.push(t("unsent.withBasal"));
  if (payload.mode === "exercise") parts.push(t("sport.sport"));
  return parts.join(" · ") || t("unsent.entry");
}

// The list above the day: what was not saved, with "Senden" and "Verwerfen".
export function UnsentEntries({ onSaved }) {
  const [list, setList] = useState(unsentEntries);
  const [busy, setBusy] = useState(null);
  useEffect(() => {
    const update = () => setList(unsentEntries());
    window.addEventListener(CHANGE, update);
    window.addEventListener("storage", update);
    return () => { window.removeEventListener(CHANGE, update); window.removeEventListener("storage", update); };
  }, []);
  if (!list.length) return null;

  async function send(item) {
    setBusy(item.payload.event_id);
    try {
      // The tapped-suggestion check belonged to the moment of the tap; now the entry records what was injected.
      const result = await post(item.path, { ...item.payload, dose_check: null, dose_shown: null });
      dropUnsent(item.payload.event_id);
      toast(t("unsent.sent"));
      await onSaved?.(result);
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(null);
    }
  }

  function discard(item) {
    if (!window.confirm(t("unsent.confirmDiscard", { time: timeOnly(item.payload.at) }))) return;
    dropUnsent(item.payload.event_id);
  }

  return html`<div class="banner unsent" role="alert">
    <strong>${t("unsent.title", { count: list.length })}</strong>
    <p>${t("unsent.hint")}</p>
    <ul>${list.map(item => html`<li key=${item.payload.event_id}>
      <span>${t("common.clock", { time: timeOnly(item.payload.at) })} · ${describe(item.payload)}</span>
      <span class="unsent-actions">
        <button type="button" class="banner-action" disabled=${busy !== null} onClick=${() => send(item)}>${busy === item.payload.event_id ? t("unsent.sending") : t("unsent.send")}</button>
        <button type="button" class="banner-action quiet" disabled=${busy !== null} onClick=${() => discard(item)}>${t("unsent.discard")}</button>
      </span></li>`)}</ul>
  </div>`;
}
