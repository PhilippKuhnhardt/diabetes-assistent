// "+" (concept 2026-11-18): a short choice of what to enter, four tiles in a grid; each tile already says what it would do (the correction dose now, whether
// basal is still open today). Choosing hands over to app.js, which opens the entry in that mode.
import { html } from "./html.js";
import { t } from "./i18n.js";

// Four tiles (user request 2026-11-18): the agent, one full manual entry for carbs and insulin (also a
// correction alone), sport and basal.
const CHOICES = [
  { key: "meal", title: "✦ " + t("quick.meal"), main: true },
  { key: "manual", title: t("quick.manual") },
  { key: "exercise", title: t("quick.exercise") },
  { key: "basal", title: t("quick.basal") },
];

export function QuickAdd({ hints, onChoose, onClose }) {
  return html`<div class="quick-add">
    <div class="quick-add-head"><span class="grab" aria-hidden="true"></span>
      <h2>${t("quick.title")}</h2><button type="button" class="quiet" aria-label=${t("common.close")} onClick=${onClose}>×</button></div>
    <div class="choices">
      ${CHOICES.map(choice => html`<button type="button" class=${"choice" + (choice.main ? " main" : "")} data-choice=${choice.key}
        onClick=${() => onChoose(choice.key)}><b>${choice.title}</b><small>${hints[choice.key] || ""}</small></button>`)}
    </div>
  </div>`;
}
