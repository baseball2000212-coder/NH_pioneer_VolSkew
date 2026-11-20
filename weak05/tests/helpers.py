import json
from pathlib import Path

import numpy as np
import pandas as pd

CFG = json.loads((Path(__file__).resolve().parents[1] / "strategy.json").read_text(encoding="utf-8"))


def make_bars(n=200, start="2026-11-10 21:00", seed=3, step=5, drift=0.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=f"{step}min")
    close = 7500 + np.cumsum(rng.normal(drift, 2.0, n))
    open_ = np.concatenate([[7500], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0, 1, n)
    low = np.minimum(open_, close) - rng.uniform(0, 1, n)
    vol = rng.integers(500, 3000, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol, "n_sub": 5},
                        index=pd.DatetimeIndex(idx, name="ts"))
