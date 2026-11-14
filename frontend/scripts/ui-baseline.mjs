// E513 round 0: screenshot every tab, light + dark, phone + desktop, into ./ui-baseline/<label>/.
// Run on the VM, with the frontend already serving:   cd frontend && npm run build && npm run preview &
//   node scripts/ui-baseline.mjs http://localhost:4173 before     (then again with "after" next round, and compare the two folders)
// Needs Playwright (npx playwright install chromium). Pages that need the admin login show their login card: that is the baseline for them too.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
const [url = "http://localhost:4173", label = "before"] = process.argv.slice(2);
const TABS = ["Dashboard", "Auto Trade", "Position Stocks", "F&O Options", "Real Money Calibration", "Paper Trades", "Hot Picks", "Surprise Momentum", "IPO Tracker", "Watchlist", "Alerts", "Data Feed", "Training Lab", "Settings"];
const SIZES = { phone: { width: 390, height: 844 }, desktop: { width: 1280, height: 800 } };
const browser = await chromium.launch();
for (const theme of ["light", "dark"]) for (const [sz, viewport] of Object.entries(SIZES)) {
  const dir = `ui-baseline/${label}/${theme}-${sz}`; mkdirSync(dir, { recursive: true });
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  await ctx.addInitScript((t) => { try { localStorage.setItem("stockky_theme", t); } catch {} }, theme);
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: "networkidle" }).catch(() => {});
  for (const tab of TABS) {
    const link = sz === "phone" ? null : page.getByRole("button", { name: tab, exact: true }).first();
    if (link && (await link.count())) await link.click().catch(() => {});
    else if (sz === "phone") {
      await page.getByRole("button", { name: "Open menu" }).click().catch(() => {});
      await page.getByRole("button", { name: tab, exact: true }).first().click().catch(() => {});
    }
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${dir}/${tab.replace(/[^a-z0-9]+/gi, "_")}.png`, fullPage: false });
  }
  await ctx.close();
}
await browser.close();
console.log("done: ui-baseline/" + label);
