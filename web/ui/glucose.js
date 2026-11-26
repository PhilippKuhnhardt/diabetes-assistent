// The current glucose as a small badge for full-screen views that cover the top bar (meal chat):
// a dose decision always needs the value in sight (user feedback 2026-11-18). app.js publishes each
// refresh as window.currentGlucose and a "glucose:update" event.
import { html, useState, useEffect } from "./html.js";
import { t } from "./i18n.js";

export function GlucoseBadge() {
  const [glucose, setGlucose] = useState(window.currentGlucose || null);
  useEffect(() => {
    const update = event => setGlucose(event.detail);
    document.addEventListener("glucose:update", update);
    return () => document.removeEventListener("glucose:update", update);
  }, []);
  if (!glucose) return null;
  return html`<span class="glucose-badge" data-status=${glucose.status} role="status" aria-label=${t("glucose.current")}>
    <strong>${glucose.value}</strong>${glucose.trend && html`<small>${glucose.trend}</small>`}</span>`;
}
