"""iMessage notifications via the `imsg` CLI (macOS)."""

from __future__ import annotations

import subprocess


class ImsgNotifier:
    def __init__(self, to: str, enabled: bool = True):
        self.to = to
        self.enabled = enabled

    def send(self, text: str) -> bool:
        if not self.enabled or not self.to:
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


def make_alert_callback(notifier: ImsgNotifier):
    """Build the ledger alert callback: fires at 50/75/90% (and 100% via cap)."""

    def _alert(pct: float, used: int, cap: int) -> None:
        bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
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
