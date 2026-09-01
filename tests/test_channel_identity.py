"""Per-channel visual identity: a channel with its own brand profile gets its
own directive; Hypogeum and unconfigured channels stay on the canonical one."""

from __future__ import annotations

from hypogeum.brand import channel_identity, visual_identity
from hypogeum.channel_config import create_brand_profile, create_channel_profile


def _seed_kids(db):
    bp = create_brand_profile(
        "Kids Brand",
        visual_style=("soft pastel watercolor backgrounds, rounded chubby characters, "
                      "dreamy bokeh stars, storybook paper grain"),
        tone="gentle whimsical soothing",
        color_palette={"night": "#2b1a4a", "glow": "#f4d9a0"},
        db_path=db,
    )
    create_channel_profile("kids", "Twinkle Kids",
                           brand_profile_id=bp.brand_profile_id, db_path=db)


def test_canonical_when_no_channel(monkeypatch):
    monkeypatch.setattr(channel_identity, "_brand_profile", lambda c, d: None)
    got = channel_identity.build_channel_visual_identity_directive(None, "video_scene")
    assert got == visual_identity.build_visual_identity_directive("video_scene")


def test_canonical_when_channel_has_brand_bible(tmp_path, monkeypatch):
    # hypogeum has docs/brand_guardian/brand_bible_hypogeum.json -> bible path
    assert channel_identity.channel_uses_own_brand_profile("hypogeum") is False
    got = channel_identity.build_channel_visual_identity_directive("hypogeum", "video_scene")
    assert got == visual_identity.build_visual_identity_directive("video_scene")


def test_profile_directive_for_channel_with_own_brand(tmp_path):
    db = tmp_path / "t.db"
    _seed_kids(db)
    # route through the profile path by pointing the resolver at the test DB
    import hypogeum.channel_config as cc
    orig = cc._resolve_db_path
    cc._resolve_db_path = lambda p: db
    try:
        assert channel_identity.channel_uses_own_brand_profile("kids") is True
        d = channel_identity.build_channel_visual_identity_directive("kids", "video_scene")
        assert "TWINKLE KIDS BRAND" in d or "TWINKLE BRAND" in d
        assert "pastel watercolor" in d.lower()
        assert "gentle" in d.lower()          # tone -> soft finish
        assert "#2b1a4a" in d                 # palette anchor
        assert d != visual_identity.build_visual_identity_directive("video_scene")
        # deterministic
        assert d == channel_identity.build_channel_visual_identity_directive("kids", "video_scene")
        # video_scene tier does not forbid natural colors
        assert "scene truthfully requires" in d.lower() or "take precedence" in d.lower()
        fb = channel_identity.get_channel_adaptive_video_forbidden_items("kids")
        assert any("fantasy-art" in x for x in fb)
    finally:
        cc._resolve_db_path = orig


def test_active_channel_contextvar_routes_and_resets(tmp_path):
    db = tmp_path / "t.db"
    _seed_kids(db)
    import hypogeum.channel_config as cc
    orig = cc._resolve_db_path
    cc._resolve_db_path = lambda p: db
    try:
        base_full = visual_identity.build_visual_identity_directive("thumbnail")
        base_compact = visual_identity.build_visual_identity_directive_compact("thumbnail")
        with channel_identity.active_channel("kids"):
            assert "pastel" in visual_identity.build_visual_identity_directive("thumbnail").lower()
            assert "pastel" in visual_identity.build_visual_identity_directive_compact("thumbnail").lower()
            assert any("fantasy-art" in x
                       for x in visual_identity.get_adaptive_video_forbidden_items())
        # cleanly reset after the block
        assert visual_identity.build_visual_identity_directive("thumbnail") == base_full
        assert visual_identity.build_visual_identity_directive_compact("thumbnail") == base_compact
        # a channel with its own brand bible (hypogeum) is never routed
        with channel_identity.active_channel("hypogeum"):
            assert visual_identity.build_visual_identity_directive("thumbnail") == base_full
    finally:
        cc._resolve_db_path = orig


def test_assemble_prompt_threads_channel_id(tmp_path):
    db = tmp_path / "t.db"
    _seed_kids(db)
    import hypogeum.channel_config as cc
    orig = cc._resolve_db_path
    cc._resolve_db_path = lambda p: db
    try:
        from hypogeum.video_prompt_writer import assemble_video_scene_prompt as a
        scene = {"scene": "A small creature curls up to sleep.", "camera": "slow drift"}
        base = a(scene)
        kids = a(scene, channel_id="kids")
        assert base == a(scene, channel_id=None)          # unchanged default
        assert kids != base
        assert "pastel" in kids.lower()
        # plan-vs-submission parity: same channel_id -> identical text
        assert kids == a(scene, channel_id="kids")
    finally:
        cc._resolve_db_path = orig
