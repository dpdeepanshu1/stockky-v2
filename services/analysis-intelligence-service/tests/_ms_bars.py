"""Shared synthetic-bar helpers for the engine tests (no network, no clock; seeded)."""
import random

T0 = 1_700_000_000
DAY = 86400


def walk(n, seed=5, base=100.0, drift=0.1, vol=1.2, t0=T0):
    random.seed(seed)
    px, out = base, []
    for i in range(n):
        o, c = px, max(5.0, px + random.gauss(drift, vol))
        out.append((t0 + i * DAY, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1000.0))
        px = c
    return out


def scale(bars, f):
    return [(b[0], b[1] * f, b[2] * f, b[3] * f, b[4] * f, b[5]) for b in bars]


def retime(bars, start_idx, t0=T0):
    return [(t0 + (start_idx + i) * DAY, b[1], b[2], b[3], b[4], b[5]) for i, b in enumerate(bars)]
