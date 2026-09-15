#!/bin/bash
# TradingAgents AutoTrade — daily research + (dry-run) execution
# Loaded via ~/Library/LaunchAgents/com.dgold.tradingagents.plist
# Runs the recurring watchlist + episodic screener holds; the screener scan
# picks new candidates (researched once, buy-once, Sell-check after).

export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PATH"
export TRADINGAGENTS_SCREENER=1
cd "$HOME/TradingAgents"

exec "$HOME/TradingAgents/.venv/bin/python" autotrade_cli.py run
