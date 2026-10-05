"""
hypogeum/openrouter_drain.py — spend an old OpenRouter key's leftover balance
first, then switch to the main key on its own (operator, 2026-10-05: "use it
until its balance is spent, then move to the other one").

OPENROUTER_DRAIN_KEY in .env = the key to use up first. While it is set and
not marked exhausted, Config.OPENROUTER_API_KEY is the drain key and
Config.OPENROUTER_MAIN_KEY the real one. It is marked exhausted (a flag file,
so every running process sees it on its next Config()) when:
  * the operator-alert loop sees its balance under DRAIN_SWITCH_BELOW_USD, or
  * a call through it gets HTTP 402 — that call is retried once on the main
    key, so no stage fails because of the switch.
Delete OPENROUTER_DRAIN_KEY from .env any time after the switch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

FLAG = Path.home() / ".cortex" / "openrouter_drain_exhausted"
BASELINE = Path.home() / ".cortex" / "openrouter_drain_total_credits"
DRAIN_SWITCH_BELOW_USD = 1.5


def purchase_detected(total_credits: float) -> bool:
    """True if the drain account's total purchased credits went UP since we
    started using it -- i.e. its card was charged (auto top-up). The operator
    wants that account used only while it never charges the card, so the
    caller switches away at once and alerts. First call records the
    baseline."""
    try:
        if not BASELINE.exists():
            BASELINE.parent.mkdir(parents=True, exist_ok=True)
            BASELINE.write_text(f"{total_credits}\n")
            return False
        return total_credits > float(BASELINE.read_text().strip() or 0) + 0.01
    except Exception:  # noqa: BLE001
        return False


def is_exhausted() -> bool:
    try:
        return FLAG.exists()
    except Exception:  # noqa: BLE001
        return False


def mark_exhausted(reason: str) -> None:
    try:
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        if not FLAG.exists():
            FLAG.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n")
            from hypogeum.logger import get_logger
            get_logger(agent="openrouter_drain").info("openrouter_drain_key_exhausted", reason=reason)
    except Exception:  # noqa: BLE001
        pass
