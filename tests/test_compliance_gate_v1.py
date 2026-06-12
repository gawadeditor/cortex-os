"""
tests/test_compliance_gate_v1.py — Phase 4D: Compliance Gate v1 banned terms check

Coverage:
    TestBannedTermsCheck        — c_banned_terms: PASS/FAIL on specific terms
    TestBannedTermsTitle        — banned terms detected in episode_title
    TestBannedTermsDescription  — banned terms detected in description field
    TestBannedTermsCategories   — conspiracy, sensationalist, fabricated-authority term sets
    TestBannedTermsCleanPass    — clean content always produces PASS
    TestBannedTermsBlocking     — banned terms cause REJECTED verdict
"""

from __future__ import annotations

from hypogeum.agents.compliance_agent import run_compliance_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _frp(title: str = "The Antikythera Mechanism",
         description: str = "A documentary about ancient technology.",
         ai_disclosure: str = "Made with AI assistance.") -> dict:
    return {
        "metadata_source": {
            "episode_title": title,
            "ai_disclosure_text": ai_disclosure,
            "description": description,
            "section_titles": ["Intro", "Main", "Outro"],
        },
        "music_log": [],
        "package_integrity": {"production_ready": True},
        "assembly_qa_report": {"overall_status": "pass"},
        "episode_render": {"video_asset_id": "vid_001", "caption_asset_id": "cap_001"},
        "shorts_renders": [],
        "thumbnail_references": [{"thumbnail_asset_id": "th_001", "format": "jpeg"}],
        "package_metadata": {"render_package_id": "rp_001", "episode_id": "ep_000"},
    }


def _run(title="The Antikythera Mechanism", description="A documentary about ancient history.",
         source_registry=None, **kwargs):
    return run_compliance_agent(
        final_render_package=_frp(title=title, description=description),
        episode_id="ep_000",
        source_registry=source_registry or [{"source_id": "src_001", "title": "Test Source"}],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# TestBannedTermsCheck
# ---------------------------------------------------------------------------

class TestBannedTermsCheck:
    def test_banned_terms_check_id_present(self, tmp_path):
        report = _run(output_dir=tmp_path)
        check_ids = {c["check_id"] for c in report["checks"]}
        assert "c_banned_terms" in check_ids

    def test_clean_content_passes(self, tmp_path):
        report = _run(output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "pass"

    def test_banned_term_in_title_fails(self, tmp_path):
        report = _run(title="The Shocking Truth About Ancient Ruins", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_banned_term_in_description_fails(self, tmp_path):
        report = _run(description="This video reveals a shocking conspiracy.", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_banned_term_causes_rejected(self, tmp_path):
        report = _run(title="The Shocking Truth", output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"
        assert report["compliant"] is False

    def test_banned_term_in_failures_list(self, tmp_path):
        report = _run(title="Unbelievable Ancient Secrets", output_dir=tmp_path)
        assert any("banned_terms" in f for f in report["failures"])

    def test_case_insensitive_matching(self, tmp_path):
        report = _run(title="CONSPIRACY of the Ages", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"


# ---------------------------------------------------------------------------
# TestBannedTermsTitle
# ---------------------------------------------------------------------------

class TestBannedTermsTitle:
    def test_conspiracy_in_title(self, tmp_path):
        report = _run(title="Ancient Conspiracy Uncovered", output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"

    def test_illuminati_in_title(self, tmp_path):
        report = _run(title="The Illuminati and Ancient Egypt", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_insane_in_title(self, tmp_path):
        report = _run(title="Insane Archaeological Find", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_proof_that_in_title(self, tmp_path):
        report = _run(title="Proof That Ancient Aliens Existed", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_clean_title_passes(self, tmp_path):
        report = _run(title="The Underground Temples of Malta", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "pass"


# ---------------------------------------------------------------------------
# TestBannedTermsDescription
# ---------------------------------------------------------------------------

class TestBannedTermsDescription:
    def test_conspiracy_in_description(self, tmp_path):
        report = _run(description="Historians believe this was part of a conspiracy.", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_scientists_confirm_in_description(self, tmp_path):
        report = _run(description="Scientists confirm the age of the site.", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_definitive_evidence_in_description(self, tmp_path):
        report = _run(description="This is definitive evidence of ancient trade routes.", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "fail"

    def test_empty_description_passes(self, tmp_path):
        """Empty description is fine for banned terms (description_present is a QA check)."""
        report = _run(description="", output_dir=tmp_path)
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "pass"

    def test_no_description_key_passes(self, tmp_path):
        """Missing description field — no banned terms can match."""
        frp = _frp(title="The Antikythera Mechanism", description="")
        del frp["metadata_source"]["description"]
        report = run_compliance_agent(
            final_render_package=frp,
            episode_id="ep_000",
            source_registry=[{"source_id": "s1", "title": "Source"}],
            output_dir=tmp_path,
        )
        statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert statuses["c_banned_terms"] == "pass"


# ---------------------------------------------------------------------------
# TestBannedTermsCategories
# ---------------------------------------------------------------------------

class TestBannedTermsCategories:
    def test_conspiracy_category(self, tmp_path):
        for term in ["conspiracy", "cover-up", "illuminati", "reptilian"]:
            report = _run(title=f"Ancient {term} revealed", output_dir=tmp_path)
            statuses = {c["check_id"]: c["status"] for c in report["checks"]}
            assert statuses["c_banned_terms"] == "fail", f"Expected fail for term: {term!r}"

    def test_sensationalist_category(self, tmp_path):
        for term in ["shocking", "unbelievable", "mindblowing", "mind-blowing", "insane"]:
            report = _run(title=f"{term} ancient discovery", output_dir=tmp_path)
            statuses = {c["check_id"]: c["status"] for c in report["checks"]}
            assert statuses["c_banned_terms"] == "fail", f"Expected fail for term: {term!r}"

    def test_fabricated_authority_category(self, tmp_path):
        for term in ["scientists confirm", "proof that", "definitive evidence"]:
            report = _run(description=f"Title: {term} the truth.", output_dir=tmp_path)
            statuses = {c["check_id"]: c["status"] for c in report["checks"]}
            assert statuses["c_banned_terms"] == "fail", f"Expected fail for term: {term!r}"


# ---------------------------------------------------------------------------
# TestBannedTermsCleanPass
# ---------------------------------------------------------------------------

class TestBannedTermsCleanPass:
    def test_legitimate_titles_pass(self, tmp_path):
        legitimate_titles = [
            "The Antikythera Mechanism",
            "Underground Temples of Malta",
            "Lost Cities of the Inca Empire",
            "Roman Engineering in Brittain",
            "The Dead Sea Scrolls — A New Perspective",
        ]
        for title in legitimate_titles:
            report = _run(title=title, output_dir=tmp_path)
            statuses = {c["check_id"]: c["status"] for c in report["checks"]}
            assert statuses["c_banned_terms"] == "pass", f"Unexpected fail for: {title!r}"

    def test_legitimate_descriptions_pass(self, tmp_path):
        descriptions = [
            "A documentary about the ancient hypogeum of Hal Saflieni in Malta.",
            "We explore the engineering techniques of Roman aqueducts.",
            "This episode examines archaeological evidence from recent excavations.",
        ]
        for desc in descriptions:
            report = _run(description=desc, output_dir=tmp_path)
            statuses = {c["check_id"]: c["status"] for c in report["checks"]}
            assert statuses["c_banned_terms"] == "pass", f"Unexpected fail for: {desc!r}"


# ---------------------------------------------------------------------------
# TestBannedTermsBlocking
# ---------------------------------------------------------------------------

class TestBannedTermsBlocking:
    def test_banned_term_blocks_verdict(self, tmp_path):
        """Banned terms → REJECTED even if all other checks pass."""
        report = _run(title="The Shocking Truth About Malta", output_dir=tmp_path)
        assert report["verdict"] == "REJECTED"

    def test_banned_terms_fail_in_check_list(self, tmp_path):
        report = _run(title="Unbelievable Discovery", output_dir=tmp_path)
        check_by_id = {c["check_id"]: c for c in report["checks"]}
        assert check_by_id["c_banned_terms"]["status"] == "fail"

    def test_multiple_banned_terms_still_one_check(self, tmp_path):
        """Multiple matched terms produce one c_banned_terms check (not multiple)."""
        report = _run(
            title="Shocking conspiracy revealed",
            description="This is unbelievable and insane.",
            output_dir=tmp_path,
        )
        banned_checks = [c for c in report["checks"] if c["check_id"] == "c_banned_terms"]
        assert len(banned_checks) == 1
