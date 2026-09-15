"""Notifications: Telegram primary, iMessage fallback via `imsg` CLI.

Telegram is the dependable path from a headless Mac (the imsg CLI can hang
sending to the owner's own cell number; Telegram bot delivery is instant and
was verified live). Both are best-effort — alert delivery must never crash
a trading run.
"""

from __future__ import annotations

import os
import subprocess

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": text},
                timeout=15,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False


class ImsgNotifier:
    def __init__(self, to: str, enabled: bool = True):
        self.to = to
        self.enabled = enabled and bool(to)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            proc = subprocess.run(
                ["imsg", "send", "--to", self.to, "--text", text],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


class ChainNotifier:
    """Try each notifier in order; succeed on the first that delivers."""

    def __init__(self, notifiers: list):
        self.notifiers = [n for n in notifiers if n is not None]

    def send(self, text: str) -> bool:
        return any(n.send(text) for n in self.notifiers)


def default_notifier(imsg_to: str | None = None) -> ChainNotifier:
    """Telegram (from env/~/hermes secrets) with optional iMessage fallback."""
    from pathlib import Path

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TRADINGAGENTS_TELEGRAM_CHAT_ID", "")
    if not token:
        env_file = Path("~/.hermes/.env").expanduser()
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    if not chat_id:
        chat_id = "8498097115"  # Daniel's Telegram home channel
    return ChainNotifier([
        TelegramNotifier(token, chat_id),
        ImsgNotifier(to=imsg_to or os.environ.get("TRADINGAGENTS_IMSG_TO", "")),
    ])


def make_alert_callback(notifier):
    """Build the ledger alert callback: fires at 50/75/90% (and 100% via cap)."""

    def _alert(pct: float, used: int, cap: int) -> None:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        msg = (
            f"⚠️ TradingAgents token budget: {int(pct * 100)}% used\n"
            f"{bar}\n"
            f"{used:,} / {cap:,} tokens this week\n"
            f"Remaining: {max(0, cap - used):,}"
        )
        if pct >= 0.90:
            msg += "\nResearch will halt at 100%."
        notifier.send(msg)

    return _alert
