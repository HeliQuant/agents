"""Why were cmETH/fBTC INCONCLUSIVE while MNT/mETH passed?
Show, per token: OOS length, OOS price move, % of OOS bars in a Trending regime
(momentum only fires on Trending), and the OOS trade count from the walk-forward.
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("multi_asset", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

OOS_TRADES = {"MNT": 17, "METH": 4, "CMETH": 3, "FBTC": 0}  # from mantle_trend_walkforward.json

for t in ["mnt", "meth", "cmeth", "fbtc"]:
    df = pd.read_csv(ROOT / "data" / f"{t}_features.csv").dropna().reset_index(drop=True)
    split = int(len(df) * 0.60)
    train, test = df.iloc[:split], df.iloc[split:].reset_index(drop=True)
    adx_th = float(train["adx"].quantile(0.60))
    vol_th = float(train["volatility_10"].quantile(0.85))
    regimes = test.apply(lambda r: ma.detect_current(r, adx_th, vol_th), axis=1)
    trend_pct = 100 * regimes.isin(["Trending_Up", "Trending_Down"]).sum() / len(test)
    pc = (float(test["close"].iloc[-1]) / float(test["close"].iloc[0]) - 1) * 100
    print(f"{t.upper():5} | OOS {len(test):4} bars (~{len(test)/24:3.0f}d) | price {pc:+6.1f}% "
          f"| trending bars {trend_pct:4.0f}% | OOS trades {OOS_TRADES[t.upper()]}")
