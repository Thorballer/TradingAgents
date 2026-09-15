"""Algorithmic screener: find unusual-momentum/volume candidates for one-shot
deep research. Pure math — zero LLM tokens.

Universe: S&P 500 constituents (daily bars via yfinance). Output: JSON state
file of candidates with signals, momentum, volume ratios, and reasons.

Signals (veto-based, not score-stacking):
- momentum_up:   1M return >= +8%, price > 20DMA > 50DMA, not overextended
- momentum_down: 1M return <= -8%, price < 20DMA < 50DMA (fade/exit research)
- volume_spike:  volume >= 2.5x its 30-day average with |1M return| >= 4%
                 (institutional attention — direction resolved by research)

Vetoes applied before a signal qualifies: earnings within 5 trading days
(untradeable binary event), day-range > 25% (meme chaos), gap > 12% at the
high end (unfollowable), price < $5.

Top N per signal family are kept. Candidates are deduped against both the
researched-ledger and the recurring watchlist, so each ticker is deep-researched
exactly once ever, and the core list is never disturbed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

STATE_DIR = Path("~/.tradingagents/autotrade").expanduser()

# Fallback universe when the S&P 500 list can't be fetched.
CORE_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "JPM",
    "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "JNJ", "ABBV", "MRK", "CVX",
    "AMD", "NFLX", "CRM", "ADBE", "DIS", "PEP", "KO", "WMT", "BAC", "ORCL",
]

MOMENTUM_THRESHOLD = 0.08       # |1M return| to qualify as momentum
EXTENSION_CAP = 0.15            # % above 20DMA → too extended to chase
VOLUME_SPIKE_RATIO = 2.5        # volume / 30-day average
VOLUME_MOVE_FLOOR = 0.04        # min |1M return| for a spike to matter
EARNINGS_DAYS = 5               # veto window around earnings
RANGE_CAP = 0.25                # max day range (H-L)/C
GAP_CAP = 0.12                  # max gap from 20DMA at the high end
MIN_PRICE = 5.0


def sp500_universe() -> list[str]:
    """Fetch current S&P 500 constituents; fall back to a fixed core list."""
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        symbols = [s for s in tables[0]["Symbol"].tolist() if isinstance(s, str)]
        if len(symbols) > 400:
            return symbols
    except Exception:
        pass
    return CORE_UNIVERSE


def _vetoes(row: dict) -> list[str]:
    v = []
    if row["close"] < MIN_PRICE:
        v.append("penny")
    if row["earnings_within_days"] is not None and row["earnings_within_days"] <= EARNINGS_DAYS:
        v.append("earnings")
    if row["day_range_pct"] > RANGE_CAP:
        v.append("wild_range")
    if row["ext_pct"] > GAP_CAP:
        v.append("overextended_gap")
    return v


def scan_universe(
    universe: list[str] | None = None,
    batch_size: int = 80,
    top_n: int = 8,
) -> list[dict]:
    """Scan the universe and return ranked candidates with signals + vetoes."""
    tickers = universe or sp500_universe()
    frames = []
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        df = yf.download(
            batch, period="6mo", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        frames.append(df)
    px = pd.concat(frames, axis=1) if len(frames) > 1 else frames[0]

    candidates: list[dict] = []
    for t in tickers:
        try:
            one = px[t].dropna(subset=["Close"])
            if len(one) < 60:
                continue
            c = one["Close"]
            v = one["Volume"]
            close = float(c.iloc[-1])
            if close <= 0 or pd.isna(close):
                continue
            ret_1m = float(c.iloc[-1] / c.iloc[-21] - 1)
            ma20 = float(c.rolling(20).mean().iloc[-1])
            ma50 = float(c.rolling(50).mean().iloc[-1])
            vol_avg30 = float(v.rolling(30).mean().iloc[-1])
            vol_ratio = float(v.iloc[-1] / vol_avg30) if vol_avg30 > 0 else 0.0
            day_range = float((one["High"].iloc[-1] - one["Low"].iloc[-1]) / close)
            ext_pct = close / ma20 - 1

            row = {
                "ticker": t,
                "close": round(close, 2),
                "ret_1m": round(ret_1m, 4),
                "above_ma20": close > ma20,
                "ma20_gt_ma50": ma20 > ma50,
                "vol_ratio": round(vol_ratio, 2),
                "day_range_pct": round(day_range, 4),
                "ext_pct": round(ext_pct, 4),
                "earnings_within_days": None,  # filled by calendar check below
            }

            vetoes = _vetoes(row)
            signals = []
            if not vetoes or vetoes == ["overextended_gap"]:
                # overextension alone doesn't disqualify a *fade* signal
                if ret_1m >= MOMENTUM_THRESHOLD and row["above_ma20"] and row["ma20_gt_ma50"] and ext_pct <= EXTENSION_CAP:
                    signals.append("momentum_up")
                if ret_1m <= -MOMENTUM_THRESHOLD and not row["above_ma20"] and not row["ma20_gt_ma50"]:
                    signals.append("momentum_down")
                if vol_ratio >= VOLUME_SPIKE_RATIO and abs(ret_1m) >= VOLUME_MOVE_FLOOR:
                    signals.append("volume_spike")

            if signals:
                row["signals"] = signals
                row["vetoes"] = vetoes
                # rank key: strongest momentum × volume attention
                row["rank_score"] = round(abs(ret_1m) * (1 + max(0.0, vol_ratio - 1) / 2), 4)
                candidates.append(row)
        except (KeyError, IndexError, ZeroDivisionError, ValueError):
            continue

    # earnings veto via calendar (only for candidates that passed price filters)
    for row in candidates:
        try:
            cal = yf.Ticker(row["ticker"]).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                nxt = dates[0] if isinstance(dates, list) else dates
                days = (pd.Timestamp(nxt) - pd.Timestamp.now()).days
                row["earnings_within_days"] = int(days) if 0 <= days <= 30 else None
        except Exception:
            row["earnings_within_days"] = None
    still_in = [r for r in candidates if not any(v == "earnings" for v in _vetoes(r))]

    # top N per signal family
    def top(family: str) -> list[dict]:
        rows = [r for r in still_in if family in r["signals"]]
        rows.sort(key=lambda r: r["rank_score"], reverse=True)
        return rows[:top_n]

    selected = top("momentum_up") + top("momentum_down") + top("volume_spike")
    seen, out = set(), []
    for r in selected:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            out.append(r)
    return out


class ScreenerLedger:
    """Dedup ledger: each ticker may be deep-researched only once, ever."""

    def __init__(self, path: Path = STATE_DIR / "screener_ledger.json"):
        self.path = Path(path).expanduser()
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self.data = {}
        else:
            self.data = {}

    def already_researched(self, ticker: str) -> bool:
        return ticker in self.data

    def mark(self, ticker: str, signal: str, meta: dict) -> None:
        self.data[ticker] = {
            "signal": signal,
            "researched_at": datetime.now().isoformat(),
            **meta,
        }
        self.path.write_text(json.dumps(self.data, indent=2))

    def entries(self) -> dict:
        return dict(self.data)
