"""
tests/test_dashboard_humanize.py — hypogeum.dashboard_humanize.signal_strength_words().

Regression (2026-09-05): this filter is called from learning.html for
EVERY pending learning_action's evidence.avg_signal_strength -- one real
action type (content_volume, see hypogeum.learning_engine.
generate_cadence_recommendation) has never carried that key, and Jinja2's
own Undefined object raises its OWN UndefinedError from __float__, never
TypeError/ValueError, so the original `except (TypeError, ValueError)`
never caught it -- /intelligence/learning 500'd outright the first time a
channel ever produced that action (this system's whole pending-action
list had always been empty before that point, so the bug was dormant
since this file was written).
"""

from __future__ import annotations

from hypogeum.dashboard_humanize import signal_strength_words


class _JinjaLikeUndefined:
    """Stands in for jinja2.Undefined: raises on the exact operation this
    filter performs (float()), but not TypeError/ValueError -- the whole
    point of this regression."""

    def __float__(self):
        raise RuntimeError("jinja2.exceptions.UndefinedError (simulated)")


def test_undefined_like_value_returns_safe_default_not_a_crash():
    result = signal_strength_words(_JinjaLikeUndefined())
    assert result == {"label": "", "level": 0, "direction": "flat"}


def test_none_returns_safe_default():
    assert signal_strength_words(None) == {"label": "", "level": 0, "direction": "flat"}


def test_strong_positive_signal():
    result = signal_strength_words(0.5)
    assert result["direction"] == "up"
    assert result["level"] == 3
    assert "positive" in result["label"]


def test_strong_negative_signal():
    result = signal_strength_words(-0.5)
    assert result["direction"] == "down"
    assert result["level"] == 3
    assert "negative" in result["label"]


def test_faint_signal():
    result = signal_strength_words(0.01)
    assert result["level"] == 0
