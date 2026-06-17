"""
hypogeum/decision_inputs — F2: Real Decision Inputs Layer

Collects and normalises real operational facts for future decision logic.
No recommendation engine. No mock data treated as truth.

Public API:
    get_decision_inputs_state(db_path=None) -> dict
"""

from hypogeum.decision_inputs.state import get_decision_inputs_state

__all__ = ["get_decision_inputs_state"]
