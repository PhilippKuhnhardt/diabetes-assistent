// The dialogs of the page (meal chat, a saved entry's card, "+"), opened through the small openers
// exported here (mealChat, entryCard, quickAdd).
import { html, render } from "./html.js";
import { MealChat } from "./agent-chat.js";
import { EntryCard } from "./entry-card.js";
import { QuickAdd } from "./quick-add.js";

// A component shown in a <dialog>: open() mounts it fresh and shows the dialog, close() unmounts it
// (which also stops what it runs, e.g. a recording) and closes the dialog. Esc closes the same way.
function dialogHost(id, onClosed = () => {}, closeOnBackdrop = false) {
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
    // A tap beside the sheet (on the backdrop, which belongs to the dialog itself) closes it.
    if (closeOnBackdrop) node().addEventListener("click", event => { if (event.target === node()) close(); });
  });
  return { show, close, isOpen: () => node().open };
}

const chat = dialogHost("agent-dialog");
// options: { info, eventId, sessionId, initialText, previousCarbs, context() -> {at, placeId}, onApply }
export const mealChat = {
  open: options => chat.show(html`<${MealChat} ...${options} onClose=${chat.close}
    onApply=${result => { options.onApply(result); chat.close(); }} />`),
  close: chat.close,
};

let cardClosed = () => {};
const card = dialogHost("edit-dialog", () => cardClosed());
// options: see EntryCard; onClosed runs whenever the card goes away.
export const entryCard = {
  open: options => {
    cardClosed = options.onClosed || (() => {});
    card.show(html`<${EntryCard} key=${options.item.id} ...${options} onClose=${card.close} />`);
  },
  close: card.close,
  isOpen: card.isOpen,
};

const add = dialogHost("add-dialog", () => {}, true);
// options: { hints: {meal, correction, carbs, exercise, basal}, onChoose(kind) }
export const quickAdd = {
  open: options => add.show(html`<${QuickAdd} hints=${options.hints} onClose=${add.close}
    onChoose=${kind => { add.close(); options.onChoose(kind); }} />`),
  close: add.close,
};
