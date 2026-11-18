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
  await page.waitForFunction(() => typeof state !== "undefined" && state.dayData);
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
  }
  await page.click("[data-nav=today]");
  const active = await page.$$eval(".appnav-item.active", items => items.map(item => item.dataset.nav));
  check(active.join() === "today", `${name}: Heute active`);
  await context.close();
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
