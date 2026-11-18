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

  for (const choice of ["meal", "correction", "carbs", "exercise", "basal"]) {
    await page.click("#nav-add");
    await page.waitForSelector("#add-dialog[open] .choice");
    check((await page.$$(".choice")).length === 5, `${name}: "+" offers 5 choices`);
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

  await page.click("[data-nav=history]");
  check(await page.isVisible(".review-panel"), `${name}: Verlauf shows the day`);
  const card = await page.$(".edit-entry:visible");
  check(Boolean(card), `${name}: a saved entry has a card button`);
  if (card) {
    await card.click();
    await page.waitForSelector("#edit-dialog[open] .entry-sheet");
    check(true, `${name}: entry card opens`);
    await page.keyboard.press("Escape");
  }

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
    await page.click("#settings-back");
    check(await page.isVisible("#settings-index"), `${name}: back to the settings list`);
  } else {
    check(await page.isVisible("[name=dose_target_mgdl]"), `${name}: all settings on one page`);
    await settings(page, name);
  }
  await page.click("[data-nav=today]");
  const active = await page.$$eval(".appnav-item.active", items => items.map(item => item.dataset.nav));
  check(active.join() === "today", `${name}: Heute active`);
  await context.close();
}

const toastSays = (page, text) => page.waitForFunction(wanted => document.querySelector("#toast")?.textContent.includes(wanted), text);

// "Tag durchgehen" (web/ui/day.js): the day chart and effects, a meal excluded and counted again, and the
// window editor: dragging, nudging, saving and going back to the standard window.
async function review(page, name) {
  await page.waitForSelector("#timeline .day-meal");
  // Wide screens show the day in the chart above the form; phones draw it in the review.
  await page.waitForSelector(name === "phone" ? "#review-chart svg" : "#day-overview-chart svg");
  check(true, `${name}: the day chart is drawn`);
  await page.waitForSelector("#effects-chart svg, #effects-chart .hint");
  check(true, `${name}: Wirkungen load`);
  const exclude = "#timeline .exclude-meal";
  const before = await page.$eval(exclude, button => button.getAttribute("aria-pressed"));
  await page.click(exclude + " >> nth=0");
  await page.waitForFunction(([selector, was]) => document.querySelector(selector)?.getAttribute("aria-pressed") !== was, [exclude, before]);
  await page.click(exclude + " >> nth=0");
  await page.waitForFunction(([selector, was]) => document.querySelector(selector)?.getAttribute("aria-pressed") === was, [exclude, before]);
  check(true, `${name}: a meal in the day is excluded and counted again`);

  const details = page.locator("#timeline .day-meal-details").first();
  if (!(await details.evaluate(node => node.open))) await details.locator("summary").click();
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
  await page.waitForSelector("#timeline .day-meal");
  check(await page.isVisible("#diary-view"), `${name}: a calculation opens its day`);
  await review(page, name);
  await page.click("[data-nav=analysis]");
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
}

(async () => {
  const browser = await chromium.launch();
  try {
    await run(browser, "phone", devices["iPhone 13"]);
    await run(browser, "desktop", { viewport: { width: 1360, height: 900 } });
  } finally {
    await browser.close();
  }
  if (failures.length) { console.log(failures.length + " failed"); process.exit(1); }
  console.log("all passed");
})();
