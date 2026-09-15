# TradingAgents AutoTrade Fork

Fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(framework credit to them) with an **auto-trading layer** bolted on: the original
researches a ticker with a multi-agent LLM pipeline and *suggests* a trade — this
fork can place the trade.

Built for the **Z.ai GLM Coding Plan** (uses the dedicated coding endpoint, so it
runs on plan credits, not pay-as-you-go balance).

## New: `tradingagents/autotrade/`

| Module | What it does |
|---|---|
| `ledger.py` | **Weekly token budget** (default 100M, Mon–Sun). Durable JSON state, hard cap that halts runs, alerts at 50/75/90% via iMessage. |
| `callbacks.py` | LangChain callback wired into every LLM call in the graph → ledger. Counts input+output+cached tokens as reported by Z.AI. |
| `research_cache.py` | **Per-day LLM decision cache.** Re-running a ticker the same day (or within TTL) costs ~0 tokens. Depth changes auto-invalidate. Market data was already cached by upstream; this caches the *research outcome*. |
| `executor.py` | **Alpaca execution.** 5-tier rating → orders. Paper by default; live needs `DRY_RUN=0` **and** `ALPACA_LIVE=1`. Stops ride exchange-side in bracket orders; watchlist-gated; whole shares; max 10% equity/symbol. |
| `notifier.py` | iMessage alerts via `imsg` CLI: threshold warnings, per-run summaries (signal, tokens, action). |
| `runner.py` | Orchestrates: budget gate → cache → research → ledger → execute → notify → JSON run log. |

CLI: `autotrade_cli.py` (run / run-all / budget / cache).

## Quick start

```bash
cd ~/TradingAgents
cp .env.example .env.local   # or put TRADINGAGENTS_* keys in ~/.hermes/.env
.venv/bin/python autotrade_cli.py run NVDA
.venv/bin/python autotrade_cli.py budget
.venv/bin/python autotrade_cli.py cache
```

`autotrade_cli.py` reads `TRADINGAGENTS_*`, `GLM_API_KEY`, `ALPACA_*` from the
environment or `~/.hermes/.env`. It maps `GLM_API_KEY` → `ZHIPU_API_KEY`
(what the glm provider expects) and points at the coding endpoint automatically.

## Controlling depth and time

- **Depth**: `TRADINGAGENTS_ANALYSTS` (drop/add analysts), `TRADINGAGENTS_DEBATE_ROUNDS`
  and `TRADINGAGENTS_RISK_ROUNDS` (1 = light, 2+ = deep), and the model choice
  (`glm-5.3-flash` for cheap/fast vs `glm-5.3` for the flagship).
- **Time/cadence**: `TRADINGAGENTS_RESEARCH_TTL_HOURS` (decision-cache window) plus
  how often the cron fires. Cache makes frequent runs nearly free.
- **Cost ceiling**: `TRADINGAGENTS_WEEKLY_TOKEN_CAP` (default 100,000,000). When the
  cap is hit mid-run, the ledger raises immediately and the executor is skipped;
  a run that would start over-cap never begins. Alerts at 50/75/90%.
- **Risk**: `TRADINGAGENTS_DRY_RUN=1` logs orders without placing them (default);
  `TRADINGAGENTS_ALPACA_LIVE=1` additionally required for real money.
  `TRADINGAGENTS_MAX_POSITION_PCT` caps per-symbol exposure.

## Safety rails

- Watchlist-gated: symbols not in `TRADINGAGENTS_WATCHLIST` are never touched.
- Foreign/manual positions are invisible to the executor (it only ever touches
  watchlist symbols).
- `Hold` and `REVIEW` signals never trade. `Sell/Underweight` only flatten this
  system's own position in that symbol.
- Budget halts are fail-closed: over cap ⇒ no research, no orders.
- Paper endpoint unless two independent switches are flipped.

## Scheduling

`com.dgold.tradingagents` LaunchAgent (see repo root) runs weekdays after the open
by default. Edit `TRADINGAGENTS_WATCHLIST` in `~/.hermes/.env` to change coverage.
Manual trigger: `launchctl kickstart gui/$(id -u)/com.dgold.tradingagents`.

## Upstream

`git remote add upstream https://github.com/TauricResearch/TradingAgents.git` is
configured; `git fetch upstream` to pull upstream improvements. All fork changes
live in `tradingagents/autotrade/` + `autotrade_cli.py` — upstream merges stay clean.
