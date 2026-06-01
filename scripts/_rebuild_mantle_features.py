"""One-off: rebuild MNT/mETH feature CSVs from their existing hourly CSVs.

Pyth oracle OHLC carries no volume, so the old add_features() produced an
all-NaN `volume_change` column, and the downstream `.dropna()` wiped every row
(→ XGBoost trained on 0 rows → crash). multi_asset.add_features now handles
zero/missing volume. This re-derives features from the on-disk hourly data
(no network re-fetch) so MNT/mETH walk-forward can run.

Run: python scripts/_rebuild_mantle_features.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
multi_asset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multi_asset)

for t in ("mnt", "meth"):
    hourly = pd.read_csv(ROOT / "data" / f"{t}_hourly.csv")
    feat = multi_asset.add_features(hourly)
    feat.to_csv(ROOT / "data" / f"{t}_features.csv", index=False)
    all_nan = [c for c in feat.columns if feat[c].isna().all()]
    print(
        f"{t.upper()}: rows {len(feat)} | after dropna {len(feat.dropna())} "
        f"| all-NaN cols {all_nan}"
    )
