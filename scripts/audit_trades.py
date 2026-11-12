#!/usr/bin/env python3
"""Trade audit: honest P&L, fee drag and per-strategy breakdown.

Reads the persisted trade history from bot_state.json and, when the structured
history is thinner than the cumulative counters (trades closed before records
were persisted), supplements it with approximate numbers parsed from
logs/trades.log PAPER CLOSE lines.

Usage:
    python3 scripts/audit_trades.py [--state bot_state.json] [--log logs/trades.log]

The report never extrapolates: with a small sample it says so.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.utils.calibration import price_calibration  # noqa: E402

MIN_SAMPLE = 30  # below this, everything is flagged as indicative only

# 2026-11-11 16:08:17 - 📝 [PAPER CLOSE] pos_x @ 0.9480 P&L $0.33 (6.53%) fees $0.0000 reason
CLOSE_RE = re.compile(
    r"PAPER CLOSE\] (?P<pos>\S+) @ (?P<exit>[\d.]+) "
    r"P&L \$(?P<pnl>-?[\d.]+) \((?P<pct>-?[\d.]+)%\)"
    r"(?: fees \$(?P<fees>[\d.]+))?(?: (?P<reason>.*))?$"
)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"ERROR: cannot read state {path}: {e}")
        sys.exit(1)


def parse_log_closes(path: Path) -> list:
    records = []
    if not path.exists():
        return records
    for line in path.read_text(errors="replace").splitlines():
        m = CLOSE_RE.search(line)
        if not m:
            continue
        records.append({
            "position_id": m.group("pos"),
            "exit_price": float(m.group("exit")),
            "pnl": float(m.group("pnl")),
            "pnl_pct": float(m.group("pct")),
            "fees": float(m.group("fees") or 0.0),
            "reason": (m.group("reason") or "").strip(),
        })
    return records


def sample_warning(n: int) -> str:
    if n >= MIN_SAMPLE:
        return ""
    return f"  [!] muestra N={n} < {MIN_SAMPLE}: TODO es indicativo, no concluyente"


def report_records(records: list, label: str = "estructurados") -> None:
    n = len(records)
    print(f"\n== Trades cerrados ({label}: {n}) ==")
    if n == 0:
        print("  Sin registros en esta época.")
        return

    wins = [r for r in records if r["pnl"] > 0]
    losses = [r for r in records if r["pnl"] < 0]
    total_pnl = sum(r["pnl"] for r in records)
    total_fees = sum(r.get("fees", 0.0) for r in records)
    gross = total_pnl + total_fees

    print(f"  Win rate:        {len(wins)}/{n} = {100.0 * len(wins) / n:.1f}%")
    print(f"  P&L neto:        ${total_pnl:.2f} (bruto ${gross:.2f}, fees ${total_fees:.2f})")
    if total_fees > 0 and gross != 0:
        print(f"  Fee drag:        {100.0 * total_fees / abs(gross):.1f}% del P&L bruto")
    if wins:
        print(f"  Ganancia media:  ${sum(r['pnl'] for r in wins) / len(wins):.3f}")
    if losses:
        print(f"  Pérdida media:   ${sum(r['pnl'] for r in losses) / len(losses):.3f}")
    print(f"  Expectativa:     ${total_pnl / n:.3f} por trade")
    if sample_warning(n):
        print(sample_warning(n))


def report_strategies(stats: dict) -> None:
    print("\n== Por estrategia (contadores acumulados) ==")
    if not stats:
        print("  Sin datos por estrategia en el estado.")
        return
    for name, s in sorted(stats.items()):
        n = int(s.get("total_trades", 0))
        print(f"  {name}: {n} trades | win rate {s.get('win_rate', 0):.1f}% | "
              f"P&L ${s.get('total_pnl', 0.0):.2f} | avg win ${s.get('avg_win', 0):.3f} | "
              f"avg loss ${s.get('avg_loss', 0):.3f}")


def report_calibration(records: list, strategy: str = None, label: str = "") -> None:
    """Precio pagado vs win rate real: ¿el precio subestima la probabilidad?"""
    scope = label or ("todas las estrategias" if not strategy else strategy)
    print(f"\n== Calibración por precio de entrada ({scope}) ==")
    rows = price_calibration(records, strategy=strategy)
    used = [r for r in rows if r['trades']]
    if not used:
        print("  Sin trades cerrados con precio de entrada registrado.")
        return
    print("  bucket        n   win%    precio   breakeven   edge_pp   P&L")
    for r in rows:
        if not r['trades']:
            continue
        print(f"  {r['bucket']:<12} {r['trades']:>3} {r['win_rate']:>6.1f}% "
              f"{r['avg_price']:>8.4f} {r['breakeven_win_rate']:>9.1f}% "
              f"{r['edge_pp']:>+8.1f}  ${r['total_pnl']:>6.2f}")
    total = sum(r['trades'] for r in used)
    print("  edge_pp = win% observado - probabilidad implícita del precio")
    print("  (positivo: las entradas rinden más de lo que el precio pide)")
    if sample_warning(total):
        print(sample_warning(total))


def report_tail_end(records: list, log_records: list) -> None:
    te = [r for r in records if r.get("strategy") == "tail_end"]
    print("\n== Auditoría tail-end (compra barato cerca de resolución) ==")
    if not te:
        print("  Sin trades tail-end en la historia estructurada.")
        if log_records:
            print(f"  (el log tiene {len(log_records)} cierres aproximados sin estrategia/entrada;")
            print("   no alcanza para auditar la economía de la estrategia)")
        return

    entries = [r["entry_price"] for r in te]
    avg_entry = sum(entries) / len(entries)
    wins = sum(1 for r in te if r["pnl"] > 0)
    observed_wr = wins / len(te) if te else 0.0
    print(f"  Muestra: {len(te)} trades | entry media ${avg_entry:.3f}")
    print(f"  Win rate observado: {observed_wr * 100:.1f}%")
    print(f"  Win rate de equilibrio (bruto): {avg_entry * 100:.1f}% "
          f"(necesitas ganar más que el precio pagado)")
    if observed_wr > avg_entry:
        print("  -> Con esta muestra, la estrategia genera edge sobre el precio.")
    else:
        print("  -> Con esta muestra, NO hay edge sobre el precio (o no hay datos).")
    print("  Riesgo estructural: cada derrota cuesta ~100% de la posición;")
    wins_to_recover = avg_entry / max(1 - avg_entry, 1e-9)
    print(f"  con entry media ${avg_entry:.2f}, 1 derrota borra ~{wins_to_recover:.0f} victorias "
          f"(retorno bruto {(1 - avg_entry) / avg_entry * 100:.1f}% por victoria).")
    if sample_warning(len(te)):
        print(sample_warning(len(te)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="bot_state.json")
    ap.add_argument("--log", default="logs/trades.log")
    args = ap.parse_args()

    state = load_state(Path(args.state))
    prof = state.get("profitability") or {}
    records = prof.get("trade_history") or []
    log_records = parse_log_closes(Path(args.log))

    print("== Contadores acumulados (estado) ==")
    total = int(prof.get("total_trades", 0))
    print(f"  Trades: {total} | wins: {prof.get('winning_trades', 0)} | "
          f"losses: {prof.get('losing_trades', 0)} | win rate: {prof.get('win_rate', 0):.1f}%")
    print(f"  P&L total: ${prof.get('total_pnl', 0.0):.2f} | capital: "
          f"${prof.get('initial_capital', 0):.2f} -> ${prof.get('current_capital', 0):.2f}")
    if total and not records:
        print("  (sin registros estructurados: estos trades se cerraron antes de la fase 9)")

    verified = [r for r in records if r.get('fill_model') == 'flow_verified']
    legacy = [r for r in records if r.get('fill_model') != 'flow_verified']

    report_records(verified, label="verificados (evidencia de flujo)")
    report_calibration(verified, label="verificados")
    if legacy:
        report_records(legacy, label="LEGACY, modelo optimista pre-fase 11")
        report_calibration(legacy, label="LEGACY — no mezclar con los verificados")
    report_strategies(prof.get("strategy_stats") or {})

    if log_records and len(records) < total:
        pnl = sum(r["pnl"] for r in log_records)
        wins = sum(1 for r in log_records if r["pnl"] > 0)
        print(f"\n== Suplemento desde logs ({len(log_records)} cierres, aproximado) ==")
        print(f"  Win rate: {100.0 * wins / len(log_records):.1f}% | P&L: ${pnl:.2f} | "
              f"fees: ${sum(r['fees'] for r in log_records):.2f}")
        reasons = {}
        for r in log_records:
            key = r["reason"] or "sin razón registrada"
            reasons[key] = reasons.get(key, 0) + 1
        print("  Razones de cierre:", ", ".join(f"{k}({v})" for k, v in sorted(reasons.items())))

    report_tail_end(verified or records, log_records)


if __name__ == "__main__":
    main()
