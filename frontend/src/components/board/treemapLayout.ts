// group306: squarified treemap (Bruls et al.). Pure: no DOM. Area of each rect is proportional to its value; rects tile the box
// exactly (no overlap, no gap) - see treemapLayout.test.mjs.
export interface TmItem { id: string; value: number }
export interface TmRect { id: string; x: number; y: number; w: number; h: number }

function worst(row: number[], side: number): number {
  const s = row.reduce((a, b) => a + b, 0);
  const mx = Math.max(...row), mn = Math.min(...row);
  return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
}

export function squarify(items: TmItem[], x: number, y: number, w: number, h: number): TmRect[] {
  const pool = items.filter(i => Number.isFinite(i.value) && i.value > 0).sort((a, b) => b.value - a.value);
  const out: TmRect[] = [];
  if (!pool.length || w <= 0 || h <= 0) return out;
  const total = pool.reduce((a, b) => a + b.value, 0);
  const scale = (w * h) / total;
  const areas = pool.map(i => i.value * scale);
  let cx = x, cy = y, cw = w, ch = h, i = 0;
  while (i < pool.length) {
    const side = Math.min(cw, ch);
    let row = [areas[i]];
    let j = i + 1;
    while (j < pool.length && worst([...row, areas[j]], side) <= worst(row, side)) { row.push(areas[j]); j++; }
    const rs = row.reduce((a, b) => a + b, 0);
    if (cw >= ch) {                       // lay the row as a column on the left
      const rw = rs / ch; let yy = cy;
      row.forEach((a, k) => { const hh = a / rw; out.push({ id: pool[i + k].id, x: cx, y: yy, w: rw, h: hh }); yy += hh; });
      cx += rw; cw -= rw;
    } else {                              // lay the row along the top
      const rh = rs / cw; let xx = cx;
      row.forEach((a, k) => { const ww = a / rh; out.push({ id: pool[i + k].id, x: xx, y: cy, w: ww, h: rh }); xx += ww; });
      cy += rh; ch -= rh;
    }
    i = j;
  }
  return out;
}
