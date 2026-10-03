"""
tests/test_channel_package_policy_isolation.py — a new channel's Channel
Package must never inherit another channel's platform policy (2026-10-03:
twinkle_mini_tales' platform_distribution_policy_v1.json was a verbatim copy
of hypogeum's, and the file->DB sync would have applied it).

Template = a tmp copy of the real channel_packages/hypogeum; tmp DB only.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from hypogeum.channel_config import (
    ChannelPackagePolicyDriftError,
    create_channel_operations_profile,
    create_channel_profile,
    get_channel_operations_profile,
    load_platform_distribution_policy,
    sync_platform_distribution_policy_to_runtime,
    write_platform_distribution_policy_from_runtime,
)
from hypogeum.channel_package_scaffold import scaffold_channel_package
from hypogeum.db import initialize_schema

REPO_TEMPLATE = Path(__file__).resolve().parent.parent / "channel_packages" / "hypogeum"
NEW = "kids_test_channel"
NEW_POLICY = {
    "youtube": {"shorts": {"enabled": True, "categoryId": "27", "madeForKids": True},
                "main_episode": {"enabled": False}},
    "tiktok": {"shorts": {"enabled": True, "privacy": "SELF_ONLY"}, "main_episode": {"enabled": True}},
}


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    root = tmp_path / "channel_packages"
    shutil.copytree(REPO_TEMPLATE, root / "hypogeum")
    return db, root


def _ops(db, channel_id=NEW, policy=None, privacy="private"):
    create_channel_profile(channel_id=channel_id, name=channel_id, db_path=db)
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, db_path=db,
        platform_deliverable_policy=policy if policy is not None else NEW_POLICY,
        default_privacy_state=privacy,
    )


def test_scaffold_writes_the_new_channels_own_policy_not_the_templates(env):
    db, root = env
    _ops(db)
    res = scaffold_channel_package(NEW, db_path=db, packages_root=root)
    assert res["created"] is True
    doc = load_platform_distribution_policy(NEW, packages_root=root)  # checksum verified
    assert doc["channel_id"] == NEW
    assert doc["policy_id"] == f"{NEW}_platform_distribution_policy_v1"
    assert doc["platform_deliverable_policy"] == NEW_POLICY
    assert doc["default_privacy_state"] == "private"
    template_doc = json.loads((root / "hypogeum" / "platform_distribution_policy_v1.json").read_text())
    assert doc["platform_deliverable_policy"] != template_doc["platform_deliverable_policy"]


def test_scaffold_copies_only_shared_artifacts(env):
    db, root = env
    _ops(db)
    scaffold_channel_package(NEW, db_path=db, packages_root=root)
    pkg = root / NEW
    assert not (pkg / "scheduling_policy_v1.json").exists()  # unreferenced leftover, not copied
    manifest = json.loads((pkg / "channel_package_manifest.json").read_text())
    assert manifest["channel_id"] == NEW
    roles = sorted(a["role"] for a in manifest["artifacts"])
    assert roles == ["deliverable_contract", "platform_distribution_policy", "workflow_definition"]
    for art in manifest["artifacts"]:  # every registered checksum is the file's real one
        assert hashlib.sha256((pkg / art["path"]).read_bytes()).hexdigest() == art["sha256"]
    contract = json.loads((pkg / "deliverable_contract_v1.json").read_text())
    if "channel_id" in contract:
        assert contract["channel_id"] == NEW
    wf = next(a for a in manifest["artifacts"] if a["role"] == "workflow_definition")
    assert (pkg / wf["path"]).read_bytes() == (root / "hypogeum" / wf["path"]).read_bytes()


def test_scaffold_without_runtime_profile_writes_no_borrowed_policy(env):
    db, root = env
    create_channel_profile(channel_id=NEW, name=NEW, db_path=db)
    scaffold_channel_package(NEW, db_path=db, packages_root=root)
    assert not (root / NEW / "platform_distribution_policy_v1.json").exists()
    with pytest.raises(ChannelPackagePolicyDriftError):
        load_platform_distribution_policy(NEW, packages_root=root)


def test_sync_refuses_a_file_that_names_another_channel(env):
    db, root = env
    _ops(db)
    scaffold_channel_package(NEW, db_path=db, packages_root=root)
    # simulate the old bug: the template's file dropped in verbatim + manifest updated to match
    src = root / "hypogeum" / "platform_distribution_policy_v1.json"
    dst = root / NEW / "platform_distribution_policy_v1.json"
    dst.write_bytes(src.read_bytes())
    m_path = root / NEW / "channel_package_manifest.json"
    m = json.loads(m_path.read_text())
    for a in m["artifacts"]:
        if a["role"] == "platform_distribution_policy":
            a["sha256"] = hashlib.sha256(dst.read_bytes()).hexdigest()
    m_path.write_text(json.dumps(m))
    with pytest.raises(ChannelPackagePolicyDriftError, match="belongs to another channel"):
        sync_platform_distribution_policy_to_runtime(NEW, packages_root=root, db_path=db)
    assert get_channel_operations_profile(NEW, db_path=db).platform_deliverable_policy == NEW_POLICY


def test_sync_refuses_to_disable_live_publishing_unless_allowed(env):
    db, root = env
    _ops(db)
    scaffold_channel_package(NEW, db_path=db, packages_root=root)
    # runtime moves ahead of the file (e.g. a direct DB edit): tiktok main_episode stays enabled,
    # file is rewritten with it disabled -> sync would switch it off
    path = root / NEW / "platform_distribution_policy_v1.json"
    doc = json.loads(path.read_text())
    doc["platform_deliverable_policy"]["tiktok"]["main_episode"]["enabled"] = False
    path.write_text(json.dumps(doc))
    m_path = root / NEW / "channel_package_manifest.json"
    m = json.loads(m_path.read_text())
    for a in m["artifacts"]:
        if a["role"] == "platform_distribution_policy":
            a["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    m_path.write_text(json.dumps(m))
    with pytest.raises(ChannelPackagePolicyDriftError, match=r"tiktok', 'main_episode"):
        sync_platform_distribution_policy_to_runtime(NEW, packages_root=root, db_path=db)
    sync_platform_distribution_policy_to_runtime(NEW, packages_root=root, db_path=db, allow_disabling=True)
    assert get_channel_operations_profile(NEW, db_path=db).platform_deliverable_policy[
        "tiktok"]["main_episode"]["enabled"] is False


def test_write_then_sync_round_trips_without_change(env):
    db, root = env
    _ops(db)
    scaffold_channel_package(NEW, db_path=db, packages_root=root)
    before = get_channel_operations_profile(NEW, db_path=db).platform_deliverable_policy
    out = write_platform_distribution_policy_from_runtime(NEW, db_path=db, packages_root=root, reason="test")
    assert out["policy_version"] == "1.1.0"
    sync_platform_distribution_policy_to_runtime(NEW, packages_root=root, db_path=db)
    assert get_channel_operations_profile(NEW, db_path=db).platform_deliverable_policy == before


def test_real_repo_packages_are_each_their_own_channel():
    """Guard against the original bug reappearing in the committed packages."""
    root = REPO_TEMPLATE.parent
    for pkg in root.iterdir():
        f = pkg / "platform_distribution_policy_v1.json"
        if f.is_file():
            assert json.loads(f.read_text())["channel_id"] == pkg.name, pkg.name
