"""selfcheck.py - run ON THE VM, inside the container:  docker compose --profile timesfm exec timesfm-forecast-service python selfcheck.py
Builds the configured backend (TSFM_BACKEND), forecasts a synthetic 300-bar series for 5 steps and prints the fan and the derived parameters.
This is the first real test of the model adapter (the build sandbox could not download weights). Exit 0 = the backend returned a usable fan."""
import math
import sys

import backends
import tsfm_features as tf


def main() -> int:
    series = [100 + 5 * math.sin(i / 9.0) + i * 0.02 for i in range(300)]
    vols = [1_000_000 + 100_000 * math.sin(i / 5.0) for i in range(300)]
    try:
        b = backends.get_backend()
        fans = b.forecast([series, vols], 5)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1
    d = tf.derive(series[-1], fans[0], trailing_vol_pct=tf.trailing_stats([{"close": c, "volume": v} for c, v in zip(series, vols)])["trailing_vol_pct"],
                  stop_pct=3.0, target_pct=6.0, horizon=5, pace=tf.volume_pace(fans[1], sum(vols[-20:]) / 20, 5))
    print("backend:", getattr(b, "name", "?"), "checkpoint:", getattr(b, "checkpoint", None))
    print("q10/q50/q90 at step 5:", [round(fans[0]["quantiles"][k][-1], 3) for k in ("0.1", "0.5", "0.9")])
    print("derived:", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
