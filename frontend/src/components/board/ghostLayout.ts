// group324: placeholder tiles for the Market Board when nothing is ranked (market closed before any session was logged, or the first
// minutes after the open). Deterministic pseudo-random sizes (seeded), NEVER data: the UI labels them as a placeholder.
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function ghostItems(n: number, seed: number): { id: string; value: number }[] {
  const r = mulberry32(seed);
  return Array.from({ length: Math.max(0, n) }, (_, i) => ({ id: `g${i}`, value: 0.4 + Math.pow(r(), 2.2) * 6 }));
}
