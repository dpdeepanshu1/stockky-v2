// round 542 (E567, S2): the AI agent card on Real Money Calibration > Auto-calibration is now a SHORT LINK to the Self Improve System tab, where the loop, the
// ideas, the scoreboard and the Run-now buttons live (no duplicate controls). It still reads GET /agent/status for ONE line of state. It sets nothing and
// shows no key. The click asks the shell to open the tab through the "stockky:goto-tab" event (App.tsx).
import { useEffect, useState } from "react";
import { runnerApi } from "../../calibrationApi";
import { headline, scoreLine, type AgentStatus } from "../../agentView";
import { Btn, Card, Chip } from "./ui";

export const GOTO_TAB_EVENT = "stockky:goto-tab";

export default function AgentCard() {
  const [st, setSt] = useState<AgentStatus | null>(null);
  useEffect(() => { let on = true; runnerApi.agentStatus().then((s) => { if (on) setSt(s); }).catch(() => { /* the link works without the status line */ }); return () => { on = false; }; }, []);
  const h = st ? headline(st) : null;
  return (
    <div data-testid="agent-card">
      <Card title="AI agent (idea source)" tone={h?.tone}
        right={<Btn small primary onClick={() => window.dispatchEvent(new CustomEvent(GOTO_TAB_EVENT, { detail: { tab: "selfimprove" } }))}>Open Self Improve System</Btn>}>
        <p className="text-mist">The nightly AI agent, its ideas, the scoreboard and the Run-now buttons moved to <b>Trading &gt; Self Improve System</b>. It only suggests: it cannot change a switch.</p>
        {st && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Chip tone={h?.tone}>{h?.text}</Chip>
            <Chip>{scoreLine(st.scoreboard)}</Chip>
          </div>
        )}
      </Card>
    </div>
  );
}
