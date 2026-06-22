"""Learning #1 runner — fetch the live campaign trade ledger and calibrate per-asset TP/SL from the
realized MFE/MAE. Diagnoses WHY the TP rarely hits (median MFE vs the 2.5x-ATR bar) and writes
data/tp_sl_calibration.json. Measure first; wiring into the campaign is a separate step.

    python scripts/95_calibrate_tpsl.py            # pull from the live firm
    python scripts/95_calibrate_tpsl.py trades.json  # or a saved /trades JSON
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from firm.tp_calibration import CALIB, calibrate  # noqa: E402

URL = "https://agents-production-5a3d.up.railway.app/trades?limit=300"


def _load_trades() -> list:
    if len(sys.argv) > 1:
        return json.loads(Path(sys.argv[1]).read_text()).get("trades", [])
    return requests.get(URL, timeout=30).json().get("trades", [])


def main() -> None:
    trades = _load_trades()
    print(f"fetched {len(trades)} closed trades · measuring real MFE/MAE per trade (exchange 15m candles)...\n")
    calib = calibrate(trades)
    if not calib:
        print("no asset reached the >=5-trade minimum — nothing written")
        return
    CALIB.parent.mkdir(exist_ok=True)
    CALIB.write_text(json.dumps(calib, indent=2))
    print(f"\nwrote {CALIB.relative_to(Path.cwd()) if CALIB.is_relative_to(Path.cwd()) else CALIB}")
    reached = [v["pct_reached_cur_tp"] for v in calib.values()]
    print(f"\nDIAGNOSIS: across {len(calib)} assets, the current 2.5x-ATR TP was reached by "
          f"{min(reached)*100:.0f}-{max(reached)*100:.0f}% of trades — "
          f"{'confirms TP is too far (rarely reachable)' if max(reached) < 0.35 else 'mixed'}.")


if __name__ == "__main__":
    main()
