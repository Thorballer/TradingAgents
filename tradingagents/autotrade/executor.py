"""Alpaca execution layer: maps the 5-tier research rating onto real orders.

Safety model (mirrors the house trading-system rules):
- PAPER endpoint by default. Live trading requires TRADINGAGENTS_ALPACA_LIVE=1
  AND dry_run disabled in config — two independent switches.
- Bracket orders put the stop exchange-side (the bot can crash; the stop must
  not). stop_limit leg uses a small collar below the stop price.
- Only symbols in this system's watchlist are ever touched. Positions in
  foreign/manual symbols are invisible to this executor.
- Hold / REVIEW signals are no-ops. Sell/Underweight close our position in
  that symbol (no leg math on the sell side).
- Whole shares only: Alpaca bracket orders don't support notional/fractional.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"


def _load_keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if key and secret:
        return key, secret
    env_file = Path("~/.hermes/.env").expanduser()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ALPACA_API_KEY=") and not key:
                key = line.split("=", 1)[1].strip()
            elif line.startswith("ALPACA_SECRET_KEY=") and not secret:
                secret = line.split("=", 1)[1].strip()
    return key, secret


class AlpacaExecutor:
    def __init__(
        self,
        watchlist: list[str],
        max_position_pct: float = 0.10,
        dry_run: bool = True,
        live: bool | None = None,
        timeout: int = 30,
    ):
        self.watchlist = {s.upper() for s in watchlist}
        self.max_position_pct = max_position_pct
        self.dry_run = dry_run
        self.live = live if live is not None else os.environ.get("TRADINGAGENTS_ALPACA_LIVE") == "1"
        self.base = LIVE_BASE if self.live else PAPER_BASE
        self.timeout = timeout
        self.key, self.secret = _load_keys()
        if not self.key or not self.secret:
            raise RuntimeError("Alpaca credentials not found (ALPACA_API_KEY/ALPACA_SECRET_KEY)")

    # -- low-level -----------------------------------------------------------

    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def _req(self, method: str, path: str, base: str | None = None, **kw) -> dict:
        url = (base or self.base) + path
        r = requests.request(
            method, url, headers=self._headers(), timeout=self.timeout, **kw
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    # -- account / positions --------------------------------------------------

    def get_equity(self) -> float:
        acct = self._req("GET", "/v2/account")
        return float(acct.get("equity", 0))

    def get_position(self, symbol: str) -> dict | None:
        try:
            return self._req("GET", f"/v2/positions/{symbol.upper()}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def last_price(self, symbol: str) -> float:
        data = self._req("GET", f"/v2/stocks/{symbol}/trades/latest", base=DATA_BASE)
        return float(data["trade"]["p"])

    # -- orders ----------------------------------------------------------------

    def submit_bracket_buy(self, symbol: str, ref_price: float, stop_price: float | None) -> dict:
        """Market buy + exchange-side stop. Returns the order dict."""
        equity = self.get_equity()
        budget = equity * self.max_position_pct
        qty = int(budget / ref_price)
        if qty < 1:
            raise RuntimeError(
                f"{symbol}: budget ${budget:.2f} too small for 1 share at ${ref_price:.2f}"
            )

        order = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": round(ref_price * 1.001, 2)},
        }
        if stop_price and 0 < stop_price < ref_price:
            order["stop_loss"] = {
                "stop_price": round(stop_price, 2),
                "limit_price": round(stop_price * 0.98, 2),
            }

        if self.dry_run:
            return {"dry_run": True, "would_submit": order, "budget": budget, "equity": equity}

        placed = self._req("POST", "/v2/orders", json=order)
        return {"dry_run": False, "order": placed}

    def close_position(self, symbol: str) -> dict:
        """Flatten our position in this symbol (exchange handles the rest)."""
        if self.dry_run:
            pos = self.get_position(symbol)
            if not pos:
                return {"dry_run": True, "would_close": None, "note": "no position"}
            return {"dry_run": True, "would_close": {"qty": pos.get("qty"), "symbol": symbol}}
        try:
            result = self._req("DELETE", f"/v2/positions/{symbol}")
            return {"dry_run": False, "closed": result or {"symbol": symbol}}
        except RuntimeError as e:
            if "404" in str(e):
                return {"dry_run": False, "closed": None, "note": "no position"}
            raise

    # -- rating dispatch ---------------------------------------------------------

    def execute_signal(self, symbol: str, signal: str, entry_price: float | None,
                       stop_loss: float | None, current_price: float | None = None) -> dict:
        """Map a 5-tier rating + trader levels onto an action. Idempotent-ish:
        buying when already long is allowed (adds), selling without a position is a no-op."""
        symbol = symbol.upper()
        if symbol not in self.watchlist:
            return {"action": "skipped", "reason": f"{symbol} not in watchlist"}
        if signal in ("Hold", "REVIEW"):
            return {"action": "hold", "reason": f"signal={signal}"}

        if signal in ("Sell", "Underweight"):
            res = self.close_position(symbol)
            res["action"] = "close" if res.get("would_close") or res.get("closed") else "none"
            res["signal"] = signal
            return res

        if signal == "Buy" or signal == "Overweight":
            ref = entry_price or current_price or self.last_price(symbol)
            stop = stop_loss if (stop_loss and stop_loss < ref) else None
            res = self.submit_bracket_buy(symbol, ref_price=ref, stop_price=stop)
            res["action"] = "buy"
            res["signal"] = signal
            res["ref_price"] = ref
            res["stop_price"] = stop
            return res

        return {"action": "skipped", "reason": f"unknown signal {signal!r}"}
