// Entry point for the Preact parts of the page. app.js stays the classic script that owns the
// page; it opens these components through window.mealChat (and later siblings).
import { html, render } from "./html.js";
import { MealChat } from "./agent-chat.js";

const dialog = () => document.getElementById("agent-dialog");

function close() {
  const node = dialog();
  render(null, node);  // unmounting stops a running recording
  if (node.open) node.close();
}

// options: { target, info, eventId, initialText, previousCarbs, context() -> {at, placeId}, api, toast, onApply }
function open(options) {
  const node = dialog();
  render(null, node);
  render(html`<${MealChat} ...${options} onClose=${close}
    onApply=${result => { options.onApply(result); close(); }} />`, node);
  if (!node.open) node.showModal();
}

document.addEventListener("DOMContentLoaded", () => {
  dialog().addEventListener("cancel", event => { event.preventDefault(); close(); });
});
window.mealChat = { open, close };
