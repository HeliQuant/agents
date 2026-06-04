"""Anchor each VALIDATED edge's validation record on Mantle Sepolia — "tervalidasi + tercatat on-chain".

For every edge in validated_edges.json, build a canonical validation record (asset, edge, p_win,
payoff, sample_n, OOS-ROI, firm tokenId) + its SHA-256, and anchor that hash in a Mantle Sepolia tx.
Anyone can later verify the firm validated this edge at that block — auditable proof, not a claim.

Candidates (candidate_edges.json) are NOT anchored as validated — only edges that earned the bar.
This closes the loop: edge_lab earns it → self-learning graduates it → THIS records it on Mantle.

Run:  python scripts/61_anchor_validation.py             # DRY: build records + hashes, no tx
      python scripts/61_anchor_validation.py --broadcast  # send one real anchor tx per edge (testnet)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firm.onchain_recorder import anchor_validation  # noqa: E402

DATA = ROOT / "data"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    broadcast = "--broadcast" in sys.argv
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    val = json.loads((DATA / "validated_edges.json").read_text())
    if not val:
        print("no validated edges to anchor.")
        return

    print(f"Anchoring {len(val)} validated edge(s) on Mantle Sepolia  "
          f"({'BROADCAST — real tx' if broadcast else 'DRY — no tx'})\n")
    anchors = {}
    for asset, edge in val.items():
        res = anchor_validation(edge, ts, send=broadcast)
        rec = res["record"]
        print(f"[{asset}] {rec['edge']}  p_win={rec['p_win']} payoff={rec['payoff_b']} "
              f"n={rec['sample_n']} OOS={rec['oos_roi_pct']}%  agentId={rec.get('firm_token_id')}")
        print(f"   record_hash: {res['record_hash']}")
        if res.get("sent"):
            print(f"   ✅ ANCHORED -> {res['explorer']}")
            anchors[asset] = {"record_hash": res["record_hash"], "tx_hash": res["tx_hash"],
                              "explorer": res["explorer"], "ts": ts, "edge": rec["edge"]}
        elif broadcast:
            print(f"   ⚠️ not sent: {res.get('error')}")
        else:
            print(f"   (dry — pass --broadcast to anchor on-chain)")
        print()

    if anchors:
        out = DATA / "validation_anchors.json"
        existing = json.loads(out.read_text()) if out.exists() else {}
        existing.update(anchors)
        out.write_text(json.dumps(existing, indent=2))
        print(f"saved {len(anchors)} anchor(s) -> data/validation_anchors.json")


if __name__ == "__main__":
    main()
