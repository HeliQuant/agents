"""What's actually IN Bybit's 3 positioning endpoints (open-interest / funding / long-short)?
Public, no secret. Perp concepts -> query MNT/BTC linear perps on MAINNET (real positioning data)."""
import requests

BASE = "https://api.bybit.com"  # mainnet public = REAL data (no key needed)


def get(label, path, params):
    r = requests.get(BASE + path, params=params, timeout=15)
    j = r.json()
    print(f"\n=== {label} -> HTTP {r.status_code} retCode {j.get('retCode')} ({j.get('retMsg')}) ===")
    return j.get("result", {}).get("list", []) if j.get("retCode") == 0 else []


def fnum(x, k):
    try:
        return float(x[k])
    except Exception:  # noqa: BLE001
        return None


for SYM in ("MNTUSDT", "BTCUSDT"):
    print(f"\n############################ {SYM} (linear perp) ############################")

    # 1) OPEN INTEREST — total open perp contracts (positioning size)
    oi = get(f"open-interest {SYM} 1h x24", "/v5/market/open-interest",
             {"category": "linear", "symbol": SYM, "intervalTime": "1h", "limit": 24})
    if oi:
        vals = [fnum(x, "openInterest") for x in oi]
        print(f"   sample row: {oi[0]}")
        print(f"   latest OI: {vals[0]:,.0f} | 24h ago: {vals[-1]:,.0f} | "
              f"change: {((vals[0]/vals[-1]-1)*100):+.1f}% (rising=positions building)")

    # 2) FUNDING RATE — sign tells which side is crowded/pays
    fh = get(f"funding/history {SYM} x30", "/v5/market/funding/history",
             {"category": "linear", "symbol": SYM, "limit": 30})
    if fh:
        rates = [fnum(x, "fundingRate") for x in fh]
        latest = rates[0]
        mean = sum(rates) / len(rates)
        print(f"   sample row: {fh[0]}")
        print(f"   latest funding: {latest*100:+.4f}% | mean(30): {mean*100:+.4f}%  "
              f"({'longs pay shorts -> long-crowded/bullish' if latest > 0 else 'shorts pay longs -> short-crowded/bearish'})")

    # 3) LONG/SHORT ACCOUNT RATIO — % of accounts long vs short
    ls = get(f"account-ratio {SYM} 1h x24", "/v5/market/account-ratio",
             {"category": "linear", "symbol": SYM, "period": "1h", "limit": 24})
    if ls:
        buy = [fnum(x, "buyRatio") for x in ls]
        print(f"   sample row: {ls[0]}")
        print(f"   latest buyRatio: {buy[0]:.3f} sellRatio: {fnum(ls[0],'sellRatio'):.3f} | "
              f"mean buyRatio(24): {sum(buy)/len(buy):.3f}  (>0.5 = crowd net long)")
