// "+" (concept 2026-11-18): a short choice of what to enter. Food with the agent is the big button,
// the rest one tap away; each tile already says what it would do (the correction dose now, whether
// basal is still open today). Choosing hands over to app.js, which opens the entry in that mode.
import { html } from "./html.js";

const CHOICES = [
  { key: "meal", title: "✦ Essen", main: true },
  { key: "correction", title: "Korrektur" },
  { key: "carbs", title: "KH direkt" },
  { key: "exercise", title: "Sport" },
  { key: "basal", title: "Basal" },
];

export function QuickAdd({ hints, onChoose, onClose }) {
  return html`<div class="quick-add">
    <div class="quick-add-head"><span class="grab" aria-hidden="true"></span>
      <h2>Eintragen</h2><button type="button" class="quiet" aria-label="Schließen" onClick=${onClose}>×</button></div>
    <div class="choices">
      ${CHOICES.map(choice => html`<button type="button" class=${"choice" + (choice.main ? " main" : "")} data-choice=${choice.key}
        onClick=${() => onChoose(choice.key)}><b>${choice.title}</b><small>${hints[choice.key] || ""}</small></button>`)}
    </div>
  </div>`;
}
