"""forecaster.py - the only module that imports torch / Kronos. engine.py and forecast_stats.py run without them.

Loads Kronos-small and its tokenizer once, puts both in eval() mode (KronosPredictor does not), and returns N independent sampled paths per call:
predict_batch on N copies of the same candles with sample_count=1 (Kronos' predict() would average the paths into one).
Model files come from Hugging Face on first start (cached in HF_HOME). Pin KRONOS_MODEL_REV / KRONOS_TOKENIZER_REV to a commit hash for
reproducible weights; unset = the repo's current head.
"""
from __future__ import annotations

import logging
import os
import zlib
from typing import Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("kronos-forecaster")


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


class KronosForecaster:
    """sample_paths(df, horizon, n_paths, seed_key) -> (closes, highs, lows), each (n_paths, horizon) in price units."""

    def __init__(self) -> None:
        import torch
        from kronos_model import Kronos, KronosPredictor, KronosTokenizer
        self.torch = torch
        self.model_name = _env("KRONOS_MODEL", "NeoQuasar/Kronos-small")
        self.tokenizer_name = _env("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
        m_rev = _env("KRONOS_MODEL_REV", "") or None
        t_rev = _env("KRONOS_TOKENIZER_REV", "") or None
        torch.set_num_threads(max(1, int(float(_env("KRONOS_TORCH_THREADS", "1")))))
        tok = KronosTokenizer.from_pretrained(self.tokenizer_name, revision=t_rev) if t_rev else KronosTokenizer.from_pretrained(self.tokenizer_name)
        mdl = Kronos.from_pretrained(self.model_name, revision=m_rev) if m_rev else Kronos.from_pretrained(self.model_name)
        tok.eval()
        mdl.eval()
        self.predictor = KronosPredictor(mdl, tok, device="cpu", max_context=int(_env("KRONOS_MAX_CONTEXT", "512")))
        self.temperature = float(_env("KRONOS_TEMPERATURE", "0.6"))
        self.top_p = float(_env("KRONOS_TOP_P", "0.9"))
        logger.info("Kronos loaded: model=%s tokenizer=%s", self.model_name, self.tokenizer_name)

    def sample_paths(self, df: pd.DataFrame, future_ts: pd.DatetimeIndex, n_paths: int, seed_key: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """df: clean_history output. Same seed_key -> the same paths (so a repeated request does not flip the verdict)."""
        seed = zlib.crc32(seed_key.encode("utf-8")) & 0x7FFFFFFF
        self.torch.manual_seed(seed)
        x = df[["open", "high", "low", "close", "volume", "amount"]]
        x_ts = pd.Series(pd.to_datetime(df["date"]).values)
        y_ts = pd.Series(future_ts.values)
        n = int(n_paths)
        with self.torch.no_grad():
            frames = self.predictor.predict_batch([x] * n, [x_ts] * n, [y_ts] * n, pred_len=len(future_ts), T=self.temperature,
                                                  top_k=0, top_p=self.top_p, sample_count=1, verbose=False)
        closes = np.stack([f["close"].values for f in frames]).astype(float)
        highs = np.stack([f["high"].values for f in frames]).astype(float)
        lows = np.stack([f["low"].values for f in frames]).astype(float)
        return closes, highs, lows
