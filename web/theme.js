// Light or dark look (2026-11-26). Loaded as a plain script in <head>, before the stylesheet paints, so a
// chosen theme never flashes. "system" (default) follows the device; "light"/"dark" are kept per device
// in localStorage and set as data-theme on <html>, which styles.css reads (color-scheme + light-dark()).
(function () {
  var choice = "system";
  try { choice = localStorage.getItem("theme") || "system"; } catch (error) { /* storage blocked: follow the system */ }
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
})();
