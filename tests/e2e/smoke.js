// Click test of the app shell in a real browser (CI: .github/workflows/tests.yml).
// Needs tests/e2e/server.py running (fake Nightscout + app on http://127.0.0.1:8765).
// Phone and desktop: navigation, "+" with each choice, a saved entry as a card, settings pages.
const { chromium, devices } = require("playwright");

const BASE = process.env.APP_URL || "http://127.0.0.1:8765";
const failures = [];
const check = (ok, what) => { console.log((ok ? "ok   " : "FAIL ") + what); if (!ok) failures.push(what); };

async function run(browser, name, options) {
  const context = await browser.newContext({ ...options, timezoneId: "Europe/Berlin", locale: "de-DE" });
  const page = await context.newPage();
  page.on("pageerror", error => check(false, `${name}: page error ${error.message}`));
  await page.goto(BASE + "/?view=diary");
  await page.waitForSelector(".appnav-item.active");
  await page.waitForSelector("#record-count:not(:empty)", { state: "attached" });
  const compact = name === "phone";

  check(await page.isVisible(".appnav"), `${name}: navigation visible`);
  check(!(await page.isVisible(".tabs")), `${name}: old tabs hidden`);

  if (!compact) await doseHelp(page, name);

  for (const choice of ["meal", "manual", "exercise", "basal"]) {
    await page.click("#nav-add");
    await page.waitForSelector("#add-dialog[open] .choice");
    check((await page.$$(".choice")).length === 4, `${name}: "+" offers 4 choices`);
    await page.click(`.choice[data-choice=${choice}]`);
    check(!(await page.evaluate(() => document.querySelector("#add-dialog").open)), `${name}: choice ${choice} closes "+"`);
    const kind = await page.inputValue("#entry-kind");
    check(kind === (choice === "exercise" || choice === "basal" ? choice : "meal"), `${name}: choice ${choice} sets kind ${kind}`);
    if (compact) check(await page.isVisible("#entry-drawer"), `${name}: entry sheet open for ${choice}`);
    if (await page.evaluate(() => document.querySelector("#agent-dialog").open)) await page.keyboard.press("Escape");
    if (compact) {
      await page.click("#entry-close");
      check(!(await page.isVisible("#entry-drawer")), `${name}: entry sheet closes`);
    }
  }

  // Heute (web/ui/today.js): the chart span switches; a hint in "Zu erledigen" opens its meal and folds away.
  await page.click("[data-nav=today]");
  await page.click("[data-overview-hours='6']");
  check(await page.getAttribute("[data-overview-hours='6']", "aria-pressed") === "true", `${name}: the chart shows 6 h`);
  await page.click("[data-overview-hours='3']");
  check((await page.$$(".meal-today")).length > 0, `${name}: Heute lists today's meals`);
  // A long meal row must not widen the page (iPhone report 2026-11-18).
  await page.evaluate(() => { document.querySelector(".meal-today span").textContent = "128 g · 11 IE · + 1 Snack · Abendessen lang"; });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: Heute fits the screen width`);
  // A meal row unfolds to its single entries; each opens its card.
  const head = page.locator(".meal-today").first();
  if (await head.getAttribute("aria-expanded") !== "true") await head.click();
  check(await page.isVisible(".meal-today-entry >> nth=0"), `${name}: a meal of Heute unfolds to its entries`);
  await page.click(".meal-today-entry:not([disabled]) >> nth=0");
  await page.waitForSelector("#edit-dialog[open] .entry-sheet");
  check(true, `${name}: an entry of a meal in Heute opens its card`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#edit-dialog").open);
  check(!(await page.$("#inbox, #inbox-badge")), `${name}: no "Zu erledigen" list any more`);

  await page.click("[data-nav=history]");
  check(await page.isVisible(".review-panel"), `${name}: Verlauf shows the day`);
  await history(page, name);
  await page.click("#timeline .history-more-entries >> nth=0");
  const card = await page.$("#timeline .history-meal .edit-entry:visible");
  check(Boolean(card), `${name}: a saved entry has a card button`);
  if (card) {
    await card.click();
    await page.waitForSelector("#edit-dialog[open] .entry-sheet");
    check(true, `${name}: entry card opens`);
    await page.keyboard.press("Escape");
  }
  await editMeal(page, name);
  await addFromHistory(page, name, compact);

  await page.click("[data-nav=analysis]");
  check(await page.isVisible("#analysis-view"), `${name}: Analyse opens`);
  await analysis(page, name);
  await page.click("[data-nav=settings]");
  check(await page.isVisible("#settings-view"), `${name}: Mehr opens settings`);
  if (compact) {
    check(await page.isVisible("#settings-index"), `${name}: settings list visible`);
    await page.click("[data-settings-open] >> text=Spritzhilfe");
    check(await page.isVisible("[name=dose_target_mgdl]"), `${name}: settings page Spritzhilfe`);
    check(await page.isVisible(".settings-save"), `${name}: save button on a model page`);
    // "Gültig ab" and "Uhrzeit" sit side by side without overlapping (iPhone report 2026-11-26).
    check(await page.evaluate(() => {
      const [date, time] = ["effective_date", "effective_time"].map(field => document.querySelector(`[name=${field}]`).getBoundingClientRect());
      return date.right <= time.left && Math.abs(date.top - time.top) < 1 && Math.abs(date.height - time.height) < 1;
    }), `${name}: Gültig ab and Uhrzeit side by side`);
    // "Abmelden" only on the list of pages (the test server has no login, so the form is unhidden here).
    await page.evaluate(() => document.querySelector("#logout-form").removeAttribute("hidden"));
    check(!(await page.isVisible("#logout-form")), `${name}: no Abmelden on a settings page`);
    await page.click("#settings-back");
    check(await page.isVisible("#settings-index"), `${name}: back to the settings list`);
    check(await page.isVisible("#logout-form"), `${name}: Abmelden on the settings list`);
    // "Darstellung": dark holds over a reload, "System" removes the choice again.
    await page.click("[data-settings-open=theme]");
    await page.click(".theme-switch button >> text=Dunkel");
    await page.reload();
    await page.waitForSelector(".appnav-item.active");
    check(await page.evaluate(() => document.documentElement.dataset.theme) === "dark", `${name}: dark theme kept after reload`);
    await page.click("[data-nav=settings]");
    await page.click("[data-settings-open=theme]");
    await page.click(".theme-switch button >> text=System");
    check(await page.evaluate(() => !document.documentElement.dataset.theme), `${name}: System follows the device`);
    await page.click("#settings-back");
  } else {
    check(await page.isVisible("[name=dose_target_mgdl]"), `${name}: all settings on one page`);
    await settings(page, name);
    await saving(page, name);
  }
  await reviewDay(page, name);
  await page.click("[data-nav=today]");
  const active = await page.$$eval(".appnav-item.active", items => items.map(item => item.dataset.nav));
  check(active.join() === "today", `${name}: Heute active`);
  await context.close();
}

// Dose help (web/ui/dose.js): carbs bring the slider with the expected value; moving it fills the insulin
// field, an own dose stops it following, and an emptied field hands the dose back.
async function doseHelp(page, name) {
  const form = "#entry-form";
  await page.fill(form + " [name=carbs]", "60");
  await page.waitForSelector("#dose-result .ds-range");
  check(await page.isVisible("#dose-result .ds-pill"), `${name}: the slider shows where glucose lands`);
  const readout = () => page.$eval("#dose-result .ds-dose strong", node => node.firstChild.textContent);
  const max = await page.getAttribute("#dose-result .ds-range", "max");
  await page.$eval("#dose-result .ds-range", (range, value) => {
    range.value = value;
    range.dispatchEvent(new Event("input", { bubbles: true }));
  }, max);
  await page.waitForFunction(value => document.querySelector("#dose-result .ds-range").value === value, max);
  const units = await page.inputValue(form + " [name=insulin]");
  check(units !== "" && (await readout()).replace(",", ".") === max, `${name}: moving the slider fills the insulin field`);
  // "0" beside the slider: no insulin with one tap, a second tap goes back to the recommendation.
  await page.click("#dose-result .ds-zero");
  check((await readout()) === "0" && await page.inputValue(form + " [name=insulin]") === "0"
    && await page.getAttribute("#dose-result .ds-zero", "aria-pressed") === "true", `${name}: "0" chooses no insulin`);
  check((await page.textContent("#entry-submit")).includes("speichern") && !(await page.textContent("#entry-submit")).includes("IE"), `${name}: "0" saves without insulin`);
  await page.click("#dose-result .ds-zero");
  check(await page.getAttribute("#dose-result .ds-zero", "aria-pressed") === "false" && await page.inputValue(form + " [name=insulin]") !== "0", `${name}: a second tap on "0" goes back`);
  await page.fill(form + " [name=insulin]", "0.5");
  check((await readout()) === "0,5" && await page.inputValue(form + " [name=insulin]") === "0.5", `${name}: an own dose shows on the slider`);
  await page.fill(form + " [name=insulin]", "");
  await page.fill(form + " [name=carbs]", "");
  await page.waitForFunction(() => !document.querySelector("#dose-result .dose-heading")?.textContent.includes("Empfehlung"));
  check(true, `${name}: no carbs, no meal recommendation`);
}

// The entry form (web/ui/entry.js) saves a meal with its dose and takes it back, and saves basal.
async function saving(page, name) {
  await page.click("[data-nav=today]");
  await page.click("[data-entry-mode=meal]");
  await page.fill("#entry-form [name=carbs]", "20");
  await page.fill("#entry-form [name=insulin]", "1");
  check((await page.textContent("#entry-submit")).includes("20 g + 1 IE"), `${name}: the button names what it saves`);
  await page.click("#entry-submit");
  await toastSays(page, "20 g + 1 IE gespeichert");
  check(await page.inputValue("#entry-form [name=carbs]") === "", `${name}: a saved entry empties the form`);
  await page.click("#toast .toast-action");
  await toastSays(page, "Eintrag aus Nightscout entfernt");
  await page.waitForFunction(() => document.querySelector("#entry-form [name=carbs]").value === "20");
  check(await page.inputValue("#entry-form [name=insulin]") === "1", `${name}: undo puts the entry back into the form`);
  await page.fill("#entry-form [name=carbs]", "");
  await page.fill("#entry-form [name=insulin]", "");
  await page.click("[data-entry-mode=basal]");
  check(Number(await page.inputValue("#entry-form [name=basal_units]")) > 0, `${name}: basal starts with the usual dose`);
  await page.fill("#entry-form [name=basal_units]", "2.5");
  await page.click("#entry-submit");
  await toastSays(page, "Basal 2,5 IE");
  await page.click("#toast .toast-action");
  await toastSays(page, "Basal entfernt");
  check(true, `${name}: basal is saved and taken back`);
  await page.click("[data-entry-mode=exercise]");
  check(await page.getAttribute("#entry-sport-effect-field [data-sport-effect=standard]", "aria-pressed") === "true", `${name}: sport starts with its kind's effect`);
  await page.click("#entry-sport-effect-field [data-sport-effect=own]");
  check(await page.isVisible("#entry-sport-effect-field [name=sport_effect_mgdl]"), `${name}: sport takes an own effect like its card`);
  await page.click("#entry-sport-effect-field [data-sport-effect=standard]");
  await page.click("[data-entry-mode=meal]");
}

// The meal card (web/ui/entry-card.js) saves through the server (/api/entry/edit), which checks it.
async function editMeal(page, name) {
  const more = page.locator("#timeline .history-more-entries").first();
  if (await more.getAttribute("aria-expanded") !== "true") await more.click();
  const card = page.locator("#timeline .history-meal .edit-entry").first();
  await card.click();
  await page.waitForSelector("#edit-dialog[open] .entry-sheet");
  const carbs = page.locator("#edit-dialog [aria-label='Kohlenhydrate in g']");
  const before = await carbs.inputValue();
  await carbs.fill("0");
  await page.fill("#edit-dialog [aria-label='Insulin in IE']", "0");
  await page.click("#edit-dialog .card-foot .primary");
  await toastSays(page, "Bitte Kohlenhydrate, Insulin oder beides eintragen.");
  check(await page.evaluate(() => document.querySelector("#edit-dialog").open), `${name}: the server refuses an empty meal on the card`);
  await carbs.fill(String(Number(before) + 5));
  await page.fill("#edit-dialog [aria-label='Insulin in IE']", "2");
  await page.click("#edit-dialog .card-foot .primary");
  await toastSays(page, "Änderung in Nightscout gespeichert");
  check(!(await page.evaluate(() => document.querySelector("#edit-dialog").open)), `${name}: a changed meal is saved from its card`);
}

const toastSays = (page, text) => page.waitForFunction(wanted => document.querySelector("#toast")?.textContent.includes(wanted), text);

// The red dot on "Verlauf": a finished day with unchecked entries shows it, a tap
// on "Verlauf" opens that day and "Tag als geprüft markieren" checks its meals at once.
async function reviewDay(page, name) {
  await page.click("[data-nav=today]");
  await page.waitForSelector(".appnav-item[data-nav=history].has-pending");
  check(true, `${name}: a red dot on Verlauf marks unchecked days`);
  await page.click("[data-nav=history]");
  await page.waitForSelector("#check-day");
  const day = await page.$eval(".week-panel.is-current .week-day[aria-current=date]", el => el.dataset.day);
  check(day < await page.evaluate(() => new Date().toLocaleDateString("sv-SE")), `${name}: Verlauf opens an unchecked earlier day`);
  check(!(await page.$("#timeline .entry-check")), `${name}: entries outside the meals need no check`);
  await page.click("#check-day");
  await page.waitForSelector("#review-summary.is-ok");
  check(!(await page.$("#check-day")), `${name}: "Tag als geprüft markieren" checks the whole day`);
}

// Verlauf (web/ui/day.js): the week strip picks a day with a task and one with sport, whose card shows
// where its effect lands; "Passt" checks a meal.
// A sideways swipe over the week strip: the row scrolls and snaps; dx > 0 goes back a week.
async function swipeWeek(page, dx) {
  const before = await page.textContent(".week-head span");
  await page.$eval(".week-track", (track, dx) => track.scrollTo({ left: track.scrollLeft - Math.sign(dx) * track.clientWidth }), dx);
  await page.waitForFunction(text => document.querySelector(".week-head span")?.textContent !== text, before);
  await page.waitForFunction(() => { const track = document.querySelector(".week-track"); return Math.abs(track.scrollLeft - track.clientWidth) < 2; });
}

async function history(page, name) {
  await page.waitForSelector(".week-panel.is-current .week-day");
  const shown = await page.getAttribute(".week-day[aria-current=date]", "data-day");
  await swipeWeek(page, 120);
  check(!(await page.$(`.week-panel.is-current .week-day[data-day="${shown}"]`)), `${name}: a swipe to the right shows the week before`);
  await swipeWeek(page, -120);
  check(!!(await page.$(`.week-panel.is-current .week-day[data-day="${shown}"]`)), `${name}: a swipe to the left comes back`);
  // The fake data has a sport session yesterday and a sensor gap two days ago.
  const yesterday = await page.evaluate(() => new Date(Date.now() - 864e5).toLocaleDateString("sv-SE", { timeZone: "Europe/Berlin" }));
  const yesterdayButton = `.week-panel.is-current .week-day[data-day="${yesterday}"]`;
  // Verlauf may open an earlier unchecked day (the red dot), so yesterday can lie in the week before or after.
  if (!(await page.$(yesterdayButton))) { await page.click(yesterday < shown ? ".week-prev" : ".week-next"); await page.waitForSelector(yesterdayButton); }
  await page.click(".week-panel.is-current .week-day[data-state=open] >> nth=0");
  await page.waitForSelector(".history-tasks a");
  check(true, `${name}: a day with an open task lists it`);
  const sport = await page.$(yesterdayButton);
  if (sport) {
    await sport.click();
    await page.waitForSelector("#timeline .history-sport");
    await page.click("#timeline .history-sport");
    await page.waitForSelector("#edit-dialog[open] .sport-shares li");
    check(true, `${name}: a sport session shows where its effect lands`);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("#edit-dialog").open);
  } else check(false, `${name}: the week has a day with sport`);
  const check0 = page.locator("#timeline .window-check").first();
  const was = await check0.getAttribute("aria-pressed");
  await check0.click();
  await page.waitForFunction(before => document.querySelector("#timeline .window-check")?.getAttribute("aria-pressed") !== before, was);
  await page.locator("#timeline .window-check").first().click();
  await page.waitForFunction(before => document.querySelector("#timeline .window-check")?.getAttribute("aria-pressed") === before, was);
  check(true, `${name}: "Passt" checks a meal and takes it back`);
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name}: Verlauf fits the screen width`);
}

// The innermost elements reaching past the screen's right edge (a wider page makes a phone zoom out).
function widerThanScreen(page) {
  return page.evaluate(() => {
    const limit = Math.min(screen.width, document.documentElement.clientWidth) + 1;
    return [...document.querySelectorAll("body *")].filter(el => {
      const box = el.getBoundingClientRect();
      // The week row scrolls sideways on purpose; its neighbour weeks lie off screen.
      return box.width && box.right > limit && !el.closest(".week-track") && ![...el.children].some(child => child.getBoundingClientRect().right > limit);
    }).slice(0, 8).map(el => el.tagName + "." + [...el.classList].join(".") + " " + Math.round(el.getBoundingClientRect().right) + " " + (el.textContent || "").trim().slice(0, 40));
  });
}

// Clicks a nav item; on a miss it prints what covers it, since CI fonts differ from a dev machine.
async function clickNav(page, nav) {
  try {
    await page.click(`[data-nav=${nav}]`, { timeout: 10000 });
  } catch (error) {
    console.log("covered nav", await page.evaluate(nav => {
      const link = document.querySelector(`[data-nav=${nav}]`), box = link.getBoundingClientRect();
      const chain = [];
      for (let el = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2); el; el = el.parentElement)
        chain.push(el.tagName + "." + [...el.classList].join(".") + "#" + el.id + " " + getComputedStyle(el).position + "/" + getComputedStyle(el).zIndex);
      return JSON.stringify({ box, vw: innerWidth, vh: innerHeight, sw: document.documentElement.scrollWidth, sy: scrollY,
        body: document.body.className, dialogs: [...document.querySelectorAll("dialog")].map(d => d.id + ":" + d.open), chain });
    }, nav));
    throw error;
  }
}

// Verlauf's day (web/ui/day.js): no day chart, effects or night any more, a meal excluded and counted again,
// and the window editor: dragging, nudging, saving and going back to the standard window.
async function review(page, name) {
  await page.waitForSelector("#timeline .history-meal");
  check(!(await page.$(".history-more, #review-chart, #effects-card, #night-card")), `${name}: Verlauf has no day chart, effects or night`);
  const more = page.locator("#timeline .history-meal:has(.exclude-meal) .history-more-entries").first();
  if (!(await more.count())) {
    const all = page.locator("#timeline .history-more-entries");
    for (let i = 0; i < await all.count(); i++) if (await all.nth(i).getAttribute("aria-expanded") !== "true") await all.nth(i).click();
  }
  else if (await more.getAttribute("aria-expanded") !== "true") await more.click();
  const exclude = "#timeline .exclude-meal";
  const before = await page.$eval(exclude, button => button.getAttribute("aria-pressed"));
  await page.click(exclude + " >> nth=0");
  await page.waitForFunction(([selector, was]) => document.querySelector(selector)?.getAttribute("aria-pressed") !== was, [exclude, before]);
  await page.click(exclude + " >> nth=0");
  await page.waitForFunction(([selector, was]) => document.querySelector(selector)?.getAttribute("aria-pressed") === was, [exclude, before]);
  check(true, `${name}: a meal in the day is excluded and counted again`);

  await page.waitForSelector("#timeline .meal-chart-plot svg");
  check(true, `${name}: a meal shows its curve`);
  await page.click("#timeline .open-window-editor >> nth=0");
  await page.waitForSelector("#window-dialog[open] #window-chart svg");
  const endTime = () => page.$eval("#window-summary .window-facts div:nth-child(2) strong", node => node.textContent);
  const end = await endTime();
  const handle = await page.$("#window-chart .window-handle[data-handle=end] .handle-line");
  const box = await handle.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x - 40, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();
  check(await endTime() !== end, `${name}: dragging moves the window end`);
  await page.click("[data-window-handle=start]");
  await page.click("[data-nudge='5']");
  await page.waitForSelector("#window-summary .window-state b");
  check(await page.getAttribute("[data-window-handle=start]", "aria-pressed") === "true", `${name}: the start handle is selected`);
  await page.click("#save-window");
  await toastSays(page, "Zeitraum gespeichert");
  await page.waitForSelector("#window-dialog", { state: "detached" });
  await page.waitForSelector("#timeline .day-meal-window:has-text('manuell')");
  check(true, `${name}: a saved window shows as manual`);
  await page.click("#timeline .open-window-editor >> nth=0");
  await page.waitForSelector("#window-dialog[open] #window-chart svg");
  await page.click("#default-window");
  await page.click("#save-window");
  await toastSays(page, "Standardzeitraum wiederhergestellt");
  await page.waitForFunction(() => !document.querySelector("#timeline .day-meal-window")?.textContent.includes("manuell"));
  check(true, `${name}: the standard window comes back`);
  await page.waitForSelector("#timeline .history-meal .window-check[aria-pressed=true]");
  check(true, `${name}: a window saved in the editor counts as checked`);
}

// Analyse (web/ui/analysis.js): Werte with the summary and every calculation, exclusion and day type,
// another period, Automatik and Gerichte, and a calculation opening its day (then reviewed there).
async function analysis(page, name) {
  await page.waitForSelector("#calculations tbody tr .exclude-meal");
  check((await page.$$("#summary .summary-card")).length === 3, `${name}: Werte shows three meal cards`);
  check((await page.$$("#calculations td[data-label]")).length > 0, `${name}: calculation cells carry their column`);
  const pressed = () => page.$eval("#calculations .exclude-meal", button => button.getAttribute("aria-pressed"));
  const before = await pressed();
  await page.click("#calculations .exclude-meal >> nth=0");
  await page.waitForFunction(was => document.querySelector("#calculations .exclude-meal")?.getAttribute("aria-pressed") !== was, before);
  await page.click("#calculations .exclude-meal >> nth=0");
  await page.waitForFunction(was => document.querySelector("#calculations .exclude-meal")?.getAttribute("aria-pressed") === was, before);
  check(true, `${name}: a meal is excluded and counted again`);
  const dayType = value => page.waitForFunction(wanted => {
    const select = document.querySelector("#calculations select.day-type");
    return select?.value === wanted && !select.disabled;
  }, value);
  await page.selectOption("#calculations select.day-type >> nth=0", "free");
  await dayType("free");
  await page.selectOption("#calculations select.day-type >> nth=0", "work");
  await dayType("work");
  check(true, `${name}: the day type changes in the list`);
  await page.selectOption("#summary-days", "7");
  await page.waitForSelector("#calculations tbody tr");
  check(true, `${name}: another period loads`);
  await page.click("[data-analysis-tab=automatik]");
  await page.waitForSelector("#automatik-explain .automatik-chips");
  check(page.url().includes("tab=automatik"), `${name}: Automatik tab in the URL`);
  await page.click("[data-analysis-tab=gerichte]");
  await page.waitForSelector("#dishes-list .hint, #dishes-list .dish-item");
  check(true, `${name}: Gerichte lists the dishes`);
  await page.click("[data-analysis-tab=werte]");
  await page.waitForSelector("#calculations tr:has(.exclude-meal) .open-meal-day");
  await page.click("#calculations tr:has(.exclude-meal) .open-meal-day >> nth=0");
  await page.waitForSelector("#timeline .history-meal");
  check(await page.isVisible("#diary-view"), `${name}: a calculation opens its day`);
  await review(page, name);
  const wide = await widerThanScreen(page);
  check(!wide.length, `${name}: the day review fits the screen ${wide.join(" | ")}`);
  await clickNav(page, "analysis");
  await page.waitForSelector("#calculations tbody tr");
}

// Settings (web/ui/settings.js): the model form saves and keeps the value, places can be created,
// renamed and deleted, a notification kind switches.
async function settings(page, name) {
  const target = await page.inputValue("[name=dose_target_mgdl]");
  await page.fill("[name=dose_target_mgdl]", "105");
  await page.click("#settings-form button[type=submit]");
  await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("Einstellungen gespeichert"));
  check(await page.inputValue("[name=dose_target_mgdl]") === "105", `${name}: a saved setting stays`);
  await page.fill("[name=dose_target_mgdl]", target);
  await page.click("#settings-form button[type=submit]");
  await page.waitForFunction(wanted => document.querySelector("[name=dose_target_mgdl]")?.value === wanted &&
    document.querySelector("#settings-version")?.textContent !== "", target);
  await page.fill("#place-new input[name=name]", "Testort");
  await page.click("#place-new button[type=submit]");
  await page.waitForSelector(".place-item .place-name");
  check(await page.inputValue(".place-item .place-name") === "Testort", `${name}: a place is created`);
  await page.fill(".place-item .place-name", "Testort 2");
  await page.press(".place-item .place-name", "Enter");
  await page.locator(".place-item .place-name").blur();
  await page.waitForFunction(() => document.querySelector(".place-item .hint")?.textContent.includes("Eintr"));
  page.once("dialog", dialog => dialog.accept());
  await page.click(".place-item .delete-treatment");
  await page.waitForFunction(() => !document.querySelector(".place-item"));
  check(true, `${name}: a place is deleted`);
  const toggle = ".notify-toggle[data-kind=check]";
  await page.waitForSelector(toggle + ":not([disabled])");
  const was = await page.isChecked(toggle);
  await page.click(toggle);
  await page.waitForFunction(([selector, before]) => {
    const input = document.querySelector(selector);
    return input.checked !== before && !input.disabled;
  }, [toggle, was]);
  await page.click(toggle);
  await page.waitForFunction(([selector, before]) => document.querySelector(selector).checked === before, [toggle, was]);
  check(true, `${name}: a notification kind switches`);
  const pause = ".notify-pause-input[data-kind=high]";
  check(await page.inputValue(pause) === "30", `${name}: high warning pause defaults to 30 min`);
  await page.fill(pause, "40");
  await page.press(pause, "Enter");
  await page.locator(pause).blur();
  await page.waitForFunction(selector => !document.querySelector(selector).disabled, pause);
  await page.reload();
  await page.waitForSelector(pause + ":not([disabled])");
  check(await page.inputValue(pause) === "40", `${name}: high warning pause is saved`);
  await page.fill(pause, "30");
  await page.locator(pause).blur();
  await page.waitForFunction(selector => !document.querySelector(selector).disabled, pause);
}

// "+" from Verlauf (bug 2026-11-24): the entry sheet shows there as well, and Verlauf stays the tab.
async function addFromHistory(page, name, compact) {
  for (const choice of ["meal", "manual"]) {
    await page.click("#nav-add");
    await page.waitForSelector("#add-dialog[open] .choice");
    await page.click(`.choice[data-choice=${choice}]`);
    if (await page.evaluate(() => document.querySelector("#agent-dialog").open)) await page.keyboard.press("Escape");
    check(await page.isVisible("#entry-drawer"), `${name}: entry form visible after "+" ${choice} from Verlauf`);
    check(await page.isVisible("#entry-form-host input[name=carbs]"), `${name}: entry fields visible from Verlauf (${choice})`);
    if (compact) {
      await page.click("#entry-close");
      check(!(await page.isVisible("#entry-drawer")), `${name}: entry sheet from Verlauf closes`);
      check(await page.isVisible(".review-panel"), `${name}: Verlauf still shown after closing the entry`);
    }
  }
}

// English (i18n 2026-11-26): an English browser gets the English page; the switch in the settings
// overrides the browser and "Automatisch" follows it again.
async function english(browser) {
  const context = await browser.newContext({ viewport: { width: 1360, height: 900 }, timezoneId: "Europe/Berlin", locale: "en-GB" });
  const page = await context.newPage();
  page.on("pageerror", error => check(false, `english: page error ${error.message}`));
  await page.goto(BASE + "/login");
  check(await page.textContent("h1") === "Log in", "english: login page in English");
  await page.goto(BASE + "/?view=settings");
  await page.waitForSelector("#language-select");
  check(await page.evaluate(() => document.documentElement.lang) === "en", "english: page language en");
  check(await page.textContent("[data-nav=today] span") === "Today", "english: navigation in English");
  check(await page.textContent(".settings-save button[type=submit]") === "Save settings", "english: settings in English");
  check(!(await page.textContent("body")).includes("Einstellungen speichern"), "english: no German settings text");
  await Promise.all([page.waitForNavigation(), page.selectOption("#language-select", "de")]);
  await page.waitForSelector("#language-select");
  check(await page.textContent("[data-nav=today] span") === "Heute", "english: switch to Deutsch overrides the browser");
  await Promise.all([page.waitForNavigation(), page.selectOption("#language-select", "")]);
  await page.waitForSelector("#language-select");
  check(await page.textContent("[data-nav=today] span") === "Today", "english: automatic follows the browser again");
  await page.goto(BASE + "/?view=diary");
  await page.waitForSelector("#record-count:not(:empty)", { state: "attached" });
  await page.click("#nav-add");
  await page.waitForSelector("#add-dialog[open] .choice");
  check(await page.textContent("#add-dialog h2") === "Add entry", "english: \"+\" in English");
  await context.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    await run(browser, "phone", devices["iPhone 13"]);
    await run(browser, "desktop", { viewport: { width: 1360, height: 900 } });
    await english(browser);
  } finally {
    await browser.close();
  }
  if (failures.length) { console.log(failures.length + " failed"); process.exit(1); }
  console.log("all passed");
})();
