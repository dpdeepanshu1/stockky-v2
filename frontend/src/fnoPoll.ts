// Round 339 (E341): ordering rules for the F&O polling hook (components/fno/useFno.ts), kept pure so they can be tested without React.
//
// Why: useFno re-runs when its inputs change (Nifty -> Sensex, another expiry, another mode / day range / file). A request sent for the OLD inputs can
// still be in flight; when it landed late it was shown under the NEW inputs (a Nifty chain under the Sensex tab, DEMO trades under REAL). Two polls can
// also finish out of order, so an older answer could replace a newer one. A gate answers one question per finished request: may this result be shown?
export interface PollGate {
  /** Call when a request is sent. Returns a ticket that is passed to `accept` when the request finishes. */
  start(): { epoch: number; seq: number };
  /** True only if the request belongs to the current inputs AND no newer request has already been shown. */
  accept(t: { epoch: number; seq: number }): boolean;
  /** Call when the inputs change or the hook unmounts: every request still in flight is dropped. */
  reset(): void;
}

export function createPollGate(): PollGate {
  let epoch = 0;
  let seq = 0;
  let shown = 0;
  return {
    start: () => ({ epoch, seq: ++seq }),
    accept: (t) => {
      if (t.epoch !== epoch) return false;   // sent for other inputs (or before an unmount)
      if (t.seq < shown) return false;       // an older poll finishing after a newer one was shown
      shown = t.seq;
      return true;
    },
    reset: () => { epoch += 1; },
  };
}

/** Same inputs as before? (the hook clears what it shows only when the inputs really changed, not on every poll or interval change) */
export function sameInputs(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
}
