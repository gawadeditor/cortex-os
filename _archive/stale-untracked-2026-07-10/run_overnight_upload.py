#!/usr/bin/env python3
"""
Overnight upload script for ep_20260629075717_c7abfd.
Performs YouTube upload of main episode and 5 shorts (private only).
Respects YOUTUBE_DRY_RUN flag from .env.
"""

import json
import datetime
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, "/Users/gawadeditor/hypogeum-brain")

EP_ID = "ep_20260629075717_c7abfd"
EP_DIR = Path(f"/Users/gawadeditor/hypogeum-brain/.hypogeum-assets/episodes/{EP_ID}")

EPISODE_TITLE = "The Hidden City Beneath Rome's Colosseum Floor"
EPISODE_DESCRIPTION = """Beneath the Colosseum's famous arena floor lay a fully operational subterranean city — a labyrinth of tunnels, elevators, and holding chambers that made the spectacle above possible, and whose full story has never been properly told.

In this episode of The Hypogeum Chronicles, we go underground — literally — to reconstruct the engineering, the logistics, the human cost, and the erasure of what the Romans called the Hypogeum.

This video was produced autonomously by the Hypogeum Brain media factory system.

#Colosseum #AncientRome #History #Documentary #Hypogeum #Roman #Mystery"""

SHORT_THEMES = [
    "The Secret Underground: What Rome Hid Beneath the Arena",
    "Engineering the Hypogeum: Rome's Greatest Hidden Machine",
    "The Spectacle of Death: How Gladiators Were Launched Into the Arena",
    "The Forgotten Workforce: Who Actually Ran the Colosseum?",
    "The Mystery Endures: What We Still Don't Know About the Hypogeum",
]

SHORT_DESCRIPTIONS = [
    "The Colosseum hid a secret city beneath its famous arena floor. #Colosseum #AncientRome #History #Shorts",
    "Rome's Hypogeum was the world's most sophisticated entertainment machine. #Engineering #AncientRome #Shorts",
    "Gladiators didn't walk into the arena. They were launched from below. #Gladiators #Rome #Shorts",
    "Thousands worked in the dark beneath the Colosseum — and we don't know their names. #History #Rome #Shorts",
    "2,000 years later, the Hypogeum still holds secrets we haven't unlocked. #Mystery #AncientRome #Shorts",
]


def load_config():
    from hypogeum.config import Config
    return Config()


def try_youtube_main_upload(config, dry_run):
    """Attempt YouTube main episode upload. Returns result dict."""
    from hypogeum.publishing.youtube import refresh_youtube_token, upload_video, upload_thumbnail

    video_path = EP_DIR / f"main_{EP_ID}.mp4"
    thumb_path = EP_DIR / "thumbnails" / "main.jpg"

    result = {
        "platform": "youtube",
        "deliverable_type": "main_episode",
        "video_path": str(video_path),
        "dry_run": dry_run,
        "status": None,
        "video_id": None,
        "url": None,
        "error": None,
    }

    if not video_path.exists():
        result["status"] = "failed"
        result["error"] = f"Video file not found: {video_path}"
        return result

    if dry_run:
        result["status"] = "dry_run_simulated"
        result["video_id"] = "dry_run_main_ep"
        result["url"] = "https://www.youtube.com/watch?v=dry_run_main_ep"
        print(f"  [DRY RUN] Would upload: {video_path.name} ({video_path.stat().st_size/1e6:.1f} MB)")
        print(f"  [DRY RUN] Title: {EPISODE_TITLE}")
        print(f"  [DRY RUN] Privacy: private")
        return result

    # Real upload
    try:
        print("  Refreshing YouTube OAuth token...")
        access_token = refresh_youtube_token(config)
        print("  Token refreshed OK")

        metadata = {
            "title": EPISODE_TITLE,
            "description": EPISODE_DESCRIPTION,
            "tags": ["Colosseum", "Ancient Rome", "History", "Documentary", "Hypogeum"],
            "categoryId": "27",  # Education
            "privacyStatus": "private",
            "madeForKids": False,
        }

        print(f"  Uploading {video_path.name} ({video_path.stat().st_size/1e6:.1f} MB) as PRIVATE...")
        video_id = upload_video(config, access_token, video_path, metadata, episode_id=EP_ID)
        print(f"  Upload complete: video_id={video_id}")
        result["video_id"] = video_id
        result["url"] = f"https://www.youtube.com/watch?v={video_id}"
        result["status"] = "uploaded_private"

        # Upload thumbnail
        if thumb_path.exists():
            print(f"  Uploading thumbnail...")
            ok = upload_thumbnail(config, access_token, video_id, thumb_path)
            result["thumbnail_applied"] = ok
            print(f"  Thumbnail: {'OK' if ok else 'FAILED'}")

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        print(f"  ERROR: {e}")

    return result


def try_youtube_shorts_upload(config, dry_run):
    """Upload all 5 shorts as private YouTube Shorts. Returns list of results."""
    from hypogeum.publishing.youtube import refresh_youtube_token, upload_video

    results = []

    # Get a token (or simulate in dry run)
    access_token = None
    if not dry_run:
        try:
            print("  Refreshing YouTube token for shorts...")
            access_token = refresh_youtube_token(config)
        except Exception as e:
            print(f"  Token refresh failed: {e}")
            for i in range(1, 6):
                results.append({
                    "short_num": i,
                    "status": "failed",
                    "error": f"Token refresh failed: {e}",
                })
            return results

    for i in range(1, 6):
        short_path = EP_DIR / "shorts" / f"short_{i}_{EP_ID}.mp4"
        theme = SHORT_THEMES[i - 1]
        description = SHORT_DESCRIPTIONS[i - 1]
        title = f"{theme} | The Hypogeum Chronicles #Shorts"

        result = {
            "short_num": i,
            "path": str(short_path),
            "title": title,
            "dry_run": dry_run,
            "status": None,
            "video_id": None,
            "url": None,
            "error": None,
        }

        if not short_path.exists():
            result["status"] = "failed"
            result["error"] = f"Short file not found: {short_path}"
            results.append(result)
            continue

        if dry_run:
            result["status"] = "dry_run_simulated"
            result["video_id"] = f"dry_run_short_{i}"
            result["url"] = f"https://www.youtube.com/watch?v=dry_run_short_{i}"
            print(f"  [DRY RUN] Short {i}: {short_path.name} ({short_path.stat().st_size/1e6:.1f} MB)")
            print(f"  [DRY RUN] Title: {title[:80]}")
            results.append(result)
            continue

        # Real upload
        try:
            metadata = {
                "title": title[:100],  # YouTube 100 char limit
                "description": description,
                "tags": ["Colosseum", "AncientRome", "Shorts", "History"],
                "categoryId": "27",
                "privacyStatus": "private",
                "madeForKids": False,
            }

            print(f"  Uploading short {i}: {short_path.name}...")
            video_id = upload_video(config, access_token, short_path, metadata, episode_id=EP_ID)
            result["video_id"] = video_id
            result["url"] = f"https://www.youtube.com/watch?v={video_id}"
            result["status"] = "uploaded_private"
            print(f"  Short {i} uploaded: {video_id}")

        except Exception as e:
            result["status"] = "failed"
            result["error"] = str(e)
            print(f"  Short {i} ERROR: {e}")

        results.append(result)

    return results


def main():
    print("=== OVERNIGHT UPLOAD: ep_20260629075717_c7abfd ===\n")

    config = load_config()
    dry_run = getattr(config, "YOUTUBE_DRY_RUN", True)
    # Also check env directly
    import os
    from dotenv import load_dotenv
    load_dotenv("/Users/gawadeditor/hypogeum-brain/.env")
    dry_run_env = os.environ.get("YOUTUBE_DRY_RUN", "true").lower() == "true"
    if dry_run_env:
        dry_run = True

    print(f"YOUTUBE_DRY_RUN: {dry_run}")
    print(f"Mode: {'DRY RUN (simulated)' if dry_run else 'REAL UPLOAD (private)'}\n")

    # Check if already uploaded (non-dry-run)
    pub_record_path = EP_DIR / f"publishing_record_{EP_ID}.json"
    prev_video_id = None
    if pub_record_path.exists():
        pub_record = json.loads(pub_record_path.read_text())
        for result in pub_record.get("platform_results", []):
            if (result.get("platform") == "youtube" and
                    result.get("deliverable_type") == "main_episode" and
                    result.get("status") == "published"):
                prev_vid = result.get("platform_video_id", "")
                if prev_vid and not prev_vid.startswith("dry_run"):
                    prev_video_id = prev_vid
                    print(f"Main episode already uploaded (real): video_id={prev_video_id}")
                    break

    # Main episode upload
    print("--- Main Episode Upload ---")
    if prev_video_id:
        main_result = {
            "platform": "youtube",
            "deliverable_type": "main_episode",
            "status": "already_uploaded",
            "video_id": prev_video_id,
            "url": f"https://www.youtube.com/watch?v={prev_video_id}",
            "skipped_reason": "already_uploaded_in_previous_session",
        }
        print(f"  Skipping re-upload — already at: {main_result['url']}")
    else:
        main_result = try_youtube_main_upload(config, dry_run)
        print(f"  Result: {main_result['status']}")

    # Shorts upload
    print("\n--- YouTube Shorts Upload ---")
    shorts_results = try_youtube_shorts_upload(config, dry_run)

    # Social platform checks
    meta_token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    tiktok_token = os.environ.get("TIKTOK_ACCESS_TOKEN", "").strip()

    platforms_skipped = []
    if not meta_token:
        platforms_skipped.append({"platform": "instagram", "reason": "META_ACCESS_TOKEN absent"})
        platforms_skipped.append({"platform": "facebook", "reason": "META_ACCESS_TOKEN absent"})
        print("\n  Instagram: SKIPPED — META_ACCESS_TOKEN absent")
        print("  Facebook: SKIPPED — META_ACCESS_TOKEN absent")

    # TikTok token is present but check if it's valid
    if tiktok_token:
        platforms_skipped.append({
            "platform": "tiktok",
            "reason": "TIKTOK_ACCESS_TOKEN present but shorts upload not in scope of overnight run (dry_run mode)"
        })
        print("  TikTok: SKIPPED — token present but upload deferred (dry run mode)")
    else:
        platforms_skipped.append({"platform": "tiktok", "reason": "TIKTOK_ACCESS_TOKEN absent"})
        print("  TikTok: SKIPPED — TIKTOK_ACCESS_TOKEN absent")

    # Build upload summary
    upload_summary = {
        "youtube_main": main_result,
        "youtube_shorts": shorts_results,
        "platforms_skipped": platforms_skipped,
    }

    print(f"\n=== UPLOAD SUMMARY ===")
    print(f"Main episode: {main_result['status']}")
    for s in shorts_results:
        print(f"  Short {s['short_num']}: {s['status']}")
    print(f"Skipped: {[p['platform'] for p in platforms_skipped]}")

    return upload_summary


if __name__ == "__main__":
    result = main()
    # Write intermediate result
    out = EP_DIR / "upload_result.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nUpload result saved to: {out}")
