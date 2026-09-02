"""
tests/test_video_dna_static_policy.py — Phase B1.2A: canonical static-framing
policy checks (hypogeum.video_dna_validator.check_static_framing_policy /
check_repeated_static_shots).

Zero provider calls. These two functions are deliberately NOT wired into
validate_scene_structural()/run_structural_validation() in this slice — see
the module comment in hypogeum/video_dna_validator.py for why. This file
tests them standalone.
"""
from __future__ import annotations

from hypogeum.video_dna_validator import (
    check_static_framing_policy, check_repeated_static_shots,
)
from hypogeum.video_dna_validation_suite import get_all_scenes


class TestStaticFramingPolicyAllowed:
    def test_static_with_atmospheric_motion_allowed(self):
        text = (
            "A dark stone chamber. Camera is static. A thin beam of daylight "
            "slowly widens, gradually revealing carved wall surfaces."
        )
        assert check_static_framing_policy(text) is None

    def test_static_with_subject_and_environmental_motion_allowed(self):
        text = (
            "Camera holds a static wide shot while dust drifts slowly through "
            "the light beam as a hand brushes the artifact clear."
        )
        assert check_static_framing_policy(text) is None

    def test_non_static_text_not_flagged(self):
        # Policy is not implicated at all when no static-framing language is present.
        text = "Camera performs a slow push in toward the artifact."
        assert check_static_framing_policy(text) is None


class TestStaticFramingPolicyForbidden:
    def test_completely_frozen_frame_forbidden(self):
        text = (
            "A stone wall in a chamber. Camera is static. No people, no "
            "text, no modern objects."
        )
        result = check_static_framing_policy(text)
        assert result is not None
        assert "frozen" in result

    def test_locked_off_with_no_motion_forbidden(self):
        text = "A bronze artifact on a table. Camera holds a locked-off frame."
        result = check_static_framing_policy(text)
        assert result is not None


class TestRepeatedStaticShots:
    def test_single_static_shot_not_flagged(self):
        texts = ["Camera is static, dust drifts.", "Camera performs a slow push in."]
        assert check_repeated_static_shots(texts) == []

    def test_two_consecutive_static_shots_warned(self):
        texts = [
            "Camera is static, dust drifts.",
            "Camera is static, light widens.",
            "Camera performs a slow push in.",
        ]
        warnings = check_repeated_static_shots(texts, max_consecutive=1)
        assert len(warnings) == 1
        assert "0-1" in warnings[0]

    def test_no_static_shots_returns_no_warnings(self):
        texts = ["Slow push in.", "Slow pull back.", "Slow pan."]
        assert check_repeated_static_shots(texts) == []

    def test_trailing_static_run_also_warned(self):
        texts = [
            "Slow push in.",
            "Camera is static, shadow recedes.",
            "Camera is static, water drips.",
        ]
        warnings = check_repeated_static_shots(texts, max_consecutive=1)
        assert len(warnings) == 1
        assert "1-2" in warnings[0]


class TestAgainstRealValidationSuite:
    """
    Informational — documents how the heuristic behaves against real,
    already-approved content, rather than asserting the suite is "clean."

    Several intentional static establishing/scale shots in the real suite
    (approved, existing content) describe no explicit motion word in their
    own canonical_prompt text — this is exactly why these checks are not
    wired into a hard gate in this slice (see the module comment in
    hypogeum/video_dna_validator.py). Individual inclusion/exclusion
    assertions are used instead of one exact-set assertion so a single
    unexpected scene fails with a clear, isolated message rather than an
    opaque set-mismatch.
    """

    def test_runs_without_error_against_full_suite(self):
        scenes = get_all_scenes()
        flagged = {
            s["validation_id"] for s in scenes
            if check_static_framing_policy(s["canonical_prompt"]) is not None
        }
        assert isinstance(flagged, set)

        # Known intentional static establishing/scale shots with no explicit
        # motion word in their own canonical_prompt text — expected flags,
        # not a defect. A future generalization of this heuristic (or a
        # category-based exemption) is B5 work, not this slice's.
        for expected in ("scale_reveal", "exterior_archaeological_ruins", "underground_chamber"):
            assert expected in flagged, (
                f"expected {expected!r} to be flagged by the current heuristic — "
                "if this changed, the suite text or the heuristic moved; review both"
            )

        # Known static-with-explicit-motion scenes — must NOT be flagged.
        for not_expected in (
            "discovery_through_light", "discovery_through_dust",
            "discovery_through_shadow", "slow_environmental_motion",
        ):
            assert not_expected not in flagged, (
                f"did not expect {not_expected!r} to be flagged — it describes "
                "explicit motion alongside static framing"
            )

        # object_reveal uses "steady frame", not a recognised static-trigger
        # phrase, so the policy is not implicated at all for that scene.
        assert "object_reveal" not in flagged

    def test_repeated_static_check_runs_without_error_against_full_suite(self):
        scenes = get_all_scenes()
        texts = [s["canonical_prompt"] for s in scenes]
        warnings = check_repeated_static_shots(texts, max_consecutive=1)
        assert isinstance(warnings, list)
