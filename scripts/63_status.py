"""HeliQuant — one-screen SYSTEM STATUS (read-only command center).

Ties the whole honest stack together for a glance / demo: validated edges (+ their on-chain
validation anchor + current live signal), paper CANDIDATES (learning), learned DESK reliability
weights, and the deployed ERC-8004 / vault addresses. Pure read of real state — no network writes,
no fabrication. If a file is missing it just shows '—'. Run: python scripts/63_status.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))


def _load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return default


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        from firm.edge_lab import live_signal
    except Exception:  # noqa: BLE001
        live_signal = None

    val = _load(DATA / "validated_edges.json", {})
    cand = _load(DATA / "candidate_edges.json", {})
    weights = _load(DATA / "desk_weights.json", {}).get("weights", {})
    anchors = _load(DATA / "validation_anchors.json", {})
    deploy = _load(ROOT.parent / "contracts" / "deployments" / "mantle_sepolia.json", {})

    bar = "═" * 78
    print(bar)
    print("  HeliQuant — Autonomous Trading-Intelligence Firm  ·  SYSTEM STATUS")
    print(bar)

    print("\n▣ VALIDATED EDGES  (live-eligible · AGGRESSIVE-capable · earned the cost-aware OOS + walk-forward bar)")
    if not val:
        print("   (none)")
    for a, e in val.items():
        sig = live_signal(a, e.get("edge", ""), DATA).get("signal") if live_signal else "?"
        anc = anchors.get(a)
        anc_s = f"on-chain ✅ tx {anc['tx_hash'][:10]}…" if anc else "on-chain: not yet anchored"
        print(f"   {a:5} {e.get('edge'):14} p_win {e.get('p_win')} · payoff {e.get('payoff_b')} · "
              f"n{e.get('sample_n')} · OOS {e.get('oos_roi_pct')}%")
        print(f"         live signal: {sig or 'wait (no extreme)'}   |   {anc_s}")

    print("\n▣ CANDIDATES  (paper-only · learning · passed 1-split but NOT yet walk-forward-robust)")
    if not cand:
        print("   (none)")
    for a, e in cand.items():
        sig = live_signal(a, e.get("edge", ""), DATA).get("signal") if live_signal else "?"
        print(f"   {a:5} {e.get('edge'):14} 1-split OOS {e.get('oos_roi_pct')}%  ·  live signal: {sig or 'wait'}  "
              f"·  paper-trading until graduated (scripts/60)")

    print("\n▣ DESK RELIABILITY  (learned from track record · ADVISORY soft-prior · bounded 0.6–1.4 · never mutes)")
    if not weights:
        print("   (neutral — no weights learned yet; org behaves identically)")
    for d, w in weights.items():
        flag = "learned" if abs(w - 1.0) > 1e-6 else "neutral"
        bar_w = "█" * int(round(w * 10))
        print(f"   {d:18} {w:.2f}x  {bar_w:<14} {flag}")

    print("\n▣ ON-CHAIN  (Mantle Sepolia · chain 5003)")
    print(f"   validation anchors recorded: {len(anchors)}")
    for key, label in (("jobManager", "JobManager"), ("tradingVault", "TradingVault"),
                       ("identityRegistry", "IdentityRegistry"), ("validationRegistry", "ValidationRegistry")):
        if deploy.get(key):
            print(f"   {label:20} {deploy[key]}")
    if deploy.get("firmTokenId"):
        print(f"   firm ERC-8004 tokenId: {deploy['firmTokenId']}")

    print("\n▣ LOOP  ·  onboard new assets: scripts/59 --write  ·  graduate/demote: scripts/60 --apply  ·  "
          "anchor: scripts/61 --broadcast")
    print(bar)
    print("  Honest by design: registry grows only on earned, walk-forward-robust edges; "
          "candidates paper-trade; gates always bind.")
    print(bar)


if __name__ == "__main__":
    main()
