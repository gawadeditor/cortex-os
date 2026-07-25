"""
hypogeum/failure_policy.py — Failure classification for queue-level auto-retry.

A tiny, dependency-free enum. Exception-defining modules (pipeline.py,
worker_integration.py) set FailurePolicy.TERMINAL on classes that
represent a deterministic content/configuration decision no retry can
change. hypogeum.queue reads it via getattr(exc, "failure_policy",
FailurePolicy.TRANSIENT) — it never imports or names a concrete
exception type, keeping the queue provider-agnostic.
"""

from __future__ import annotations

from enum import Enum


class FailurePolicy(Enum):
    """TRANSIENT (default) is safe to auto-retry, bounded. TERMINAL never is."""
    TRANSIENT = "transient"
    TERMINAL = "terminal"
