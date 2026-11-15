// E564: after a redeploy the hashed chunk files of the OLD build are gone. A tab (or a cached entry script) from before the deploy then asks for
// e.g. /assets/FnoTab-<old hash>.js, gets a 404 and shows "Failed to fetch dynamically imported module". React.lazy caches that failed import, so
// the boundary's "Try again" could never recover. The fix is to load the new index.html once, which points at the files that exist now.

/** True for the messages browsers give when a lazy chunk cannot be downloaded (Chrome/Edge, Firefox, Safari, Vite's preload helper). */
export function isChunkLoadError(err: unknown): boolean {
  const msg = String((err as { message?: unknown } | null | undefined)?.message ?? err ?? "");
  return /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS|ChunkLoadError|Loading chunk [\w-]+ failed/i.test(msg);
}

const KEY = "stockky:chunk_reload_at";
const WINDOW_MS = 60_000;

/** Reloads at most once per minute (so a really missing file cannot cause a reload loop). Returns true when it reloaded. */
export function reloadOnceForNewVersion(
  store: Pick<Storage, "getItem" | "setItem"> | null | undefined = (() => { try { return window.sessionStorage; } catch { return null; } })(),
  now: number = Date.now(),
  reload: () => void = () => window.location.reload(),
): boolean {
  try {
    const last = Number(store?.getItem(KEY) || 0);
    if (last && now - last < WINDOW_MS) return false;
    store?.setItem(KEY, String(now));
  } catch { /* storage blocked: still reload once, the 404 is certain */ }
  reload();
  return true;
}
