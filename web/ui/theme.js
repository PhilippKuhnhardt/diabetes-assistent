// "Darstellung": light, dark or the device's setting (2026-11-26). The choice lives per device in
// localStorage; web/theme.js applies it before the first paint, this panel changes it live.
import { html, useState } from "./html.js";

const CHOICES = [["system", "System"], ["light", "Hell"], ["dark", "Dunkel"]];

export function storedTheme() {
  try { return localStorage.getItem("theme") || "system"; } catch (error) { return "system"; }
}

export function applyTheme(choice) {
  try {
    if (choice === "system") localStorage.removeItem("theme"); else localStorage.setItem("theme", choice);
  } catch (error) { /* storage blocked: the choice holds until the page reloads */ }
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;
}

export function ThemePanel({ page }) {
  const [choice, setChoice] = useState(storedTheme);
  const pick = value => { applyTheme(value); setChoice(value); };
  return html`<section class=${"panel theme-panel" + (page === "theme" ? " is-current" : "")} data-settings-page="theme">
    <h2>Darstellung</h2>
    <div class="entry-switch theme-switch" role="group" aria-label="Darstellung">
      ${CHOICES.map(([value, label]) => html`<button type="button" class="entry-mode" aria-pressed=${String(choice === value)}
        onClick=${() => pick(value)}>${label}</button>`)}
    </div>
    <p class="hint">„System“ folgt der Einstellung des Geräts. Die Wahl gilt nur auf diesem Gerät.</p>
  </section>`;
}
