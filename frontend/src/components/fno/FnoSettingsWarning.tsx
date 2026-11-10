// Round 291: the "settings that cannot work together" warning (round 290) on Real Money Calibration > F&O too, where the times are changed.
// Shows nothing while the list is empty or the service cannot be reached (the Switches panel shows its own errors). Reads the running values only.
import { fnoApi } from "../../fnoApi";
import { settingsProblemText } from "../../fnoView";
import { Msg } from "../calibration/ui";
import { useFno } from "./useFno";

export default function FnoSettingsWarning({ everyMs = 10000 }: { everyMs?: number }) {
  const ov = useFno(fnoApi.overview, everyMs);
  const text = settingsProblemText(ov.data?.param_problems);
  if (!text) return null;
  return <div data-testid="fno-settings-problem"><Msg kind="err">{text}</Msg></div>;
}
