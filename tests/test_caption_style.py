"""Tests for hypogeum.caption_style — per-channel burned-caption look +
9:16 safe-area placement."""
from __future__ import annotations

import pytest

from hypogeum.caption_style import (
    CaptionStyle, SHORT_NATIVE_DEFAULT, merge_style, build_ass_style,
    safe_area_margins, resolve_caption_style, burn_enabled,
)
from hypogeum.db import initialize_schema, get_connection
import hypogeum.channel_config as cc


def test_default_is_1080_safe_and_bottom():
    a = build_ass_style(SHORT_NATIVE_DEFAULT, 1080, 1920)
    assert a["play_res_x"] == 1080 and a["play_res_y"] == 1920
    assert a["alignment"] == 2                       # bottom-centre
    # sits above the VERTICAL_MASTER bottom safe zone (500px on 1920) + extra lift
    assert 490 <= a["margin_v"] <= 560
    # clear of the right action rail (VERTICAL_MASTER right = 180px on 1080)
    assert a["margin_r"] >= 178


def test_safe_area_scales_with_width():
    m1 = safe_area_margins(1080, 1920)
    m2 = safe_area_margins(2160, 3840)
    assert abs(m2["bottom"] - 2 * m1["bottom"]) <= 1
    assert abs(m2["right"] - 2 * m1["right"]) <= 1


def test_style_scales_font_to_frame_width():
    s = CaptionStyle(font_size=64)
    a1080 = build_ass_style(s, 1080, 1920)
    a2160 = build_ass_style(s, 2160, 3840)
    assert ",128," in a2160["style_line"]           # 64 -> 128 at 2x width
    assert ",64," in a1080["style_line"]


def test_merge_only_touches_known_keys():
    s = merge_style({"font_size": 80, "position": "center", "bogus": "x"})
    assert s.font_size == 80 and s.position == "center"
    assert s.font == SHORT_NATIVE_DEFAULT.font        # untouched


def test_validated_clamps_out_of_range():
    s = CaptionStyle(font_size=9999, outline_px=-5, position="sideways").validated()
    assert s.font_size == 160 and s.outline_px == 0
    assert s.position == "lower-third"


def test_center_position_ignores_margin_v():
    a = build_ass_style(CaptionStyle(position="center"), 1080, 1920)
    assert a["alignment"] == 5 and a["margin_v"] == 0


def test_hex_colours_become_ass_bgr():
    a = build_ass_style(CaptionStyle(primary_hex="FF0000"), 1080, 1920)
    assert "&H000000FF" in a["style_line"]            # red -> BGR opaque


@pytest.fixture()
def db_with_channel(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    conn = get_connection(p)
    conn.close()
    pp = cc.create_publishing_profile(name="pp", enabled_platforms=["youtube"], db_path=p)
    cc.create_channel_profile(channel_id="ch", name="Ch",
                              publishing_profile_id=pp.publishing_profile_id, db_path=p)
    return p, pp.publishing_profile_id


def test_burn_disabled_by_default_flag(db_with_channel):
    p, pid = db_with_channel
    cc.update_publishing_profile(pid, db_path=p, auto_captions=0)
    assert burn_enabled("ch", db_path=p) is False


def test_burn_and_style_round_trip(db_with_channel):
    p, pid = db_with_channel
    cc.update_publishing_profile(
        p and pid, db_path=p, auto_captions=1,
        caption_style={"font": "Poppins", "font_size": 70, "position": "center", "uppercase": True},
    )
    assert burn_enabled("ch", db_path=p) is True
    s = resolve_caption_style("ch", db_path=p)
    assert s.font == "Poppins" and s.font_size == 70 and s.position == "center"
    assert s.uppercase is True


def test_unknown_channel_is_safe():
    assert burn_enabled("nope") is False
    assert resolve_caption_style("nope") == SHORT_NATIVE_DEFAULT
