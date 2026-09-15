"""LangChain callback handler that feeds every LLM call into the TokenLedger.

TradingAgentsGraph forwards ``callbacks`` into each provider client's LLM
constructor (llm_kwargs["callbacks"]), so one handler instance captures
token usage from every agent in the graph (analysts, researchers, debates,
managers, trader, risk team, PM).
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

# ChatOpenAI reports usage on the LLMEnd; chunked streaming paths report on
# each chunk, so guard double-counting by keying on run_id.
from .ledger import TokenLedger


class LedgerCallbackHandler(BaseCallbackHandler):
    def __init__(self, ledger: TokenLedger):
        self.ledger = ledger
        self._seen_run_ids: set[str] = set()
        self.runs: list[dict] = []

    def on_llm_end(self, response, *, run_id, **kwargs: Any) -> None:
        try:
            key = str(run_id)
            if key in self._seen_run_ids:
                return
            self._seen_run_ids.add(key)

            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            if not usage and hasattr(response, "generations"):
                # Some providers only attach usage metadata per generation.
                for gen_list in response.generations:
                    for gen in gen_list:
                        meta = getattr(gen, "message", None)
                        meta = getattr(meta, "usage_metadata", None) if meta else None
                        if meta:
                            usage = meta
                            break
                    if usage:
                        break

            def _pick(*names: str) -> int:
                for n in names:
                    v = usage.get(n)
                    if isinstance(v, (int, float)):
                        return int(v)
                return 0

            inp = _pick("prompt_tokens", "input_tokens")
            out = _pick("completion_tokens", "output_tokens")
            cache_read = _pick("cache_read_input_tokens", "cached_tokens", "cache_read_tokens")

            self.runs.append(
                {"input": inp, "output": out, "cache_read": cache_read}
            )
            self.ledger.record_usage(
                input_tokens=inp, output_tokens=out, cache_read_tokens=cache_read
            )
        except Exception:
            # Token accounting must never crash a research run mid-flight;
            # the pre/post-run ledger checks bound any missed accounting.
            pass
