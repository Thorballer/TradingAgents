"""Per-day LLM decision cache.

TradingAgents already caches market data per day (CSVs under data_cache_dir),
but every run still burns tokens re-running the full agent graph. This cache
persists the *research outcome* (reports + decision + signal) keyed by
(ticker, trade_date, depth fingerprint) so re-runs within the research TTL are
free. Cache writes happen only after a successful graph run; a cached result
records zero LLM tokens.

The fingerprint includes every knob that changes research depth/behavior, so
turning depth up correctly invalidates shallow cached results.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

DEFAULT_TTL_HOURS = 20  # ~ one trading day; overnight runs see fresh sessions


def depth_fingerprint(cfg: dict) -> str:
    """Stable hash of the research-depth-affecting config keys."""
    keys = {
        "analysts": sorted(cfg.get("selected_analysts", [])),
        "max_debate_rounds": cfg.get("max_debate_rounds"),
        "max_risk_rounds": cfg.get("max_risk_discuss_rounds"),
        "deep_llm": cfg.get("deep_think_llm"),
        "quick_llm": cfg.get("quick_think_llm"),
        "provider": cfg.get("llm_provider"),
        "news_limit": cfg.get("news_article_limit"),
        "global_news_limit": cfg.get("global_news_article_limit"),
        "global_news_days": cfg.get("global_news_lookback_days"),
        "output_language": cfg.get("output_language"),
    }
    blob = json.dumps(keys, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class ResearchCache:
    def __init__(self, cache_dir: str | Path, ttl_hours: float = DEFAULT_TTL_HOURS):
        self.dir = Path(cache_dir).expanduser() / "decisions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_hours = ttl_hours

    def _path(self, ticker: str, trade_date: str, fingerprint: str) -> Path:
        safe_ticker = "".join(c for c in ticker.upper() if c.isalnum() or c in "-.")
        return self.dir / f"{safe_ticker}_{trade_date}_{fingerprint}.json"

    def get(self, ticker: str, trade_date: str, fingerprint: str):
        p = self._path(ticker, trade_date, fingerprint)
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        written = datetime.fromisoformat(rec["written_at"])
        age_h = (datetime.now() - written).total_seconds() / 3600
        if age_h > self.ttl_hours:
            return None
        rec["cache_hit"] = True
        rec["cache_age_hours"] = round(age_h, 2)
        return rec

    def put(
        self,
        ticker: str,
        trade_date: str,
        fingerprint: str,
        signal: str,
        final_decision_text: str,
        trader_plan_text: str,
        reports: dict,
        tokens_used: int,
    ) -> None:
        rec = {
            "ticker": ticker,
            "trade_date": trade_date,
            "fingerprint": fingerprint,
            "signal": signal,
            "final_decision": final_decision_text,
            "trader_plan": trader_plan_text,
            "reports": reports,
            "tokens_used": tokens_used,
            "written_at": datetime.now().isoformat(),
        }
        self._path(ticker, trade_date, fingerprint).write_text(json.dumps(rec, indent=2))
