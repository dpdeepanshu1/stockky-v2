// frontend/src/components/ui/AiOrb.tsx  (design v5 - the AI animation; styles live in design-v5.css)
// Pure CSS, decorative (aria-hidden): rings + orbiting dots around a glowing core. transform / opacity only.
// "idle" = slow breathing, "thinking" = rings spin faster while a scan runs, "done" = calm green glow.
// Switched off by reduced-motion and TV mode, paused while the tab is hidden (see design-v5.css).
export type AiOrbState = "idle" | "thinking" | "done";

export function AiOrb({ state = "idle", size = 72 }: { state?: AiOrbState; size?: number }) {
  return (
    <span className={`v5-orb ${state}`} style={{ width: size, height: size }} aria-hidden="true">
      <span className="v5-orb-glow" />
      <span className="v5-orb-ring r1"><i /></span>
      <span className="v5-orb-ring r2"><i /></span>
      <span className="v5-orb-ring r3"><i /></span>
      <span className="v5-orb-core" />
    </span>
  );
}

/** A tiny three-bar "AI is typing" indicator for inline status lines. */
export function AiTyping() {
  return <span className="v5-typing" aria-hidden="true"><i /><i /><i /></span>;
}
