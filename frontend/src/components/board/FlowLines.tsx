// group306: one <canvas> of thin curves, one per stock (its day path from the sector rail edge into its tile), drawn with a
// draw-in animation. Capped at 250 lines, 30 fps, paused while the tab is hidden, off under prefers-reduced-motion (drawn once).
import { useEffect, useRef } from "react";
import type { BoardRow } from "./boardTypes";
import type { TmRect } from "./treemapLayout";

export interface FlowItem { row: BoardRow; rect: TmRect }
const MAX_LINES = 250;

export default function FlowLines({ items, width, height, reduced }: { items: FlowItem[]; width: number; height: number; reduced: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current;
    if (!cv || width <= 0 || height <= 0) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = width * dpr; cv.height = height * dpr;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(document.documentElement);
    const buy = css.getPropertyValue("--buy").trim() || "#0ecb81", sell = css.getPropertyValue("--sell").trim() || "#f6465d";
    const lines = items.slice(0, MAX_LINES).map(({ row, rect }) => {
      const sp = row.spark || [];
      const lo = Math.min(...sp), hi = Math.max(...sp), span = hi - lo || 1;
      const x0 = 0, y0 = height / 2, x1 = rect.x + rect.w / 2, y1 = rect.y + rect.h / 2;
      const pts = sp.map((v, i) => {                 // the path fans out around the straight line by its normalised price move
        const t = sp.length > 1 ? i / (sp.length - 1) : 1;
        const off = ((v - lo) / span - 0.5) * 18 * (1 - t);
        return [x0 + (x1 - x0) * t, y0 + (y1 - y0) * t + off] as [number, number];
      });
      return { pts, col: row.side === "bull" ? buy : sell };
    });
    let progress = reduced ? 1 : 0, raf = 0, lastT = 0;
    const draw = () => {
      ctx.clearRect(0, 0, width, height);
      ctx.lineWidth = 0.7; ctx.globalAlpha = 0.22;
      for (const l of lines) {
        const n = Math.max(2, Math.floor(l.pts.length * progress));
        ctx.strokeStyle = l.col; ctx.beginPath();
        l.pts.slice(0, n).forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
      }
    };
    const step = (t: number) => {
      if (document.visibilityState === "hidden") { raf = requestAnimationFrame(step); return; }
      if (t - lastT >= 33) { lastT = t; progress = Math.min(1, progress + 0.06); draw(); }
      if (progress < 1) raf = requestAnimationFrame(step);
    };
    if (reduced) draw(); else raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [items, width, height, reduced]);
  return <canvas ref={ref} aria-hidden="true" style={{ position: "absolute", inset: 0, width, height, pointerEvents: "none" }} />;
}
