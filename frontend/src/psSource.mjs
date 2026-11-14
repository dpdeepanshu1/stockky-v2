// E538 (redesign round 5b): PositionStocksTab.tsx was split into the page shell plus components/positionStocks/*.
// Source-level guard tests that used to read the one big file read this joined text instead (same strings, now spread over the files).
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
export const PS_DIR = join(here, "components/positionStocks");
export function psFiles() {
  return ["components/PositionStocksTab.tsx", ...readdirSync(PS_DIR).sort().map((f) => `components/positionStocks/${f}`)];
}
export function psSource() {
  return psFiles().map((f) => readFileSync(join(here, f), "utf8")).join("\n");
}
