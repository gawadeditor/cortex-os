"""Test support: replace hypogeum.capability_boundary.verify_and_consume_
capability() with a pass-through for FOCUSED adapter-behavior unit tests
that hand-build a capability and call adapter.invoke() directly without a
full submission-gate / persisted-envelope / receipt-trail setup.

Authorization/authenticity/single-use are covered exhaustively by
tests/test_capability_boundary.py and the domain gate-path tests -- these
focused tests only care about what _invoke_real() does with a payload.
"""
from __future__ import annotations

import json
from types import SimpleNamespace


def install_passthrough(monkeypatch):
    def _verify(capability, **_kw):
        try:
            payload = json.loads(capability.operation_payload_bytes)
        except Exception:  # noqa: BLE001
            payload = {}
        return SimpleNamespace(
            capability=capability,
            envelope=None,
            operation_payload=payload,
            execution_id=str(payload.get("episode_id", "")),
            idempotency_key=capability.idempotency_key,
            claim_nonce="stub-nonce",
            assert_live=lambda: None,
            claim_io_entry=lambda: "stub-io-nonce",
        )

    monkeypatch.setattr(
        "hypogeum.capability_boundary.verify_and_consume_capability", _verify,
    )
