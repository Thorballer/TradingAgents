#!/usr/bin/env python3
"""Auto-trading CLI for the TradingAgents fork.

Usage:
  python -m autotrade_cli run NVDA [--date YYYY-MM-DD]
  python -m autotrade_cli run-all NVDA AAPL MSFT
  python -m autotrade_cli budget
  python -m autotrade_cli cache --clear

Env / .env keys (all TRADINGAGENTS_-prefixed, read from env or ~/.hermes/.env):
  TRADINGAGENTS_WATCHLIST=NVDA,AAPL,MSFT    (empty/absent = observe only)
  TRADINGAGENTS_IMSG_TO=+15615123130
  TRADINGAGENTS_WEEKLY_TOKEN_CAP=100000000
  TRADINGAGENTS_RESEARCH_TTL_HOURS=20
  TRADINGAGENTS_MAX_POSITION_PCT=0.10
  TRADINGAGENTS_DEBATE_ROUNDS=1
  TRADINGAGENTS_RISK_ROUNDS=1
  TRADINGAGENTS_ANALYSTS=market,news,fundamentals
  TRADINGAGENTS_DEEP_THINK_LLM=glm-4.7
  TRADINGAGENTS_QUICK_THINK_LLM=glm-4.7-flash
  TRADINGAGENTS_DRY_RUN=1                   (1 = log-only; 0 = place orders)
  TRADINGAGENTS_ALPACA_LIVE=1               (only in addition to DRY_RUN=0)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

STATE_DIR = Path("~/.tradingagents/autotrade").expanduser()
HERMES_ENV = Path("~/.hermes/.env").expanduser()


def _load_env_file() -> None:
    """Load TRADINGAGENTS_* / GLM_API_KEY / ALPACA_* from ~/.hermes/.env (no echo)."""
    keys = ("TRADINGAGENTS_", "GLM_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FRED_API_KEY")
    if not HERMES_ENV.exists():
        return
    for line in HERMES_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if any(k == p or k.startswith(p) for p in keys) and k not in os.environ:
            os.environ[k] = v
    # TradingAgents' glm provider reads ZHIPU_API_KEY; accept GLM_API_KEY as alias.
    if os.environ.get("GLM_API_KEY") and not os.environ.get("ZHIPU_API_KEY"):
        os.environ["ZHIPU_API_KEY"] = os.environ["GLM_API_KEY"]


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def build_runner():
    from tradingagents.autotrade import AutoTradeRunner

    return AutoTradeRunner.from_env(
        watchlist=[s.strip().upper() for s in os.environ.get("TRADINGAGENTS_WATCHLIST", "").split(",") if s.strip()],
        analysts=[s.strip() for s in os.environ.get("TRADINGAGENTS_ANALYSTS", "market,news,fundamentals").split(",") if s.strip()],
        debate_rounds=int(os.environ.get("TRADINGAGENTS_DEBATE_ROUNDS", "1")),
        risk_rounds=int(os.environ.get("TRADINGAGENTS_RISK_ROUNDS", "1")),
        weekly_cap_tokens=int(os.environ.get("TRADINGAGENTS_WEEKLY_TOKEN_CAP", "100000000")),
        research_ttl_hours=float(os.environ.get("TRADINGAGENTS_RESEARCH_TTL_HOURS", "20")),
        dry_run=_bool(os.environ.get("TRADINGAGENTS_DRY_RUN"), default=True),
        max_position_pct=float(os.environ.get("TRADINGAGENTS_MAX_POSITION_PCT", "0.10")),
        imsg_to=os.environ.get("TRADINGAGENTS_IMSG_TO") or None,
    )


def cmd_budget() -> int:
    from tradingagents.autotrade.ledger import TokenLedger

    ledger = TokenLedger(
        state_path=STATE_DIR / "ledger.json",
        weekly_cap_tokens=int(os.environ.get("TRADINGAGENTS_WEEKLY_TOKEN_CAP", "100000000")),
        alert_callback=lambda *a: None,
    )
    u = ledger.usage()
    bar = "█" * int(u["pct"] * 30) + "░" * (30 - int(u["pct"] * 30))
    print(f"Week of {u['week_start']}")
    print(f"[{bar}] {u['pct'] * 100:.1f}%")
    print(f"{u['used']:,} / {u['cap']:,} tokens  (remaining {u['remaining']:,})")
    print(f"Alerts fired: {', '.join(u['fired']) or 'none'}")
    return 0


def cmd_cache(args) -> int:
    import shutil

    from tradingagents.autotrade.research_cache import ResearchCache

    d = STATE_DIR / "research_cache" / "decisions"
    if args.clear:
        if d.exists():
            n = len(list(d.glob('*.json')))
            shutil.rmtree(d)
            print(f"Cleared {n} cached decisions.")
        else:
            print("Cache empty.")
    else:
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
        print(f"{len(files)} cached decisions in {d}")
        for p in files[:10]:
            rec = json.loads(p.read_text())
            age_h = (__import__("datetime").datetime.now() - __import__("datetime").datetime.fromisoformat(rec["written_at"])).total_seconds() / 3600
            print(f"  {rec['ticker']:6} {rec['trade_date']} {rec['signal']:12} "
                  f"{rec.get('tokens_used', 0):>10,} tok  age {age_h:.1f}h")
    return 0


def cmd_run(runner, args) -> int:
    tickers = args.tickers
    for i, t in enumerate(tickers):
        try:
            result = runner.run(t, trade_date=args.date)
        except Exception as e:
            print(f"{t}: FAILED {e}", file=sys.stderr)
            continue
        sig = result.get("signal", "?")
        tok = result.get("tokens_used", 0)
        ex = result.get("execution", {})
        print(f"{t}: {sig} | tokens {tok:,} | {ex.get('action', 'none')}"
              f"{' (dry-run)' if ex.get('dry_run') else ''}")
    return 0


def main() -> int:
    _load_env_file()
    ap = argparse.ArgumentParser(prog="autotrade")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="research + (optionally) trade tickers")
    p_run.add_argument("tickers", nargs="+")
    p_run.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")

    sub.add_parser("budget", help="show weekly token budget")
    p_cache = sub.add_parser("cache", help="show/clear decision cache")
    p_cache.add_argument("--clear", action="store_true")

    args = ap.parse_args()

    if args.cmd == "budget":
        return cmd_budget()
    if args.cmd == "cache":
        return cmd_cache(args)

    runner = build_runner()
    return cmd_run(runner, args)


if __name__ == "__main__":
    sys.exit(main())
