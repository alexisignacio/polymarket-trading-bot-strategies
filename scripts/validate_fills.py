#!/usr/bin/env python3
"""Validate paper fills against real market prints (Data API v2).

For every closed trade with an entry timestamp, fetches the market's real
trades around that moment and checks whether the market actually printed at
(or better than) our entry price. A high unconfirmed rate means the paper
fill model is optimistic.

Read-only; stdlib only. This is a heuristic:
  - prints are matched within +-WINDOW seconds of the entry
  - a BUY entry is confirmed by a real print at price <= entry_price + tolerance
  - markets without prints in the window count as unconfirmed (we cannot
    prove the level traded)

Usage:
    python3 scripts/validate_fills.py [--state bot_state.json] [--window 300]
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

DATA_API = "https://data-api.polymarket.com/v2/trades"
TOLERANCE = 0.002  # price tolerance in probability points


def fetch_prints(condition_id: str, limit: int = 500) -> list:
    """Real prints for a market (newest first). Empty list on failure."""
    params = urllib.parse.urlencode({"condition": condition_id, "limit": limit})
    req = urllib.request.Request(f"{DATA_API}?{params}", headers={"User-Agent": "polyhft-validate/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [warn] no se pudo consultar {condition_id[:16]}...: {e}")
        return []
    if isinstance(payload, dict):
        return payload.get("data") or []
    return payload if isinstance(payload, list) else []


def parse_ts(value) -> float:
    """Trade record timestamp (iso or epoch) -> epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return 0.0


def validate(records: list, window: float) -> dict:
    by_market = {}
    for rec in records:
        mid = rec.get("market_id")
        if mid:
            by_market.setdefault(mid, []).append(rec)

    entries = confirmed = unconfirmed = no_prints = 0
    gaps = []  # (our price - best confirming print), positive = we got a better price
    for market_id, recs in by_market.items():
        prints = fetch_prints(market_id)
        if not prints:
            no_prints += len(recs)
            unconfirmed += len(recs)
            entries += len(recs)
            continue
        for rec in recs:
            if rec.get("side") != "buy":
                continue
            entries += 1
            entry_ts = parse_ts(rec.get("entry_time"))
            entry_price = float(rec.get("entry_price") or 0.0)
            nearby = [
                p for p in prints
                if abs(float(p.get("timestamp") or 0) - entry_ts) <= window
            ]
            outcome = str(rec.get("outcome") or "").upper()
            if outcome in ("YES", "NO"):
                # Sin filtrar por opción, los prints de la otra cara falsan
                # confirmaciones (un print de NO a 0.03 "confirma" una compra
                # de YES a 0.47)
                nearby = [
                    p for p in nearby
                    if str(p.get("outcome") or "").upper() == outcome
                ]
            confirm = [
                p for p in nearby
                if float(p.get("price") or 2.0) <= entry_price + TOLERANCE
            ]
            if confirm:
                confirmed += 1
                best = min(float(p["price"]) for p in confirm)
                gaps.append(entry_price - best)
            else:
                unconfirmed += 1
        time.sleep(0.2)

    return {
        "entries": entries,
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "no_prints": no_prints,
        "avg_price_gap": (sum(gaps) / len(gaps)) if gaps else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="bot_state.json")
    ap.add_argument("--window", type=float, default=300.0)
    args = ap.parse_args()

    state = json.loads(Path(args.state).read_text())
    records = (state.get("profitability") or {}).get("trade_history") or []
    buys = [r for r in records if r.get("side") == "buy" and r.get("entry_time")]
    if not buys:
        print("Sin trades comprados con entry_time registrado en el estado.")
        return

    print(f"Validando {len(buys)} entradas de {len({r.get('market_id') for r in buys})} "
          f"mercados contra prints reales (ventana ±{args.window:.0f}s)...")
    result = validate(buys, args.window)

    n = result["entries"] or 1
    print(f"\nConfirmadas:   {result['confirmed']}/{result['entries']} "
          f"({100.0 * result['confirmed'] / n:.1f}%)")
    print(f"Sin confirmar: {result['unconfirmed']} (de las cuales {result['no_prints']} "
          f"sin prints publicados en el mercado)")
    if result["avg_price_gap"] is not None:
        print(f"Gap medio (nuestro precio - mejor print confirmante): "
              f"{result['avg_price_gap']:+.4f}")
    print("\nNotas: heuristicas — un fill sin confirmar no es imposible (el libro")
    print("puede haberse movido entre snapshots), pero una tasa alta de")
    print("no confirmadas indica que el modelo de fill es optimista.")


if __name__ == "__main__":
    main()
