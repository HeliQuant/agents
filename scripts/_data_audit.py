"""Audit current dataset: rows + date span + bar frequency per asset."""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

print(f"{'asset':7} {'rows':>6}  {'span':>20}  {'days':>5}  bar")
for f in sorted(DATA.glob("*_features.csv")):
    df = pd.read_csv(f)
    name = f.stem.replace("_features", "")
    ts = None
    if "datetime" in df.columns:
        ts = pd.to_datetime(df["datetime"], errors="coerce", utc=True).dropna()
    elif "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], unit="ms", errors="coerce", utc=True).dropna()
    if ts is not None:
        if len(ts) > 1:
            span = ts.max() - ts.min()
            med = ts.sort_values().diff().median()
            print(f"{name:7} {len(df):6}  {str(ts.min())[:10]}->{str(ts.max())[:10]}  {span.days:5}  ~{med}")
            continue
    print(f"{name:7} {len(df):6}  (no usable time col; cols: {[c for c in df.columns][:5]})")
