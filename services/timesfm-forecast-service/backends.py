"""backends.py - interchangeable forecast backends for the TimesFM service (group 434).

A backend answers one question: forecast(series_list, horizon) -> one fan per series, where a fan is
  {"mean": [h], "quantiles": {"0.1": [h], "0.5": [h], "0.9": [h], ...}}
Selected by TSFM_BACKEND (default timesfm25):
  timesfm25     local Apache-2.0 weights (google/timesfm-2.5-200m-pytorch). The production-safe local route.
  bigquery      BigQuery AI.FORECAST (production use is allowed through Google Cloud). Price data leaves the box: your call.
  research-3.0  local TimesFM 3.0 weights, whose licence bars production use. REFUSES to build when TSFM_TRADE_MODE is REAL.
  fake          deterministic stand-in for tests and wiring checks. It has no skill: never read its numbers as a forecast.
Model libraries are imported late, so /health and the pure tests never need torch. Nothing here has run against real weights in the build
sandbox (no download access); `python selfcheck.py` on the VM is the first real test of each backend.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence

LEVELS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
Fan = Dict[str, Any]


class BackendError(RuntimeError):
    pass


class BackendRefused(BackendError):
    """The backend must not run in this trade mode (licence)."""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


class FakeBackend:
    """Persistence forecast with a fan that widens with sqrt(step) from the series' own recent volatility. No skill; deterministic."""
    name = "fake"
    checkpoint = "none"

    def forecast(self, series_list: Sequence[Sequence[float]], horizon: int) -> List[Fan]:
        out: List[Fan] = []
        for s in series_list:
            vals = [float(x) for x in s]
            if len(vals) < 3:
                raise BackendError("series too short")
            last = vals[-1]
            diffs = [abs(vals[i] - vals[i - 1]) for i in range(max(1, len(vals) - 20), len(vals))]
            step = max(sum(diffs) / len(diffs), abs(last) * 1e-6)
            z = {0.1: -1.2816, 0.2: -0.8416, 0.3: -0.5244, 0.4: -0.2533, 0.5: 0.0, 0.6: 0.2533, 0.7: 0.5244, 0.8: 0.8416, 0.9: 1.2816}
            qs = {str(l): [max(0.0, last + z[l] * step * math.sqrt(i + 1)) for i in range(horizon)] for l in LEVELS}
            out.append({"mean": [last] * horizon, "quantiles": qs})
        return out


class TimesFM25Backend:
    """Local TimesFM 2.5 (Apache-2.0 weights). Written from the upstream README (TimesFM_2p5_200M_torch, ForecastConfig, forecast(horizon, inputs)
    returning (point, quantiles) with quantiles shaped (series, horizon, 10) = [mean, q10 .. q90]). Verify with `python selfcheck.py`."""
    name = "timesfm25"
    default_checkpoint = "google/timesfm-2.5-200m-pytorch"
    default_class = "TimesFM_2p5_200M_torch"

    def __init__(self, checkpoint: str = "", class_name: str = "", max_context: int = 512, max_horizon: int = 32):
        self.checkpoint = checkpoint or self.default_checkpoint
        self.class_name = class_name or self.default_class
        self.max_context, self.max_horizon = int(max_context), int(max_horizon)
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import timesfm                                  # noqa: PLC0415 - late: torch is heavy
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"timesfm is not installed in this image ({str(e)[:100]})") from e
        cls = getattr(timesfm, self.class_name, None)
        if cls is None:
            raise BackendError(f"timesfm has no class {self.class_name}")
        model = cls.from_pretrained(self.checkpoint)
        model.compile(timesfm.ForecastConfig(max_context=self.max_context, max_horizon=self.max_horizon, normalize_inputs=True,
                                             use_continuous_quantile_head=True, force_flip_invariance=True, infer_is_positive=True,
                                             fix_quantile_crossing=True))
        self._model = model
        return model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def package_version(self) -> Optional[str]:
        try:
            import importlib.metadata as md
            return md.version("timesfm")
        except Exception:  # noqa: BLE001
            return None

    def forecast(self, series_list: Sequence[Sequence[float]], horizon: int) -> List[Fan]:
        import numpy as np                                  # noqa: PLC0415
        if horizon > self.max_horizon:
            raise BackendError(f"horizon {horizon} above max_horizon {self.max_horizon}")
        model = self._load()
        inputs = [np.asarray(s[-self.max_context:], dtype="float32") for s in series_list]
        point, quant = model.forecast(horizon=horizon, inputs=inputs)
        point, quant = np.asarray(point), np.asarray(quant)
        out: List[Fan] = []
        for i in range(len(inputs)):
            qi = quant[i]
            if qi.ndim != 2 or qi.shape[0] < horizon:
                raise BackendError(f"unexpected quantile shape {quant.shape}")
            if qi.shape[1] == 10:                           # [mean, q10 .. q90]
                mean, cols = qi[:horizon, 0], qi[:horizon, 1:]
            elif qi.shape[1] == 9:
                cols = qi[:horizon, :]
                mean = np.asarray(point[i])[:horizon]
            else:
                raise BackendError(f"unexpected quantile width {qi.shape[1]}")
            out.append({"mean": [float(x) for x in mean], "quantiles": {str(l): [float(x) for x in cols[:horizon, j]] for j, l in enumerate(LEVELS)}})
        return out


class Research30Backend(TimesFM25Backend):
    """Local TimesFM 3.0 weights: non-commercial / non-production licence, so it exists for the offline probe only. It refuses to be built in
    REAL mode. There is no default checkpoint or class (nothing is guessed from here): set TSFM_RESEARCH_CHECKPOINT and TSFM_RESEARCH_CLASS."""
    name = "research-3.0"

    def __init__(self, trade_mode: str, checkpoint: str = "", class_name: str = "", **kw: Any):
        if str(trade_mode or "").strip().upper() == "REAL":
            raise BackendRefused("research-3.0 weights are not licensed for production use; it will not run with TSFM_TRADE_MODE=REAL")
        if not checkpoint or not class_name:
            raise BackendError("research-3.0 needs TSFM_RESEARCH_CHECKPOINT and TSFM_RESEARCH_CLASS (no default is guessed)")
        super().__init__(checkpoint=checkpoint, class_name=class_name, **kw)


class BigQueryBackend:
    """BigQuery AI.FORECAST. UNVERIFIED against a live project: the SQL below follows Google's AI.FORECAST documentation as I read it; if the
    argument names differ in your region / release, only SQL needs changing. It returns q10 / q50 / q90 (an 80% interval) and the forecast value,
    which is all tsfm_features needs. Needs TSFM_BQ_PROJECT and application-default credentials."""
    name = "bigquery"
    checkpoint = "bigquery:AI.FORECAST"
    SQL = ("SELECT forecast_timestamp, forecast_value, prediction_interval_lower_bound AS lo, prediction_interval_upper_bound AS hi "
           "FROM AI.FORECAST((SELECT ts, v FROM UNNEST(@ts) AS ts WITH OFFSET i JOIN UNNEST(@vals) AS v WITH OFFSET j ON i = j), "
           "data_col => 'v', timestamp_col => 'ts', horizon => @h, confidence_level => 0.8) ORDER BY forecast_timestamp")

    def __init__(self, project: str = ""):
        self.project = project or _env("TSFM_BQ_PROJECT")
        if not self.project:
            raise BackendError("bigquery backend needs TSFM_BQ_PROJECT")
        self._client: Any = None

    def forecast(self, series_list: Sequence[Sequence[float]], horizon: int) -> List[Fan]:
        try:
            from google.cloud import bigquery               # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"google-cloud-bigquery is not installed ({str(e)[:100]})") from e
        import datetime as dt
        client = self._client or bigquery.Client(project=self.project)
        self._client = client
        out: List[Fan] = []
        for s in series_list:
            n = len(s)
            start = dt.datetime(2000, 1, 1)
            ts = [start + dt.timedelta(days=i) for i in range(n)]            # an abstract daily axis: only the order matters
            cfg = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("ts", "TIMESTAMP", ts), bigquery.ArrayQueryParameter("vals", "FLOAT64", [float(x) for x in s]),
                bigquery.ScalarQueryParameter("h", "INT64", int(horizon))])
            rows = list(client.query(self.SQL, job_config=cfg).result())
            if len(rows) < horizon:
                raise BackendError(f"bigquery returned {len(rows)} rows for horizon {horizon}")
            rows = rows[:horizon]
            mid = [float(r["forecast_value"]) for r in rows]
            out.append({"mean": mid, "quantiles": {"0.1": [float(r["lo"]) for r in rows], "0.5": mid, "0.9": [float(r["hi"]) for r in rows]}})
        return out


def get_backend(name: Optional[str] = None, trade_mode: Optional[str] = None) -> Any:
    """Build the backend named by TSFM_BACKEND. Unknown names raise BackendError (a typo must not silently pick another licence)."""
    n = (name or _env("TSFM_BACKEND", "timesfm25")).lower()
    mode = trade_mode if trade_mode is not None else _env("TSFM_TRADE_MODE", "REAL")          # unset = the strict reading
    if n == "fake":
        return FakeBackend()
    if n == "timesfm25":
        return TimesFM25Backend(checkpoint=_env("TSFM_CHECKPOINT"), max_context=int(_env("TSFM_CONTEXT", "512")))
    if n == "research-3.0":
        return Research30Backend(mode, checkpoint=_env("TSFM_RESEARCH_CHECKPOINT"), class_name=_env("TSFM_RESEARCH_CLASS"), max_context=int(_env("TSFM_CONTEXT", "512")))
    if n == "bigquery":
        return BigQueryBackend()
    raise BackendError(f"unknown TSFM_BACKEND {n!r} (timesfm25 | bigquery | research-3.0 | fake)")
