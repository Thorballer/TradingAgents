"""Auto-trading layer for TradingAgents.

Adds on top of the research framework (which only *suggests* trades):
- TokenLedger: durable weekly token budget with hard cap + threshold alerts
- LedgerCallbackHandler: LangChain callback that feeds the ledger
- research_cache: per-day LLM decision cache (market data is already cached)
- AlpacaExecutor: maps the 5-tier rating onto bracket orders (paper default)
- ImsgNotifier: iMessage notifications via the `imsg` CLI
- AutoTradeRunner: orchestrates research -> decision -> execution
"""

from .ledger import TokenLedger
from .callbacks import LedgerCallbackHandler
from .research_cache import ResearchCache, depth_fingerprint
from .executor import AlpacaExecutor
from .notifier import ImsgNotifier
from .runner import AutoTradeRunner

__all__ = [
    "TokenLedger",
    "LedgerCallbackHandler",
    "ResearchCache",
    "depth_fingerprint",
    "AlpacaExecutor",
    "ImsgNotifier",
    "AutoTradeRunner",
]
