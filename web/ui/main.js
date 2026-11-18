// Entry point for the Preact parts of the page. app.js stays the classic script that owns the
// page; it opens these components through small openers on window (mealChat, entryCard).
import { html, render } from "./html.js";
import { MealChat } from "./agent-chat.js";
import { EntryCard } from "./entry-card.js";

// A component shown in a <dialog>: open() mounts it fresh and shows the dialog, close() unmounts it
// (which also stops what it runs, e.g. a recording) and closes the dialog. Esc closes the same way.
function dialogHost(id, onClosed = () => {}) {
  const node = () => document.getElementById(id);
  function close() {
    render(null, node());
    if (node().open) node().close();
    onClosed();
  }
  function show(vnode) {
    render(null, node());
    render(vnode, node());
    if (!node().open) node().showModal();
  }
  document.addEventListener("DOMContentLoaded", () => {
    node().addEventListener("cancel", event => { event.preventDefault(); close(); });
  });
  return { show, close, isOpen: () => node().open };
}

const chat = dialogHost("agent-dialog");
// options: { target, info, eventId, initialText, previousCarbs, context() -> {at, placeId}, api, toast, onApply }
window.mealChat = {
  open: options => chat.show(html`<${MealChat} ...${options} onClose=${chat.close}
    onApply=${result => { options.onApply(result); chat.close(); }} />`),
  close: chat.close,
};

let cardClosed = () => {};
const card = dialogHost("edit-dialog", () => cardClosed());
// options: see EntryCard; onClosed runs whenever the card goes away.
window.entryCard = {
  open: options => {
    cardClosed = options.onClosed || (() => {});
    card.show(html`<${EntryCard} key=${options.item.id} ...${options} onClose=${card.close} />`);
  },
  close: card.close,
  isOpen: card.isOpen,
};
