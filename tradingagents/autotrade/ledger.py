"""Durable weekly token budget with hard cap and threshold alerts.

Counts input+output+cached-read tokens reported by the LLM provider and
persists to a JSON file so the budget survives crashes and restarts.

Design notes:
- Budget week is Monday 00:00 local time through Sunday.
- A cache read means tokens billed cheaply, but they still flow through the
  provider; we count them (cache tokens are also included) so the cap is a
  true ceiling on provider-side usage.
- Threshold alerts fire once per crossing per week (50/75/90 by default);
  the fired set is persisted with the usage so restarts don't re-spam.
- check_budget() is called BEFORE starting research and raises BudgetExceeded
  when the cap is already hit — a run that would blow the cap never starts.
- After a run, record_usage() re-checks the cap; when exceeded it raises
  BudgetExceeded and sets a ``halted`` flag the runner stores in state so the
  executor is skipped for any later run that week.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_ALERT_THRESHOLDS = (0.50, 0.75, 0.90)


def _week_start(now: datetime | None = None) -> datetime:
    """Monday 00:00 of the week containing ``now``."""
    now = now or datetime.now()
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


class BudgetExceeded(RuntimeError):
    """Raised when a cap would be (or has been) exceeded."""


class TokenLedger:
    """Persisted weekly token budget. All methods are thread-safe."""

    def __init__(
        self,
        state_path: str | Path,
        weekly_cap_tokens: int = 100_000_000,
        alert_thresholds: tuple[float, ...] = DEFAULT_ALERT_THRESHOLDS,
        alert_callback=None,
    ):
        self.path = Path(state_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.weekly_cap = int(weekly_cap_tokens)
        self.thresholds = tuple(sorted(alert_thresholds))
        self.alert_callback = alert_callback  # fn(pct_used, used, cap) -> None
        self._lock = threading.Lock()
        self._state = self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                pass  # corrupt file -> start a fresh ledger, cap still enforced
        return {"week_start": None, "used": 0, "fired": []}

    def _save_locked(self) -> None:
        self.path.write_text(json.dumps(self._state, indent=2))

    def _roll_week_locked(self) -> None:
        current_week = _week_start().isoformat()
        if self._state.get("week_start") != current_week:
            self._state = {"week_start": current_week, "used": 0, "fired": []}
            self._save_locked()

    # -- accounting ----------------------------------------------------------

    def record_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Add token usage, fire threshold alerts, enforce the hard cap.

        Raises BudgetExceeded when the cap is crossed (the usage that crossed
        it is still recorded — the number is the truth).
        """
        with self._lock:
            self._roll_week_locked()
            self._state["used"] += int(input_tokens) + int(output_tokens) + int(cache_read_tokens)
            used = self._state["used"]
            for t in self.thresholds:
                mark = f"{int(t * 100)}"
                if used >= self.weekly_cap * t and mark not in self._state["fired"]:
                    self._state["fired"].append(mark)
                    if self.alert_callback:
                        try:
                            self.alert_callback(t, used, self.weekly_cap)
                        except Exception:
                            pass  # alert delivery must never corrupt the ledger
            self._save_locked()
            if used > self.weekly_cap:
                raise BudgetExceeded(
                    f"Weekly token cap exceeded: {used:,} > {self.weekly_cap:,}"
                )

    def check_budget(self, margin_tokens: int = 0) -> None:
        """Raise BudgetExceeded if the remaining budget is below ``margin_tokens``.

        Call before starting an expensive run so a cap-hit run never starts.
        """
        with self._lock:
            self._roll_week_locked()
            used = self._state["used"]
        if used + margin_tokens > self.weekly_cap:
            raise BudgetExceeded(
                f"Weekly token cap reached: {used:,}/{self.weekly_cap:,} used"
                + (f" (needs ~{margin_tokens:,} more)" if margin_tokens else "")
            )

    # -- reporting -----------------------------------------------------------

    def usage(self) -> dict:
        with self._lock:
            self._roll_week_locked()
            used = self._state["used"]
        return {
            "used": used,
            "cap": self.weekly_cap,
            "pct": used / self.weekly_cap if self.weekly_cap else 0.0,
            "week_start": self._state["week_start"],
            "fired": list(self._state["fired"]),
            "remaining": max(0, self.weekly_cap - used),
        }

    def reset_week(self) -> None:
        """Manual override: zero the current week (does not change the cap)."""
        with self._lock:
            self._state = {"week_start": _week_start().isoformat(), "used": 0, "fired": []}
            self._save_locked()
