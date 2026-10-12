// group306/324: one <canvas> under the tiles. Every stock is a curve that leaves its sector's row in the rail, bundles into a thin glowing
// bus and fans out into its tile (order-block.com's look). The curves draw in ONCE per mount; later data refreshes redraw instantly
// (no replay every 2 s poll). While the market is live, pulses travel along the strongest curves. The static curves are cached on an
// offscreen canvas, so a pulse frame is one drawImage + a few dots. 30 fps, paused while the tab is hidden, capped at 250 curves /
// 40 pulses, and under prefers-reduced-motion everything is drawn once and still.
import { useEffect, useMemo, useRef } from "react";
import type { BoardRow } from "./boardTypes";
import type { TmRect } from "./treemapLayout";
import { busX, busY, flowPath, pointAt, type Pt } from "./flowGeometry";

export interface FlowItem { row: BoardRow; rect: TmRect; srcY: number }
const MAX_LINES = 250, MAX_PULSES = 40;

interface Line { path: Pt[]; col: string; strong: boolean; phase: number; speed: number }

export default function FlowLines({ items, width, height, lane, live, reduced }:
  { items: FlowItem[]; width: number; height: number; lane: number; live: boolean; reduced: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const itemsRef = useRef(items); itemsRef.current = items;
  const drawnOnce = useRef(false);
  // the effect restarts only when the picture would really change (rounded geometry), not on every poll
  const sig = useMemo(() => items.slice(0, MAX_LINES).map(i =>
    `${i.row.symbol}${i.row.side[0]}${i.row.rank <= 10 ? "!" : ""}:${Math.round(i.rect.x)},${Math.round(i.rect.y)},${Math.round(i.rect.h)}:${Math.round(i.srcY)}`).join("|"), [items]);

  useEffect(() => {
    const cv = ref.current;
    if (!cv || width <= 0 || height <= 0 || lane <= 0) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(width * dpr); cv.height = Math.round(height * dpr);
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const root = cv.closest(".ob") ?? document.documentElement;
    const css = getComputedStyle(root);
    const buy = css.getPropertyValue("--ob-g").trim() || "#1fe08f", sell = css.getPropertyValue("--ob-r").trim() || "#ff3d5a";
    const bx = busX(lane);
    const lines: Line[] = itemsRef.current.slice(0, MAX_LINES).map(({ row, rect, srcY }, i) => {
      const bus = { x: bx, y: busY(srcY, height) };
      const sp = row.spark || [];
      const lo = sp.length ? Math.min(...sp) : 0, hi = sp.length ? Math.max(...sp) : 1, span = hi - lo || 1;
      const path = flowPath({ x: 0, y: srcY }, bus, { x: rect.x + 6, y: rect.y + rect.h / 2 }, 30);
      if (sp.length > 1) path.forEach((p, k) => {            // the day's price path wiggles the first (rail -> bus) half
        const t = k / (path.length - 1);
        if (t < 0.5) p.y += ((sp[Math.min(sp.length - 1, Math.floor(t * 2 * (sp.length - 1)))] - lo) / span - 0.5) * 10 * Math.sin(t * 2 * Math.PI);
      });
      return { path, col: row.side === "bull" ? buy : sell, strong: row.rank <= 10, phase: (i * 0.37) % 1, speed: 0.00018 + (row.rank % 7) * 0.00002 };
    });
    const drawLines = (c: CanvasRenderingContext2D, progress: number) => {
      c.clearRect(0, 0, width, height);
      for (const l of lines) {
        const n = Math.max(2, Math.floor(l.path.length * progress));
        c.strokeStyle = l.col; c.lineWidth = l.strong ? 1.1 : 0.65; c.globalAlpha = l.strong ? 0.5 : 0.17;
        c.beginPath(); l.path.slice(0, n).forEach((p, k) => (k ? c.lineTo(p.x, p.y) : c.moveTo(p.x, p.y))); c.stroke();
      }
      if (progress >= 1 && lines.length) {                        // the bus: a thin glowing spine where the curves bundle
        const ys = lines.map(l => l.path[Math.floor(l.path.length / 2)].y), y0 = Math.min(...ys) - 6, y1 = Math.max(...ys) + 6;
        const g = c.createLinearGradient(0, y0, 0, y1); g.addColorStop(0, buy); g.addColorStop(0.5, "rgba(255,255,255,.7)"); g.addColorStop(1, sell);
        c.globalAlpha = 0.85; c.strokeStyle = g; c.lineWidth = 2; c.shadowColor = "rgba(255,255,255,.35)"; c.shadowBlur = 8;
        c.beginPath(); c.moveTo(bx, y0); c.lineTo(bx, y1); c.stroke(); c.shadowBlur = 0;
      }
      c.globalAlpha = 1;
    };
    const pulses = lines.filter(l => l.strong).slice(0, MAX_PULSES);
    let raf = 0, lastT = 0, progress = reduced || drawnOnce.current ? 1 : 0;
    let cache: HTMLCanvasElement | null = null;
    const drawPulses = (t: number) => {
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height);
      if (cache) ctx.drawImage(cache, 0, 0);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      for (const l of pulses) {
        const p = pointAt(l.path, (l.phase + t * l.speed) % 1);
        ctx.globalAlpha = 0.18; ctx.fillStyle = l.col; ctx.beginPath(); ctx.arc(p.x, p.y, 3.4, 0, 6.2832); ctx.fill();
        ctx.globalAlpha = 0.95; ctx.beginPath(); ctx.arc(p.x, p.y, 1.4, 0, 6.2832); ctx.fill();
      }
      ctx.globalAlpha = 1;
    };
    const bake = () => {
      cache = document.createElement("canvas"); cache.width = cv.width; cache.height = cv.height;
      const cc = cache.getContext("2d"); if (!cc) { cache = null; return; }
      cc.setTransform(dpr, 0, 0, dpr, 0, 0); drawLines(cc, 1);
    };
    const step = (t: number) => {
      raf = requestAnimationFrame(step);
      if (document.visibilityState === "hidden" || t - lastT < 33) return;
      lastT = t;
      if (progress < 1) {
        progress = Math.min(1, progress + 0.07);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0); drawLines(ctx, progress);
        if (progress >= 1) { drawnOnce.current = true; if (live) bake(); else cancelAnimationFrame(raf); }
      } else if (live) drawPulses(t);
    };
    if (reduced) { ctx.setTransform(dpr, 0, 0, dpr, 0, 0); drawLines(ctx, 1); drawnOnce.current = true; }
    else if (progress >= 1) {                                       // a refresh after the first draw-in: paint the final picture at once
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); drawLines(ctx, 1);
      if (live) { bake(); raf = requestAnimationFrame(step); }
    } else raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [sig, width, height, lane, live, reduced]);
  if (lane <= 0) return null;
  return <canvas ref={ref} aria-hidden="true" style={{ width, height }} />;
}
