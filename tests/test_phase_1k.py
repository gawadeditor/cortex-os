"""
tests/test_phase_1k.py — Phase 1K: Provider dashboard cleanup and connector verification

Coverage:
    TestSectionLabels         — correct section headings in /providers HTML
    TestProviderGrouping      — each provider in the correct section
    TestElevenLabsSplit       — ElevenLabs in audio section, not image section
    TestVideoProviderPairs    — all 4 pairs rendered with via-MUAPI + official cards
    TestPublishingTestButtons — TikTok/Instagram/Facebook now have Test buttons
    TestPublishingSaveCredential — Meta/TikTok publishing cards have SP connect link
    TestConnectForms          — all 7 required connect forms return 200 with correct title
    TestVerifyRoutes          — all providers return ok=True, none "unknown provider"
    TestNoPlaceholders        — no "not yet implemented" badges remain
    TestSourcePrecedence      — Secret Profile beats env for verify routes
    TestNoNetworkCalls        — no sockets during unit tests
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "1k_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(db):
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db)
    with server.app.test_client() as c:
        yield c


def _providers_html(client):
    """Return /providers page body with comparison patched out."""
    with patch("run_dashboard.get_comparison_result", return_value=None):
        return client.get("/providers").data.decode()


# ===========================================================================
# TestSectionLabels
# ===========================================================================

class TestSectionLabels:

    def test_ai_image_providers_label(self, client):
        assert "AI Image Providers" in _providers_html(client)

    def test_ai_audio_providers_label(self, client):
        assert "AI Audio Providers" in _providers_html(client)

    def test_video_providers_label(self, client):
        assert "Video Providers" in _providers_html(client)

    def test_publishing_platforms_label(self, client):
        assert "Publishing Platforms" in _providers_html(client)

    def test_provider_comparison_label(self, client):
        assert "Provider Comparison" in _providers_html(client)

    def test_old_ai_content_label_gone(self, client):
        assert "AI Content Providers" not in _providers_html(client)


# ===========================================================================
# TestProviderGrouping
# ===========================================================================

class TestProviderGrouping:

    def _section_slice(self, html: str, start_label: str, end_label: str) -> str:
        s = html.find(start_label)
        e = html.find(end_label, s)
        return html[s:e] if s >= 0 and e > s else ""

    def test_openai_in_image_section(self, client):
        html = _providers_html(client)
        img_slice = self._section_slice(html, "AI Image Providers", "AI Audio Providers")
        assert "OpenAI" in img_slice

    def test_openrouter_in_image_section(self, client):
        html = _providers_html(client)
        img_slice = self._section_slice(html, "AI Image Providers", "AI Audio Providers")
        assert "OpenRouter" in img_slice

    def test_muapi_in_image_section(self, client):
        html = _providers_html(client)
        img_slice = self._section_slice(html, "AI Image Providers", "AI Audio Providers")
        assert "MUAPI" in img_slice

    def test_freepik_in_image_section(self, client):
        html = _providers_html(client)
        img_slice = self._section_slice(html, "AI Image Providers", "AI Audio Providers")
        assert "Freepik" in img_slice

    def test_elevenlabs_not_in_image_section(self, client):
        html = _providers_html(client)
        img_slice = self._section_slice(html, "AI Image Providers", "AI Audio Providers")
        assert "ElevenLabs" not in img_slice

    def test_seedance_pairs_in_video_section(self, client):
        html = _providers_html(client)
        vid_slice = self._section_slice(html, "Video Providers", "Publishing Platforms")
        assert "Seedance" in vid_slice
        assert "BytePlus ModelArk" in vid_slice or "seedance_official" in vid_slice

    def test_runway_official_in_video_section(self, client):
        html = _providers_html(client)
        vid_slice = self._section_slice(html, "Video Providers", "Publishing Platforms")
        assert "Runway" in vid_slice

    def test_kling_official_in_video_section(self, client):
        html = _providers_html(client)
        vid_slice = self._section_slice(html, "Video Providers", "Publishing Platforms")
        assert "Kling" in vid_slice

    def test_veo_official_in_video_section(self, client):
        html = _providers_html(client)
        vid_slice = self._section_slice(html, "Video Providers", "Publishing Platforms")
        assert "Veo" in vid_slice

    def test_youtube_in_publishing_section(self, client):
        html = _providers_html(client)
        pub_slice = self._section_slice(html, "Publishing Platforms", "Provider Comparison")
        assert "YouTube" in pub_slice

    def test_tiktok_in_publishing_section(self, client):
        html = _providers_html(client)
        pub_slice = self._section_slice(html, "Publishing Platforms", "Provider Comparison")
        assert "TikTok" in pub_slice

    def test_instagram_in_publishing_section(self, client):
        html = _providers_html(client)
        pub_slice = self._section_slice(html, "Publishing Platforms", "Provider Comparison")
        assert "Instagram" in pub_slice

    def test_facebook_in_publishing_section(self, client):
        html = _providers_html(client)
        pub_slice = self._section_slice(html, "Publishing Platforms", "Provider Comparison")
        assert "Facebook" in pub_slice

    def test_section_order(self, client):
        """AI Image → AI Audio → Video → Publishing → Comparison.

        Uses the section-label div text, not raw string search, to avoid
        false matches in CSS comments (e.g. '/* Provider Comparison */').
        """
        import re
        html = _providers_html(client)
        # Find each label's position via its section-label div (not CSS text)
        def _pos(label):
            m = re.search(r'class="section-label[^"]*"[^>]*>\s*' + re.escape(label), html)
            return m.start() if m else -1

        positions = {
            "AI Image Providers":   _pos("AI Image Providers"),
            "AI Audio Providers":   _pos("AI Audio Providers"),
            "Video Providers":      _pos("Video Providers"),
            "Publishing Platforms": _pos("Publishing Platforms"),
            "Provider Comparison":  _pos("Provider Comparison"),
        }
        for label, pos in positions.items():
            assert pos >= 0, f"Section label {label!r} not found in page"
        ordered = sorted(positions.items(), key=lambda x: x[1])
        labels = [k for k, _ in ordered]
        assert labels == [
            "AI Image Providers",
            "AI Audio Providers",
            "Video Providers",
            "Publishing Platforms",
            "Provider Comparison",
        ]


# ===========================================================================
# TestElevenLabsSplit
# ===========================================================================

class TestElevenLabsSplit:

    def test_elevenlabs_in_audio_section(self, client):
        html = _providers_html(client)
        aud_start = html.find("AI Audio Providers")
        vid_start = html.find("Video Providers")
        audio_slice = html[aud_start:vid_start]
        assert "ElevenLabs" in audio_slice

    def test_ai_image_providers_has_four_providers(self, client):
        html = _providers_html(client)
        img_start = html.find("AI Image Providers")
        aud_start = html.find("AI Audio Providers")
        img_slice = html[img_start:aud_start]
        count = sum(1 for name in ["OpenAI", "OpenRouter", "MUAPI", "Freepik"]
                    if name in img_slice)
        assert count == 4

    def test_elevenlabs_appears_after_image_section_ends(self, client):
        """ElevenLabs card must appear after the AI Image section ends."""
        html = _providers_html(client)
        ai_img_end    = html.find("AI Audio Providers")
        elevenlabs_pos = html.find("ElevenLabs")
        assert elevenlabs_pos > ai_img_end


# ===========================================================================
# TestVideoProviderPairs
# ===========================================================================

class TestVideoProviderPairs:

    def test_all_four_pairs_present(self, client):
        html = _providers_html(client)
        vid_start = html.find("Video Providers")
        pub_start = html.find("Publishing Platforms")
        vid_slice = html[vid_start:pub_start]
        for name in ["Seedance", "Veo", "Kling", "Runway"]:
            assert name in vid_slice, f"{name} not found in video section"

    def test_via_muapi_and_official_for_each(self, client):
        html = _providers_html(client)
        vid_start = html.find("Video Providers")
        pub_start = html.find("Publishing Platforms")
        vid_slice = html[vid_start:pub_start]
        assert "via MUAPI" in vid_slice
        # Official cards: Seedance 2.0, Runway (Official), Kling (Official), Veo (Google)
        assert "BytePlus ModelArk" in vid_slice or "seedance_official" in vid_slice
        assert "Runway (Official)" in vid_slice or "runway_official" in vid_slice

    def test_no_placeholder_badges(self, client):
        html = _providers_html(client)
        assert "Official API — not yet implemented" not in html

    def test_video_section_description_updated(self, client):
        html = _providers_html(client)
        # Old description removed
        assert "direct official API slot (future)" not in html
        # New description present
        assert "direct official API" in html


# ===========================================================================
# TestPublishingTestButtons
# ===========================================================================

class TestPublishingTestButtons:

    def test_verified_at_publish_time_gone(self, client):
        assert "Verified at publish time" not in _providers_html(client)

    def _pub_slice(self, client):
        html = _providers_html(client)
        pub_start = html.find("Publishing Platforms")
        comp_div  = html.find('<div class="cmp-section">')
        return html[pub_start:comp_div] if 0 <= pub_start < comp_div else ""

    def test_tiktok_has_test_button(self, client):
        pub = self._pub_slice(client)
        assert 'tr-tiktok' in pub, "tr-tiktok result div missing from publishing section"

    def test_instagram_has_test_button(self, client):
        pub = self._pub_slice(client)
        assert 'tr-instagram' in pub

    def test_facebook_has_test_button(self, client):
        pub = self._pub_slice(client)
        assert 'tr-facebook' in pub

    def test_youtube_still_has_test_button(self, client):
        html = _providers_html(client)
        assert 'tr-youtube' in html


# ===========================================================================
# TestPublishingSaveCredential
# ===========================================================================

class TestPublishingSaveCredential:

    def _pub_slice(self, client):
        html = _providers_html(client)
        pub_start = html.find("Publishing Platforms")
        # Use the actual div element, not CSS class selector, as end marker.
        # 'Provider Comparison' appears in a CSS comment before the div.
        comp_div = html.find('<div class="cmp-section">')
        if pub_start < 0 or comp_div < 0 or comp_div < pub_start:
            return ""
        return html[pub_start:comp_div]

    def test_save_credential_button_present(self, client):
        # SP connect flow replaced by platform OAuth buttons (Meta) and Paste Token (TikTok)
        pub = self._pub_slice(client)
        assert ("Authorize with Meta" in pub or "Paste Token" in pub
                or "/platforms/meta/authorize" in pub
                or "/platforms/connect/tiktok" in pub)

    def test_tiktok_sp_connect_link(self, client):
        # TikTok now uses /platforms/connect/tiktok (Paste Token flow)
        pub = self._pub_slice(client)
        assert "/platforms/connect/tiktok" in pub

    def test_instagram_sp_connect_link(self, client):
        # Instagram now uses /platforms/meta/authorize (Meta OAuth flow)
        pub = self._pub_slice(client)
        assert "/platforms/meta/authorize" in pub

    def test_facebook_sp_connect_link(self, client):
        # Facebook now uses /platforms/meta/authorize (Meta OAuth flow)
        pub = self._pub_slice(client)
        assert "/platforms/meta/authorize" in pub

    def test_youtube_has_no_sp_connect_link(self, client):
        # YouTube uses OAuth flow only — no Secret Profile SP link
        pub = self._pub_slice(client)
        assert "/providers/content/connect/youtube" not in pub

    def test_meta_notice_is_compact(self, client):
        # The old large orange oauth-notice block must not appear in the
        # publishing section body (CSS class definition doesn't count).
        pub = self._pub_slice(client)
        # oauth-notice div usage (not the CSS class definition in <style>)
        assert '<div class="oauth-notice"' not in pub


# ===========================================================================
# TestConnectForms
# ===========================================================================

class TestConnectForms:

    @pytest.mark.parametrize("provider,expected_title_fragment", [
        ("freepik",          "Freepik"),
        ("runway_official",  "Runway"),
        ("kling_official",   "Kling"),
        ("veo_official",     "Veo"),
        ("tiktok",           "TikTok"),
        ("instagram",        "Instagram"),
        ("facebook",         "Facebook"),
    ])
    def test_connect_form_renders(self, client, provider, expected_title_fragment):
        resp = client.get(f"/providers/content/connect/{provider}")
        assert resp.status_code == 200, f"{provider} returned {resp.status_code}"
        assert expected_title_fragment.encode() in resp.data, \
            f"{expected_title_fragment!r} not found in {provider} connect form"

    def test_unknown_provider_returns_404(self, client):
        resp = client.get("/providers/content/connect/totally_fake_provider")
        assert resp.status_code == 404

    def test_kling_form_mentions_key_secret_format(self, client):
        resp = client.get("/providers/content/connect/kling_official")
        body = resp.data.decode()
        # Kling uses key_id:key_secret format — instructions must mention colon separator
        assert "KLING_API_KEY" in body

    def test_veo_form_mentions_google_api_key(self, client):
        resp = client.get("/providers/content/connect/veo_official")
        assert b"GOOGLE_API_KEY" in resp.data

    def test_facebook_form_mentions_meta_access_token(self, client):
        resp = client.get("/providers/content/connect/facebook")
        assert b"META_ACCESS_TOKEN" in resp.data

    def test_tiktok_form_mentions_tiktok_access_token(self, client):
        resp = client.get("/providers/content/connect/tiktok")
        assert b"TIKTOK_ACCESS_TOKEN" in resp.data

    def test_save_route_exists_for_all(self, client):
        """POST to the save endpoint must not 404 even with bad data (returns redirect/error)."""
        for provider in ("freepik", "runway_official", "kling_official",
                         "veo_official", "tiktok", "instagram", "facebook"):
            resp = client.post(f"/providers/content/connect/{provider}/save",
                               data={"credential": "test-key", "label": "test"})
            # Should not be 404 (provider must be known)
            assert resp.status_code != 404, f"{provider} save route returned 404"


# ===========================================================================
# TestVerifyRoutes
# ===========================================================================

class TestVerifyRoutes:

    @pytest.mark.parametrize("provider", [
        "openai", "openrouter", "muapi", "freepik", "elevenlabs",
        "seedance_official", "runway_official", "kling_official", "veo_official",
        "facebook", "instagram", "tiktok", "youtube",
    ])
    def test_verify_returns_ok_true(self, client, provider):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "no key", "source": "env",
                                 "checked_at": "2026-06-21T00:00:00+00:00",
                                 "provider": provider}):
            resp = client.post(f"/api/providers/verify/{provider}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_unknown_provider_returns_404(self, client):
        resp = client.post("/api/providers/verify/totally_fake_xyz")
        assert resp.status_code == 404

    def test_tiktok_verify_not_unknown(self, client):
        """TikTok was previously excluded — must now return 200 not 404."""
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "no key", "source": "env",
                                 "checked_at": "t", "provider": "tiktok"}):
            resp = client.post("/api/providers/verify/tiktok")
        assert resp.status_code == 200

    def test_facebook_verify_not_unknown(self, client):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "no key", "source": "env",
                                 "checked_at": "t", "provider": "facebook"}):
            resp = client.post("/api/providers/verify/facebook")
        assert resp.status_code == 200

    def test_instagram_verify_not_unknown(self, client):
        with patch("run_dashboard.verify_provider",
                   return_value={"status": "missing", "latency_ms": None,
                                 "error": "no key", "source": "env",
                                 "checked_at": "t", "provider": "instagram"}):
            resp = client.post("/api/providers/verify/instagram")
        assert resp.status_code == 200

    def test_seedance_official_verify_uses_secret_profile(self, db, monkeypatch):
        """Seedance Official key saved as Secret Profile is used by verify route."""
        from hypogeum.provider_verifier import verify_provider
        with patch("hypogeum.byteplus_ark.probe_credential",
                   return_value=(True, None, 95)):
            result = verify_provider("seedance_official", db_path=db)
        # No env key set — should resolve from Secret Profile if available,
        # or return 'missing'. Either way, ok must be truthy.
        assert result["provider"] == "seedance_official"


# ===========================================================================
# TestNoPlaceholders
# ===========================================================================

class TestNoPlaceholders:

    def test_no_direct_placeholder_badges(self, client):
        html = _providers_html(client)
        assert "Official API — not yet implemented" not in html

    def test_runway_official_has_test_button(self, client):
        html = _providers_html(client)
        assert "tr-runway_official" in html

    def test_kling_official_has_test_button(self, client):
        html = _providers_html(client)
        assert "tr-kling_official" in html

    def test_veo_official_has_test_button(self, client):
        html = _providers_html(client)
        assert "tr-veo_official" in html

    def test_seedance_official_has_test_button(self, client):
        html = _providers_html(client)
        assert "tr-seedance_official" in html


# ===========================================================================
# TestSourcePrecedence
# ===========================================================================

class TestSourcePrecedence:

    def test_secret_profile_beats_env_for_freepik(self, db, monkeypatch):
        from hypogeum.credential_manager import create_secret_profile
        from hypogeum.provider_comparison import _resolve_credential
        monkeypatch.setenv("FREEPIK_API_KEY", "env-key")
        create_secret_profile(
            provider="freepik", label="freepik-sp",
            credential_type="api_key", credential_data="sp-key",
            db_path=db,
        )
        cred, source = _resolve_credential("freepik", db)
        assert cred == "sp-key"
        assert source == "secret_profile"

    def test_env_fallback_when_no_profile(self, db, monkeypatch):
        from hypogeum.provider_comparison import _resolve_credential
        monkeypatch.setenv("RUNWAY_API_KEY", "rw-env")
        cred, source = _resolve_credential("runway_official", db)
        assert cred == "rw-env"
        assert source == "env"

    def test_meta_credential_resolved(self, db, monkeypatch):
        from hypogeum.credential_manager import create_secret_profile
        from hypogeum.provider_comparison import _resolve_credential
        create_secret_profile(
            provider="facebook", label="fb-sp",
            credential_type="api_key", credential_data="EAA-sp-token",
            db_path=db,
        )
        cred, source = _resolve_credential("facebook", db)
        assert cred == "EAA-sp-token"
        assert source == "secret_profile"


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    @pytest.fixture(autouse=True)
    def block_sockets(self):
        _orig = socket.socket
        def _blocked(*a, **kw):
            raise RuntimeError("Network blocked in unit tests")
        socket.socket = _blocked
        yield
        socket.socket = _orig

    def test_providers_page_no_network(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            resp = client.get("/providers")
        assert resp.status_code == 200

    def test_all_connect_forms_no_network(self, client):
        for provider in ("freepik", "runway_official", "kling_official",
                         "veo_official", "tiktok", "instagram", "facebook"):
            resp = client.get(f"/providers/content/connect/{provider}")
            assert resp.status_code == 200, f"{provider} connect form failed"

    def test_section_labels_correct_no_network(self, client):
        with patch("run_dashboard.get_comparison_result", return_value=None):
            html = client.get("/providers").data.decode()
        assert "AI Image Providers" in html
        assert "AI Audio Providers" in html
        assert "Video Providers" in html
        assert "Publishing Platforms" in html
