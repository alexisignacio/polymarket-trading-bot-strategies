#!/usr/bin/env python3
"""Daily trading report: runs the trade audit and delivers it.

Without Telegram credentials it prints to stdout (cron-friendly); with
TELEGRAM_BOT_TOKEN + chat id configured it sends the report to Telegram.

Cron example (09:00 daily):
    0 9 * * * cd /path/to/repo && venv/bin/python scripts/daily_report.py >> logs/daily_report.log 2>&1

Usage:
    python3 scripts/daily_report.py [--state bot_state.json] [--log logs/trades.log]
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TELEGRAM_LIMIT = 4000  # leave margin under the 4096 message cap


def build_report(state: str, log: str, validate: bool = True) -> str:
    """The audit report as text (single source of truth: audit_trades.py)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_trades.py"),
         "--state", state, "--log", log],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    report = (result.stdout or "") + (result.stderr or "")

    if validate:
        check = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_fills.py"),
             "--state", state],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        report += "\n\n== Validación de fills vs prints reales ==\n"
        report += (check.stdout or "") + (check.stderr or "")
    return report


def send_telegram(report: str) -> bool:
    """Send via Telegram when credentials exist; False when not configured."""
    import os
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        try:
            import yaml
            cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
            tg = cfg.get("telegram") or {}
            token = token or tg.get("bot_token")
            chat_id = chat_id or tg.get("chat_id")
        except Exception:
            pass
    if not token or not chat_id:
        return False

    from src.utils.telegram_notifier import TelegramNotifier
    notifier = TelegramNotifier(token, chat_id)
    text = report.strip()[:TELEGRAM_LIMIT]
    return bool(notifier.send_message(f"📊 <b>Reporte diario PolyHFT</b>\n<pre>{text}</pre>"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="bot_state.json")
    ap.add_argument("--log", default="logs/trades.log")
    ap.add_argument("--no-validate", action="store_true",
                    help="omitir la validación de fills contra prints reales")
    args = ap.parse_args()

    report = build_report(args.state, args.log, validate=not args.no_validate)
    print(report)
    if send_telegram(report):
        print("[daily_report] reporte enviado a Telegram")
    else:
        print("[daily_report] Telegram no configurado (solo stdout)")


if __name__ == "__main__":
    main()
