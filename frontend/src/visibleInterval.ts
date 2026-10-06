// group 184: data polls that stop while the browser tab is hidden.
//
// A browser tab in the background kept running every poll in the app (about 10 endpoints per open tab, all of them
// hitting the gateway and, through it, the rate-limited market-data and broker calls) although nobody could see the
// result. setVisibleInterval() is a drop-in for setInterval for those polls:
//   - while document.visibilityState is "hidden" a tick does nothing (no request);
//   - when the tab becomes visible again and the last run is at least one period old, it runs once at once, so the
//     screen is current the moment you look at it instead of up to one period later.
// While the tab is visible the cadence is exactly what it was. Returns the cleanup function.
// Not for clocks (no network) or for job-progress polls that must keep going until the job ends.

export function setVisibleInterval(fn: () => unknown, ms: number): () => void {
  const hasDoc = typeof document !== "undefined";
  let lastRun = Date.now();
  const run = () => {
    lastRun = Date.now();
    try {
      void fn();
    } catch {
      /* a failing poll must not stop the timer */
    }
  };
  const id = setInterval(() => {
    if (hasDoc && document.visibilityState === "hidden") return;
    run();
  }, ms);
  const onVisible = () => {
    if (document.visibilityState === "visible" && Date.now() - lastRun >= ms) run();
  };
  if (hasDoc) document.addEventListener("visibilitychange", onVisible);
  return () => {
    clearInterval(id);
    if (hasDoc) document.removeEventListener("visibilitychange", onVisible);
  };
}
