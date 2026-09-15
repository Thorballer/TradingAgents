"""AutoTradeRunner: research -> decision -> (optionally) execute.

One object, one method. Built for cron:

    runner = AutoTradeRunner.from_env()
    result = runner.run("NVDA")

Controls:
- Depth: selected_analysts, max_debate_rounds, max_risk_discuss_rounds
- Time:  research_ttl_hours (cache reuse window) — cron frequency sets cadence
- Cost:  weekly token cap w/ alerts (TokenLedger) + LLM decision cache
- Risk:  dry_run default ON, paper endpoint default, watchlist gate
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from .callbacks import LedgerCallbackHandler
from .executor import AlpacaExecutor
from .ledger import BudgetExceeded, TokenLedger
from .notifier import default_notifier, make_alert_callback
from .research_cache import ResearchCache, depth_fingerprint
from .screener import STATE_DIR as SCREENER_STATE_DIR, ScreenerLedger, scan_universe

DEFAULT_STATE_DIR = Path("~/.tradingagents/autotrade").expanduser()
WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri: screener runs on trading days only
ET = ZoneInfo("America/New_York")

# Research must START inside this window. Ends well before the close because
# the order is placed AFTER research finishes (~25-40 min): a start after
# ~14:45 could land its market order at/after the close, where it queues for
# the next open — the exact double-buy trap this guard exists to prevent.
RUN_WINDOW = (dtime(9, 35), dtime(14, 45))


def market_hours_ignored() -> bool:
    """Manual-override escape hatch (TRADINGAGENTS_IGNORE_MARKET_HOURS=1)."""
    return os.environ.get("TRADINGAGENTS_IGNORE_MARKET_HOURS") == "1"


def market_phase(now: datetime | None = None) -> str:
    """US equity session phase in ET: 'pre' | 'open' | 'post' | 'closed'."""
    now = now or datetime.now(ET)
    if now.weekday() > 4:
        return "closed"
    t = now.time()
    if dtime(9, 30) <= t < dtime(16, 0):
        return "open"
    if dtime(4, 0) <= t < dtime(9, 30):
        return "pre"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "post"
    return "closed"


def in_run_window(now: datetime | None = None) -> bool:
    """True when a research+trade run may START (weekday, regular session)."""
    now = now or datetime.now(ET)
    return market_phase(now) == "open" and RUN_WINDOW[0] <= now.time() < RUN_WINDOW[1]


EPISODIC_PATH = DEFAULT_STATE_DIR / "episodic_watchlist.json"


def _load_episodic() -> list[str]:
    """Symbols picked by the screener (researched once, held until stopped out)."""
    if EPISODIC_PATH.exists():
        try:
            return json.loads(EPISODIC_PATH.read_text()).get("symbols", [])
        except json.JSONDecodeError:
            pass
    return []


def _remember_episodic(symbol: str) -> None:
    symbols = _load_episodic()
    if symbol not in symbols:
        symbols.append(symbol)
    EPISODIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    EPISODIC_PATH.write_text(json.dumps({"symbols": symbols}, indent=2))


def _forget_episodic(symbol: str) -> None:
    """Drop a symbol once it's been closed out (Sell rating, flat or filled)."""
    symbols = [s for s in _load_episodic() if s != symbol]
    EPISODIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    EPISODIC_PATH.write_text(json.dumps({"symbols": symbols}, indent=2))


class AutoTradeRunner:
    def __init__(
        self,
        config: dict,
        ledger: TokenLedger,
        cache: ResearchCache,
        notifier,
        executor: AlpacaExecutor | None,
        results_dir: Path = DEFAULT_STATE_DIR / "runs",
    ):
        self.config = config
        self.ledger = ledger
        self.cache = cache
        self.notifier = notifier
        self.executor = executor
        self.results_dir = Path(results_dir).expanduser()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.fingerprint = depth_fingerprint(config)
        self.graph: TradingAgentsGraph | None = None
        self.handler: LedgerCallbackHandler | None = None
        # Screener (set by from_env when enabled)
        self.screener_enabled: bool = False
        self.screener_top_n: int = 2
        self.screener_ledger = ScreenerLedger()

    # ------------------------------------------------------------------ setup

    @classmethod
    def from_env(
        cls,
        watchlist: list[str] | None = None,
        analysts: list[str] | None = None,
        debate_rounds: int = 1,
        risk_rounds: int = 1,
        weekly_cap_tokens: int = 100_000_000,
        research_ttl_hours: float = 20,
        dry_run: bool = True,
        max_position_pct: float = 0.10,
        imsg_to: str | None = None,
        screener: bool = False,
        screener_top_n: int = 2,
    ) -> "AutoTradeRunner":
        cfg = DEFAULT_CONFIG.copy()
        cfg["llm_provider"] = os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "glm")
        # glm provider reads ZHIPU_API_KEY; GLM_API_KEY is accepted as an alias.
        if os.environ.get("GLM_API_KEY") and not os.environ.get("ZHIPU_API_KEY"):
            os.environ["ZHIPU_API_KEY"] = os.environ["GLM_API_KEY"]
        # GLM Coding Plan keys only work on the dedicated coding endpoint;
        # the general /api/paas/v4 rejects them with 1113 (insufficient balance).
        cfg["backend_url"] = os.environ.get(
            "TRADINGAGENTS_LLM_BACKEND_URL", "https://api.z.ai/api/coding/paas/v4"
        )
        cfg["deep_think_llm"] = os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "glm-4.7")
        cfg["quick_think_llm"] = os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "glm-5.3-flash")
        # Ride out bursty Z.AI 429s ("temporarily overloaded") with SDK retries.
        cfg["llm_max_retries"] = int(os.environ.get("TRADINGAGENTS_LLM_MAX_RETRIES", "6"))
        cfg["max_debate_rounds"] = debate_rounds
        cfg["max_risk_discuss_rounds"] = risk_rounds
        if analysts:
            cfg["_selected_analysts"] = analysts

        state_dir = DEFAULT_STATE_DIR
        notifier = default_notifier(imsg_to=imsg_to)
        ledger = TokenLedger(
            state_path=state_dir / "ledger.json",
            weekly_cap_tokens=weekly_cap_tokens,
            alert_callback=make_alert_callback(notifier),
        )
        cache = ResearchCache(state_dir / "research_cache", ttl_hours=research_ttl_hours)
        executor = None
        if watchlist:
            executor = AlpacaExecutor(
                watchlist=watchlist, dry_run=dry_run, max_position_pct=max_position_pct
            )
        runner = cls(cfg, ledger, cache, notifier, executor)
        if runner.executor is not None:
            # Screener picks stay manageable across sessions (Sell ratings can
            # close them) without joining the recurring watchlist.
            runner.executor.watchlist |= set(_load_episodic())
        runner.screener_enabled = screener
        runner.screener_top_n = screener_top_n
        runner.screener_ledger = ScreenerLedger()
        return runner

    # ------------------------------------------------------------- screener

    def run_screener(self) -> list[dict]:
        """Scan for movers and deep-research the top new candidates (once each).

        Returns the list of candidate scan rows that were researched. A ticker
        picked here never joins the recurring watchlist — it is researched,
        traded if the rating is directional, and then held until its stop or a
        future Sell rating on a manual re-run. Dedup is permanent (ledger) and
        also respects the recurring watchlist. Weekends are skipped (scan data
        would be stale) and scans only run during market hours.
        """
        if not in_run_window():
            return []
        try:
            candidates = scan_universe(top_n=self.screener_top_n)
        except Exception as e:
            self.notifier.send(f"⚠️ Screener failed: {type(e).__name__}: {e}")
            return []

        recurring = {s.upper() for s in (self.executor.watchlist if self.executor else [])}
        fresh = [
            c for c in candidates
            if c["ticker"] not in recurring and not self.screener_ledger.already_researched(c["ticker"])
        ]

        # Budget-gate BEFORE burning tokens: estimate a worst-case run (~300k)
        # per candidate and stop admitting once the cap would be at risk.
        results = []
        for cand in fresh:
            try:
                self.ledger.check_budget(margin_tokens=300_000)
            except BudgetExceeded:
                self.notifier.send(
                    f"🛑 Screener paused — token budget too low for a new research run "
                    f"({self.ledger.usage()['used']:,}/{self.ledger.usage()['cap']:,})."
                )
                break
            t = cand["ticker"]
            # Make the pick tradable in this session BEFORE its run (research
            # and execution happen inside self.run). Rolled back on failure.
            if self.executor is not None:
                self.executor.watchlist.add(t)
            res = self.run(t, _from_screener=True)
            ok = res.get("status") in ("researched", "cache_hit")
            if ok:
                _remember_episodic(t)
                self.screener_ledger.mark(
                    t,
                    ",".join(cand["signals"]),
                    {
                        "research_signal": res.get("signal"),
                        "close": cand["close"],
                        "ret_1m": cand["ret_1m"],
                        "vol_ratio": cand["vol_ratio"],
                        "tokens_used": res.get("tokens_used", 0),
                    },
                )
            results.append({**cand, "run": {k: res.get(k) for k in ("status", "signal", "tokens_used")}})
        return results

    def _build_graph(self) -> tuple[TradingAgentsGraph, LedgerCallbackHandler]:
        """Build the graph with the ledger callback attached."""
        cfg = self.config.copy()
        analysts = cfg.pop("_selected_analysts", None) or ("market", "news", "fundamentals")
        handler = LedgerCallbackHandler(self.ledger)
        graph = TradingAgentsGraph(
            selected_analysts=tuple(analysts), config=cfg, callbacks=[handler]
        )
        return graph, handler

    # ------------------------------------------------------------------- run

    def run(self, ticker: str, trade_date: str | None = None,
            allow_buy: bool = True, _from_screener: bool = False) -> dict:
        """Research one ticker and optionally execute. Returns a result dict.

        Episodic symbols (screener picks) are buy-once: only the screener run
        that picked them may open a position; every later run is a Sell-check
        that may close but never add. Runs may only START inside the market
        hours window (weekdays 9:35am–3:30pm ET) — never pre/after hours, so
        orders can't queue for the next open and double-fire.
        """
        ticker = ticker.upper()
        if not in_run_window() and not market_hours_ignored():
            phase = market_phase()
            result = {
                "ticker": ticker,
                "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
                "status": "skipped_market_closed",
                "phase": phase,
                "note": f"runs start only weekdays 9:35am-2:45pm ET (now: {phase})",
            }
            self._finish(result, notify=False)
            return result
        if (not _from_screener
                and ticker in {s.upper() for s in _load_episodic()}):
            allow_buy = False
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        started = time.time()
        result: dict = {
            "ticker": ticker,
            "trade_date": trade_date,
            "fingerprint": self.fingerprint,
            "started_at": datetime.now().isoformat(),
        }

        # 1. Budget gate — a cap-hit run never starts.
        try:
            self.ledger.check_budget()
        except BudgetExceeded as e:
            result.update(status="halted_budget", error=str(e))
            self._finish(result, notify=True)
            return result

        # 2. Research cache.
        cached = self.cache.get(ticker, trade_date, self.fingerprint)
        if cached:
            signal = cached["signal"]
            result.update(
                status="cache_hit",
                signal=signal,
                decision=cached["final_decision"],
                trader_plan=cached["trader_plan"],
                tokens_used=0,
            )
        else:
            if self.graph is None or self.handler is None:
                self.graph, self.handler = self._build_graph()
            graph = self.graph
            before = self.ledger.usage()["used"]
            try:
                _final_state, signal = graph.propagate(ticker, trade_date)
            except BudgetExceeded as e:
                result.update(status="halted_budget_midrun", error=str(e))
                self._finish(result, notify=True)
                return result
            except Exception as e:
                # Unattended runs must never fail silently (cron can't see a traceback).
                result.update(status="error", error=f"{type(e).__name__}: {e}")
                self._finish(result, notify=True)
                return result
            tokens_used = self.ledger.usage()["used"] - before
            state = graph.curr_state
            result.update(
                status="researched",
                signal=signal,
                decision=state.get("final_trade_decision", ""),
                trader_plan=state.get("trader_investment_plan", ""),
                tokens_used=tokens_used,
            )
            result["reports"] = {
                k: state.get(k, "")
                for k in ("market_report", "sentiment_report", "news_report", "fundamentals_report")
            }
            self.cache.put(
                ticker=ticker,
                trade_date=trade_date,
                fingerprint=self.fingerprint,
                signal=signal,
                final_decision_text=result["decision"],
                trader_plan_text=result["trader_plan"],
                reports=result["reports"],
                tokens_used=tokens_used,
            )

        result["budget_after"] = self.ledger.usage()

        # 3. Execution.
        if self.executor:
            entry, stop = _parse_levels(result.get("trader_plan") or "")
            try:
                result["execution"] = self.executor.execute_signal(
                    ticker, signal, entry_price=entry, stop_loss=stop,
                    allow_buy=allow_buy,
                )
                # Episodic symbol closed out (or flat on a Sell) → forget it.
                if (signal in ("Sell", "Underweight")
                        and result["execution"].get("action") in ("none", "close")):
                    _forget_episodic(ticker)
            except Exception as e:  # order rejections must not lose the research
                result["execution"] = {"action": "error", "error": str(e)}
        else:
            result["execution"] = {"action": "none", "reason": "no executor (observe mode)"}

        result["elapsed_s"] = round(time.time() - started, 1)
        self._finish(result, notify=True)
        return result

    # ------------------------------------------------------------------ misc

    def _finish(self, result: dict, notify: bool) -> None:
        path = self.results_dir / f"{result.get('ticker', 'run')}_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(result, indent=2, default=str))
        result["result_path"] = str(path)
        if notify:
            self.notifier.send(_summary_text(result))


def _parse_levels(trader_plan: str) -> tuple[float | None, float | None]:
    """Pull Entry Price / Stop Loss out of the rendered trader proposal."""
    import re

    def _grab(label: str) -> float | None:
        m = re.search(rf"\*\*{label}\*\*:\s*\$?([\d,]+\.?\d*)", trader_plan, re.IGNORECASE)
        return float(m.group(1).replace(",", "")) if m else None

    return _grab("Entry Price"), _grab("Stop Loss")


def _summary_text(r: dict) -> str:
    if r.get("status", "").startswith("halted"):
        return f"🛑 {r['ticker']} research halted: {r.get('error', 'budget cap')}"
    sig = r.get("signal", "?")
    icon = {"Buy": "🟢", "Overweight": "🟢", "Hold": "🟡", "Underweight": "🔴", "Sell": "🔴"}.get(sig, "⚪️")
    lines = [f"{icon} {r['ticker']} → {sig} ({r.get('status', '?')})"]
    tok = r.get("tokens_used", 0)
    if tok:
        b = r.get("budget_after", {})
        lines.append(f"Tokens: {tok:,} this run · week {b.get('pct', 0) * 100:.1f}% of cap")
    ex = r.get("execution") or {}
    lines.append(f"Action: {ex.get('action', 'none')}")
    if ex.get("dry_run"):
        lines.append("(dry-run — no real order)")
    return "\n".join(lines)
