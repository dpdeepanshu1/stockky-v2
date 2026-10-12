// group324: pure geometry for the Market Board flow lanes (no DOM). Every stock gets a curve that starts at its sector's row in the
// sector rail, bundles into a thin vertical "bus" and fans out again into the stock's tile - the look of order-block.com's board.
// Tested in flowGeometry.test.mjs.
export interface Pt { x: number; y: number }

/** Width of the curve lane left of the tiles; 0 on a narrow (phone) layout where the lane would eat the tiles. */
export function laneWidth(width: number, narrow: boolean): number {
  if (narrow || !(width > 0)) return 0;
  return Math.round(Math.min(220, Math.max(90, width * 0.2)));
}

/** Width of the sector rail column (matches `.ob-body.wide` in board.css). */
export const RAIL_W = 176;
/** Narrow (phone) layout, decided from the WHOLE board's width. It must not be decided from the stage width: showing the rail shrinks the
 *  stage, which would flip the layout back and forth for boards between ~520 and ~700 px wide. */
export function isNarrow(boardWidth: number): boolean { return boardWidth > 0 && boardWidth < RAIL_W + 520; }

/** x of the bus inside the lane. */
export function busX(lane: number): number { return Math.round(lane * 0.55); }

/** Row height shared by the sector rail and the curve origins, so a curve leaves from the row it belongs to. */
export function railRowHeight(count: number, height: number, natural = 26, top = 6): number {
  if (!(count > 0) || !(height > 0)) return natural;
  const fit = (height - top - 6) / (count + 1);       // +1 = the "All sectors" row
  return Math.max(14, Math.min(natural, fit));
}

/** y (in stage coordinates) of the centre of sector `index`'s rail row; index -1 (unknown sector) = the middle of the stage. */
export function sectorY(index: number, count: number, height: number, top = 6): number {
  if (index < 0 || index >= count) return height / 2;
  const rh = railRowHeight(count, height, 26, top);
  // row 0 of the rail is "All sectors"; with very many sectors the rail scrolls, so the origin is clamped into the stage
  return Math.min(height - 6, top + rh * (index + 1) + rh / 2);
}

export function cubic(p0: Pt, p1: Pt, p2: Pt, p3: Pt, t: number): Pt {
  const u = 1 - t, a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
  return { x: a * p0.x + b * p1.x + c * p2.x + d * p3.x, y: a * p0.y + b * p1.y + c * p2.y + d * p3.y };
}

/** Two cubic segments joined at the bus with horizontal tangents (src -> bus -> dst), sampled into `steps` + 1 points. */
export function flowPath(src: Pt, bus: Pt, dst: Pt, steps = 28): Pt[] {
  const half = Math.max(2, Math.floor(steps / 2));
  const dx1 = bus.x - src.x, dx2 = dst.x - bus.x;
  const a1 = { x: src.x + dx1 * 0.6, y: src.y }, a2 = { x: bus.x - dx1 * 0.4, y: bus.y };
  const b1 = { x: bus.x + dx2 * 0.4, y: bus.y }, b2 = { x: dst.x - dx2 * 0.6, y: dst.y };
  const out: Pt[] = [];
  for (let i = 0; i <= half; i++) out.push(cubic(src, a1, a2, bus, i / half));
  for (let i = 1; i <= half; i++) out.push(cubic(bus, b1, b2, dst, i / half));
  return out;
}

/** Where the curve meets the bus: pulled toward the stage centre so the curves bundle into one thin spine. */
export function busY(srcY: number, height: number): number { return height / 2 + (srcY - height / 2) * 0.12; }

/** Point at fraction u (0..1) along a sampled path, interpolated between samples. */
export function pointAt(path: Pt[], u: number): Pt {
  if (!path.length) return { x: 0, y: 0 };
  const f = Math.min(1, Math.max(0, u)) * (path.length - 1), i = Math.floor(f), k = f - i;
  const a = path[i], b = path[Math.min(path.length - 1, i + 1)];
  return { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k };
}
