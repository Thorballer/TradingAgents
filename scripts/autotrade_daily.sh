#!/bin/bash
# TradingAgents AutoTrade — daily research + (dry-run) execution
# Loaded via ~/Library/LaunchAgents/com.dgold.tradingagents.plist

export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PATH"
cd "$HOME/TradingAgents"

exec "$HOME/TradingAgents/.venv/bin/python" autotrade_cli.py run-all NVDA AAPL
