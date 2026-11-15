// round 553 (E578, Tredye plan P4): the global cues strip - S&P 500, Nasdaq, Brent crude, dollar index, USD/INR, gold. READ ONLY and DISPLAY ONLY: it reads the
// gateway's /market/global-cues and draws six tiles. It feeds no trading decision, no switch and no Market Weather dial (the dials stay set only by the runner's
// own overlay). Words and rules live in globalCuesView.ts (globalCues.test.mjs): a missing or old quote is a dash, every number says which bar it is from.
import { useEffect, useState } from "react";
import { Card, CardHeader } from "./kit/Card";
import { Stat, Stats } from "./kit/Stat";
import { Button } from "./kit/Button";
import { StatusPill } from "./kit/StatusPill";
import { SkeletonLines } from "./kit/Skeleton";
import { setVisibleInterval } from "../visibleInterval";
import { api } from "../api";
import { globalCueTiles, stripSub, type GlobalCuesBody } from "../globalCuesView";

export default function GlobalCuesStrip() {
  const [data, setData] = useState<GlobalCuesBody | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = async (manual = false) => {
    if (manual) setBusy(true);
    try {
      setData(await api.globalCues());          // only a good answer replaces what is on screen
      setFailed(false);
    } catch {
      setFailed(true);                            // the last good read stays, but the line under the title says the refresh failed
    } finally {
      setLoading(false);
      setBusy(false);
    }
  };

  useEffect(() => {
    void load();
    return setVisibleInterval(() => load(), 300000);   // 5 minutes, paused while the tab is hidden; the gateway side caches the same time
  }, []);

  if (loading && !data) {
    return (
      <Card role="status" aria-label="Loading global cues">
        <SkeletonLines rows={2} />
      </Card>
    );
  }

  const tiles = globalCueTiles(data);
  const behind = failed || (data != null && (data.error != null || data.enabled === false));
  return (
    <Card aria-label="Global cues" data-testid="global-cues">
      <CardHeader
        title="Global cues"
        sub={stripSub(data, failed)}
        action={
          <>
            {behind && <StatusPill state="paused" title="Some or all of this strip could not be refreshed">Delayed</StatusPill>}
            <Button variant="ghost" size="sm" onClick={() => void load(true)} loading={busy} aria-label="Refresh global cues">Refresh</Button>
          </>
        }
      />
      <Stats>
        {tiles.map((t) => <Stat key={t.key} label={t.label} value={t.value} tone={t.tone} delta={t.delta} sub={t.sub} size="sm" />)}
      </Stats>
      <p className="hm-note">For reading only. Yahoo daily bars can lag, and nothing here is used by a trading decision.</p>
    </Card>
  );
}
