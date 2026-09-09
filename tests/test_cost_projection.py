"""
tests/test_cost_projection.py — Focused tests for
hypogeum/cost_projection.py: the idempotent bridge from the
authoritative protected-execution spend ledger
(run_authorization_grant_consumption) to dashboard/legacy cost
reporting (costs + protected_path_cost_projections).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

import hypogeum.authorization_store as store
import hypogeum.cost_projection as cp


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "test.db"))
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, total_cost_usd REAL)
        """
    )
    c.execute(
        """
        CREATE TABLE costs (
            cost_id TEXT PRIMARY KEY, run_id TEXT, episode_id TEXT, provider TEXT,
            model TEXT, call_type TEXT, tokens_or_credits REAL, cost_usd REAL,
            called_at TEXT, metadata_version TEXT DEFAULT 'v1'
        )
        """
    )
    store.ensure_tables(c)
    cp.ensure_tables(c)
    yield c
    c.close()


def _insert_committed_ledger_row(
    conn, *, reservation_id="res1", grant_id="grant1", grant_version=1, episode_id="ep_x",
    channel_id="chan_x", provider="fake", model="fake-model", provider_operation="video.text_to_video",
    authorization_id="auth1", idempotency_key="idem1", charged_amount="0.39", currency="USD",
):
    conn.execute(
        """
        INSERT OR IGNORE INTO run_authorization_grants
            (grant_id, grant_version, grant_checksum, channel_id, episode_id, workflow_id,
             workflow_version, workflow_checksum, bindings_json, max_total_spend_all_bindings,
             currency, expires_at, approval_id, approval_version, status, registered_at)
        VALUES (?, ?, 'checksum1', ?, ?, 'wf', '1.0', 'wfchecksum', '[]', NULL, ?, '2099-01-01T00:00:00Z',
                'approval1', 1, 'active', '2026-01-01T00:00:00Z')
        """,
        (grant_id, grant_version, channel_id, episode_id, currency),
    )
    conn.execute(
        """
        INSERT INTO run_authorization_grant_consumption
            (reservation_id, grant_id, grant_version, provider, model, provider_operation,
             idempotency_key, envelope_id, authorization_id, reserved_amount, charged_amount,
             state, reconciliation_resolution, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'env1', ?, ?, ?, 'committed', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (reservation_id, grant_id, grant_version, provider, model, provider_operation,
         idempotency_key, authorization_id, charged_amount, charged_amount),
    )
    conn.commit()


class TestProjectCommittedCharge:

    def test_inserts_one_row(self, conn):
        inserted = cp.project_committed_charge(
            conn, reservation_id="res1", grant_id="grant1", grant_version=1, episode_id="ep_x",
            channel_id="chan_x", provider="fake", model="fake-model", provider_operation="video.text_to_video",
            authorization_id="auth1", charged_amount=Decimal("0.39"), currency="USD", idempotency_key="idem1",
        )
        assert inserted is True
        total = cp.get_projected_total_for_episode(conn, "ep_x")
        assert total == Decimal("0.39")

    def test_replay_is_idempotent(self, conn):
        kwargs = dict(
            reservation_id="res1", grant_id="grant1", grant_version=1, episode_id="ep_x", channel_id="chan_x",
            provider="fake", model="fake-model", provider_operation="video.text_to_video",
            authorization_id="auth1", charged_amount=Decimal("0.39"), currency="USD", idempotency_key="idem1",
        )
        first = cp.project_committed_charge(conn, **kwargs)
        second = cp.project_committed_charge(conn, **kwargs)
        assert first is True
        assert second is False
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0.39")


class TestReconcileAllCommittedCharges:

    def test_reconciles_a_committed_ledger_row(self, conn):
        _insert_committed_ledger_row(conn)
        result = cp.reconcile_all_committed_charges(conn)
        assert result == {"projected": 1, "already_present": 0, "total_committed_ledger_rows": 1}
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0.39")

    def test_reconciliation_is_idempotent(self, conn):
        _insert_committed_ledger_row(conn)
        cp.reconcile_all_committed_charges(conn)
        second = cp.reconcile_all_committed_charges(conn)
        assert second == {"projected": 0, "already_present": 1, "total_committed_ledger_rows": 1}
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0.39")

    def test_non_committed_states_are_never_projected(self, conn):
        """reserved/boundary_entered/released/possible_unknown_acceptance
        rows must never appear in dashboard-visible cost -- only a
        confirmed, charged 'committed' outcome is real spend."""
        conn.execute(
            """
            INSERT INTO run_authorization_grants
                (grant_id, grant_version, grant_checksum, channel_id, episode_id, workflow_id,
                 workflow_version, workflow_checksum, bindings_json, max_total_spend_all_bindings,
                 currency, expires_at, approval_id, approval_version, status, registered_at)
            VALUES ('grant2', 1, 'c2', 'chan_x', 'ep_x', 'wf', '1.0', 'wfc', '[]', NULL, 'USD',
                    '2099-01-01T00:00:00Z', 'approval2', 1, 'active', '2026-01-01T00:00:00Z')
            """
        )
        for state in ("reserved", "boundary_entered", "released", "possible_unknown_acceptance"):
            conn.execute(
                """
                INSERT INTO run_authorization_grant_consumption
                    (reservation_id, grant_id, grant_version, provider, model, provider_operation,
                     idempotency_key, envelope_id, authorization_id, reserved_amount, charged_amount,
                     state, reconciliation_resolution, created_at, updated_at)
                VALUES (?, 'grant2', 1, 'fake', 'fake-model', 'video.text_to_video', ?, 'env1', ?,
                        '0.39', '0.39', ?, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """,
                (f"res_{state}", f"idem_{state}", f"auth_{state}", state),
            )
        conn.commit()
        result = cp.reconcile_all_committed_charges(conn)
        assert result["projected"] == 0
        assert cp.get_projected_total_for_episode(conn, "ep_x") == Decimal("0")

    def test_generic_across_multiple_episodes_and_channels(self, conn):
        """Never filtered to one episode/channel -- proves the same
        mechanism generalizes."""
        _insert_committed_ledger_row(conn, reservation_id="res_a", grant_id="grant_a", episode_id="ep_a", channel_id="chan_a")
        _insert_committed_ledger_row(conn, reservation_id="res_b", grant_id="grant_b", episode_id="ep_b", channel_id="chan_b")
        result = cp.reconcile_all_committed_charges(conn)
        assert result["projected"] == 2
        assert cp.get_projected_total_for_episode(conn, "ep_a") == Decimal("0.39")
        assert cp.get_projected_total_for_episode(conn, "ep_b") == Decimal("0.39")


class TestResolveEpisodeId:
    """Channel-operating grants carry episode_id='*'; the real episode is
    recovered from the leading segment of the idempotency_key."""

    def test_channel_operating_grant_resolves_from_key(self):
        assert cp.resolve_episode_id("*", "ep_ad71a178:s09:primary") == "ep_ad71a178"

    def test_channel_operating_grant_resolves_narration_key(self):
        assert cp.resolve_episode_id("*", "ep_4f92438f:narr_b_001") == "ep_4f92438f"

    def test_episode_scoped_grant_wins_over_key(self):
        assert cp.resolve_episode_id("ep_af078f34", "ep_af078f34:s01:primary") == "ep_af078f34"

    def test_opaque_key_falls_back_to_grant_value(self):
        assert cp.resolve_episode_id("*", "429fdd19e6cdeed97dbf6f0bfbe26ec5") == "*"

    def test_empty_key_falls_back(self):
        assert cp.resolve_episode_id("*", "") == "*"
        assert cp.resolve_episode_id("*", None) == "*"


class TestReconcileAttributesChannelOperatingGrant:

    def test_charge_under_star_grant_is_attributed_to_episode(self, conn):
        _insert_committed_ledger_row(
            conn,
            reservation_id="op_grant_x:1:muapi:wan2.7-text-to-video:video.text_to_video:ep_ad71a178:s09:primary",
            grant_id="op_grant_x", episode_id="*", channel_id="twinkle_mini_tales",
            provider="muapi", model="wan2.7-text-to-video",
            idempotency_key="ep_ad71a178:s09:primary", authorization_id="ep_ad71a178:s09:primary",
            charged_amount="0.65",
        )
        result = cp.reconcile_all_committed_charges(conn)
        assert result["projected"] == 1
        assert cp.get_projected_total_for_episode(conn, "ep_ad71a178") == Decimal("0.65")
        # the literal '*' must never carry the charge
        assert cp.get_projected_total_for_episode(conn, "*") == Decimal("0")


class TestLegacyCallTypeOperation:

    def test_openrouter_agent_names_all_map_to_text_completion(self):
        for ct in ("executive_producer", "narrative_architect", "visual_package_director", "some_future_agent"):
            assert cp.legacy_call_type_operation("openrouter", ct) == ("openrouter", "text.completion")

    def test_perplexity_maps_to_research_query(self):
        assert cp.legacy_call_type_operation("perplexity", "web_search") == ("perplexity", "research.query")

    def test_muapi_and_elevenlabs_explicit_rows(self):
        assert cp.legacy_call_type_operation("muapi", "scene_generation") == ("muapi", "video.text_to_video")
        assert cp.legacy_call_type_operation("muapi", "video_upscale") == ("muapi", "upscale.video")
        assert cp.legacy_call_type_operation("elevenlabs", "music") == ("elevenlabs", "music.track")

    def test_unknown_call_type_is_none(self):
        assert cp.legacy_call_type_operation("muapi", "source_enhancement") is None


class TestGetDedupedEpisodeTotal:

    def _legacy(self, conn, episode_id, provider, call_type, cost):
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES (?, 'run1', ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
            (f"c_{episode_id}_{call_type}_{cost}", episode_id, provider, call_type, cost),
        )
        conn.commit()

    def test_modern_episode_not_double_counted(self, conn):
        """Every legacy row also has a committed ledger charge for the same
        operation -> deduped total == ledger total, not the sum."""
        # ledger: video + narration + text
        _insert_committed_ledger_row(
            conn, reservation_id="r_vid", grant_id="op_grant_x", episode_id="*",
            provider="muapi", model="wan2.7-text-to-video", provider_operation="video.text_to_video",
            idempotency_key="ep_m:s01:primary", authorization_id="ep_m:s01:primary", charged_amount="9.75",
        )
        _insert_committed_ledger_row(
            conn, reservation_id="r_narr", grant_id="op_grant_x", grant_version=1, episode_id="*",
            provider="elevenlabs", model="eleven_multilingual_v2", provider_operation="voice.narration_segment",
            idempotency_key="ep_m:narr_001", authorization_id="ep_m:narr_001", charged_amount="0.20",
        )
        _insert_committed_ledger_row(
            conn, reservation_id="r_txt", grant_id="op_grant_x", episode_id="*",
            provider="openrouter", model="anthropic/claude-sonnet-4.6", provider_operation="text.completion",
            idempotency_key="ep_m:executive_producer:x", authorization_id="ep_m:executive_producer:x",
            charged_amount="1.00",
        )
        # legacy: the SAME spend, recorded again by finance.record_cost
        self._legacy(conn, "ep_m", "muapi", "scene_generation", 9.75)
        self._legacy(conn, "ep_m", "elevenlabs", "narration", 0.20)
        self._legacy(conn, "ep_m", "openrouter", "executive_producer", 1.00)

        assert cp.get_deduped_episode_total(conn, "ep_m") == Decimal("10.95")
        # the naive reconciled total double-counts:
        cp.reconcile_all_committed_charges(conn)
        assert cp.get_reconciled_episode_total(conn, "ep_m") == Decimal("21.90")

    def test_ungated_music_is_added_on_top_of_ledger(self, conn):
        _insert_committed_ledger_row(
            conn, reservation_id="r_vid", grant_id="op_grant_x", episode_id="*",
            provider="muapi", model="wan2.7-text-to-video", provider_operation="video.text_to_video",
            idempotency_key="ep_mu:s01:primary", authorization_id="ep_mu:s01:primary", charged_amount="5.00",
        )
        # music recorded only in legacy (cache path / pre-gate) -> no music.track ledger row
        self._legacy(conn, "ep_mu", "elevenlabs", "music", 1.42)
        self._legacy(conn, "ep_mu", "muapi", "scene_generation", 5.00)  # dup of ledger, must drop

        assert cp.get_deduped_episode_total(conn, "ep_mu") == Decimal("6.42")

    def test_gated_music_is_not_double_counted(self, conn):
        _insert_committed_ledger_row(
            conn, reservation_id="r_music", grant_id="op_grant_x", episode_id="*",
            provider="elevenlabs", model="eleven_music_v1", provider_operation="music.track",
            idempotency_key="ep_gm:music_001", authorization_id="ep_gm:music_001", charged_amount="1.42",
        )
        self._legacy(conn, "ep_gm", "elevenlabs", "music", 1.42)
        assert cp.get_deduped_episode_total(conn, "ep_gm") == Decimal("1.42")

    def test_pure_pre_grant_episode_uses_legacy_costs(self, conn):
        self._legacy(conn, "ep_old", "openrouter", "research_director", 1.50)
        self._legacy(conn, "ep_old", "muapi", "scene_generation", 6.00)
        assert cp.get_deduped_episode_total(conn, "ep_old") == Decimal("7.50")

    def test_unknown_legacy_call_type_is_kept(self, conn):
        _insert_committed_ledger_row(
            conn, reservation_id="r_vid", grant_id="op_grant_x", episode_id="*",
            provider="muapi", model="wan2.7-text-to-video", provider_operation="video.text_to_video",
            idempotency_key="ep_uk:s01:primary", authorization_id="ep_uk:s01:primary", charged_amount="5.00",
        )
        self._legacy(conn, "ep_uk", "muapi", "source_enhancement", 0.08)
        assert cp.get_deduped_episode_total(conn, "ep_uk") == Decimal("5.08")

    def test_breakdown_shape(self, conn):
        _insert_committed_ledger_row(
            conn, reservation_id="r_vid", grant_id="op_grant_x", episode_id="*",
            provider="muapi", model="wan2.7-text-to-video", provider_operation="video.text_to_video",
            idempotency_key="ep_bd:s01:primary", authorization_id="ep_bd:s01:primary", charged_amount="5.00",
        )
        self._legacy(conn, "ep_bd", "elevenlabs", "music", 1.00)
        b = cp.get_deduped_episode_breakdown(conn, "ep_bd")
        assert b["ledger_total"] == Decimal("5.00")
        assert b["legacy_only_total"] == Decimal("1.00")
        assert b["total"] == Decimal("6.00")
        assert b["has_ledger_charges"] is True
        assert b["legacy_only_by_call_type"] == {"music": Decimal("1.00")}


class TestGetReconciledEpisodeTotal:

    def test_combines_legacy_costs_and_protected_path_projections(self, conn):
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES ('c1', 'run1', 'ep_x', 'openrouter', 'research', 1.50, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        _insert_committed_ledger_row(conn)
        cp.reconcile_all_committed_charges(conn)
        total = cp.get_reconciled_episode_total(conn, "ep_x")
        assert total == Decimal("1.50") + Decimal("0.39")

    def test_zero_when_nothing_recorded(self, conn):
        assert cp.get_reconciled_episode_total(conn, "ep_nonexistent") == Decimal("0")

    def test_exact_decimal_arithmetic(self, conn):
        """0.1 + 0.2 != 0.3 in float; this must never surface here."""
        conn.execute(
            "INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at) "
            "VALUES ('c1', 'run1', 'ep_x', 'openrouter', 'research', 0.1, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        _insert_committed_ledger_row(conn, charged_amount="0.2")
        cp.reconcile_all_committed_charges(conn)
        total = cp.get_reconciled_episode_total(conn, "ep_x")
        assert total == Decimal("0.3")
        assert str(total) == "0.3"
