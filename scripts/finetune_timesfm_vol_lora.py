"""scripts/finetune_timesfm_vol_lora.py — LoRA fine-tune TimesFM 2.5 on crypto REALIZED VOL.

The literature (arXiv 2505.11163) says the decisive vol-forecast win comes AFTER fine-tuning; our
zero-shot already beats HAR-RV on majors, so this tests whether a LoRA-specialised crypto-vol model
beats zero-shot. Adapts the official examples/finetuning/finetune_lora.py (Transformers + PEFT) to
our daily-RV series (multi-asset), CPU, small/fast. Compares fine-tuned vs zero-shot (MAE/QLIKE/corr).

Run: python scripts/finetune_timesfm_vol_lora.py --epochs 3 --num_samples 1500
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))
ASSETS = ["BTC", "ETH", "SOL", "MNT", "METH", "CMETH", "FBTC", "USDE", "ENA"]
MODEL_ID = "google/timesfm-2.5-200m-transformers"
ADAPTER = ROOT / "data" / "timesfm_vol_lora"
EVAL_TAIL = 120  # last N daily-RV points per series held out for eval


def _rv(asset: str) -> np.ndarray:
    f = ROOT / "data" / f"{asset.lower()}_features.csv"
    if not f.exists():
        return np.array([])
    close = pd.read_csv(f, usecols=["close"])["close"].to_numpy(float)
    r = np.diff(np.log(close))
    nd = len(r) // 24
    return np.array([np.sqrt(np.sum(r[d * 24:(d + 1) * 24] ** 2)) for d in range(nd)])


class RandWin(Dataset):
    def __init__(self, series, ctx, hor, n, seed=42):
        self.series, self.ctx, self.hor, self.s = series, ctx, hor, []
        rng = np.random.default_rng(seed)
        valid = [i for i, s in enumerate(series) if len(s) >= ctx + hor]
        for _ in range(n):
            i = rng.choice(valid)
            st = rng.integers(0, len(series[i]) - ctx - hor + 1)
            self.s.append((i, st))

    def __len__(self):
        return len(self.s)

    def __getitem__(self, k):
        i, st = self.s[k]
        s = self.series[i]
        return (torch.tensor(s[st:st + self.ctx], dtype=torch.float32),
                torch.tensor(s[st + self.ctx:st + self.ctx + self.hor], dtype=torch.float32))


def _qlike(act, pred):
    pred = np.clip(pred, 1e-9, None)
    return float(np.mean(act / pred - np.log(act / pred) - 1.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context_len", type=int, default=64)
    ap.add_argument("--horizon_len", type=int, default=1)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch_size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--num_samples", type=int, default=1500)
    ap.add_argument("--lora_r", type=int, default=4)
    a = ap.parse_args()

    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import TimesFm2_5ModelForPrediction

    rv_all = {x: _rv(x) for x in ASSETS}
    rv_all = {k: v for k, v in rv_all.items() if len(v) >= a.context_len + a.horizon_len + EVAL_TAIL}
    train_series = [v[:-EVAL_TAIL] for v in rv_all.values()]
    print(f"assets with data: {list(rv_all)} | train series: {len(train_series)}")

    print(f"loading {MODEL_ID} (first run downloads ~weights) …")
    model = TimesFm2_5ModelForPrediction.from_pretrained(MODEL_ID, torch_dtype=torch.float32, device_map="cpu")
    ctx = min(a.context_len, getattr(model.config, "context_length", a.context_len))

    model = get_peft_model(model, LoraConfig(r=a.lora_r, lora_alpha=a.lora_r * 2,
                                             target_modules="all-linear", lora_dropout=0.05, bias="none"))
    model.print_trainable_parameters()

    dl = DataLoader(RandWin(train_series, ctx, a.horizon_len, a.num_samples), batch_size=a.batch_size,
                    shuffle=True, drop_last=True)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=a.epochs * len(dl))

    for ep in range(1, a.epochs + 1):
        model.train()
        tot, nb = 0.0, 0
        for c, t in dl:
            out = model(past_values=c, future_values=t, forecast_context_len=ctx)
            out.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); opt.zero_grad(); sched.step()
            tot += float(out.loss.item()); nb += 1
        print(f"  epoch {ep}/{a.epochs}  train loss {tot / max(nb,1):.5f}")
    model.save_pretrained(str(ADAPTER))
    print(f"saved adapter -> {ADAPTER}")

    # ── eval: fine-tuned vs zero-shot on held-out tail (batched) ──
    base = TimesFm2_5ModelForPrediction.from_pretrained(MODEL_ID, torch_dtype=torch.float32, device_map="cpu").eval()
    ft = model.eval()  # in-memory trained model IS the fine-tuned one (adapter also saved above)
    X, Y = [], []
    for v in rv_all.values():
        for d in range(len(v) - EVAL_TAIL, len(v), 2):  # stride 2 over held-out tail
            if d - ctx >= 0:
                X.append(v[d - ctx:d]); Y.append(v[d])
    Xt = torch.tensor(np.array(X), dtype=torch.float32)
    act = np.array(Y)

    def predict(m):
        outs = []
        with torch.no_grad():
            for i in range(0, len(Xt), 64):
                outs.append(m(past_values=Xt[i:i + 64]).mean_predictions[:, 0].float().cpu().numpy())
        return np.clip(np.concatenate(outs), 1e-9, None)

    bp, fp = predict(base), predict(ft)
    print(f"\n=== eval ({len(act)} held-out next-day RV points) ===")
    for name, p in (("zero-shot", bp), ("LoRA-FT ", fp)):
        mae = float(np.mean(np.abs(p - act))) * 1e4
        print(f"  {name}:  MAE {mae:.2f}bp   QLIKE {_qlike(act, p):.4f}   corr {np.corrcoef(p, act)[0,1]:.3f}")
    imp = (_qlike(act, bp) - _qlike(act, fp)) / abs(_qlike(act, bp)) * 100
    print(f"  → LoRA QLIKE change: {imp:+.1f}%  ({'BETTER' if imp > 0 else 'worse'} than zero-shot)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"ERROR {e}")
        traceback.print_exc()
