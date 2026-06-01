"""Quick: what regime is each local asset in right now (latest bar)?"""
import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ma", ROOT / "scripts" / "multi_asset.py")
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)

VALIDATED = {"MNT", "METH"}
for f in sorted((ROOT / "data").glob("*_features.csv")):
    try:
        df = pd.read_csv(f).dropna().reset_index(drop=True)
        if len(df) < 50:
            continue
        adx_th = float(df["adx"].quantile(0.60))
        vol_th = float(df["volatility_10"].quantile(0.85))
        last = df.iloc[-1]
        reg = ma.detect_current(last, adx_th, vol_th)
        tic = f.stem.replace("_features", "").upper()
        star = " *VALIDATED*" if tic in VALIDATED else ""
        flag = "  <<< TRENDING" if reg in ("Trending_Up", "Trending_Down") else ""
        print(f"{tic:8} {reg:16} adx={last['adx']:5.1f} (th {adx_th:4.1f}){star}{flag}")
    except Exception as e:  # noqa: BLE001
        print(f"{f.stem:8} ERR {str(e)[:60]}")
