"""
hypogeum/vertical_master.py — the ONE vertical-video delivery contract.

Every short-native deliverable this factory produces is a single MP4 that is
uploaded verbatim to TikTok, Instagram Reels, Facebook Reels and YouTube
Shorts — only the caption / hashtags / title / thumbnail differ per platform.
For one file to be accepted (and not silently down-ranked, re-encoded, or
music-blocked) on all four, it must sit inside the intersection of every
platform's spec. That intersection is defined here and nowhere else.

Consumers (all import from this module, none re-declare the numbers):
  - hypogeum/agents/video_renderer.py — encodes to VIDEO_* / AUDIO_* / COLOR_*
  - hypogeum/caption_style.py          — safe-area comes from SAFE_AREA_PX
  - hypogeum/agents/production_output_gate.py — fail-closed conformance check
  - hypogeum/publishing/cross_platform_readiness.py — references the same spec

Sources: TikTok Creator "video specifications" (9:16, 1080x1920, H.264/AAC,
<=60s recommended), Meta Reels publishing spec (9:16, 1080x1920, H.264 high
profile, AAC 48kHz stereo, <=90s), YouTube Shorts (vertical, <=3min — the
binding constraint here is TikTok/Reels, not Shorts). The 58s cap and the
"music outside the platform libraries" rule avoid the two cross-platform
failure modes that are not about the container: a >60s clip losing Shorts
eligibility / gaining stricter music rules, and a platform-library track
that clears on one app and is muted on another.
"""
from __future__ import annotations

# --- geometry --------------------------------------------------------------
WIDTH = 1080
HEIGHT = 1920
ASPECT_RATIO = "9:16"
SAMPLE_ASPECT_RATIO = "1:1"          # square pixels
SCAN = "progressive"

# --- duration -------------------------------------------------------------
# Hard render-time cap. Under 60s keeps YouTube Shorts eligibility and the
# lighter music-licensing regime on every platform; 58 (not 59) leaves head
# room for container/muxer rounding so the probe never reads 60.x.
MAX_DURATION_SECONDS = 58.0

# --- video --------------------------------------------------------------
VIDEO_CODEC = "libx264"             # H.264
VIDEO_PROFILE = "high"
VIDEO_LEVEL = "4.0"                 # 1080p30 fits comfortably; universally decodable
VIDEO_PIX_FMT = "yuv420p"           # 8-bit 4:2:0
FRAME_RATE = 30                     # constant
# Quality-targeted VBR with a platform-friendly ceiling. CRF drives quality;
# maxrate/bufsize cap the peak at the top of the platforms' recommended
# upload band. CRF 19 lands a motion-heavy 1080x1920 short around 10-16 Mbps
# (the platform recommendation) and a near-static bedtime short lower — by
# design: the platforms re-encode every upload anyway, so spending a fixed
# 12 Mbps on a calm scene only bloats the file with no downstream quality
# gain. The ceiling is the part that matters for cross-platform acceptance.
VIDEO_CRF = 19
VIDEO_MAXRATE = "16M"
VIDEO_BUFSIZE = "24M"
VIDEO_TARGET_BITRATE = "12M"        # -b:v hint; -crf wins when both are set

# --- audio --------------------------------------------------------------
AUDIO_CODEC = "aac"                 # AAC-LC
AUDIO_SAMPLE_RATE = 48000           # every platform's reference rate
AUDIO_CHANNELS = 2                  # stereo
AUDIO_BITRATE = "256k"              # inside the 192-320 kbps band

# --- colour ------------------------------------------------------------
# Rec.709 SDR, explicitly tagged. Untagged 1080p is interpreted inconsistently
# (some pipelines assume 601 for <=720 height, 709 otherwise) which shifts
# saturation after a platform re-encode.
COLOR_PRIMARIES = "bt709"
COLOR_TRC = "bt709"
COLOR_SPACE = "bt709"
COLOR_RANGE = "tv"                  # limited / video range

# --- in-frame safe area (px on the 1080x1920 canvas) --------------------
# No text or logo above SAFE_AREA_PX["top"]; nothing important below
# HEIGHT - SAFE_AREA_PX["bottom"]; keep clear of the left gutter and the
# right action rail. Titles / captions / subtitles live inside SAFE_BOX_PX.
SAFE_AREA_PX = {
    "top": 250,       # status bar + "Following / For You"
    "bottom": 500,    # username, caption, progress bar, nav, CTA sticker
    "left": 80,       # left gutter
    "right": 180,     # like / comment / share / audio-disc rail
}
SAFE_BOX_PX = {       # the rectangle text may occupy
    "x_min": SAFE_AREA_PX["left"],
    "x_max": WIDTH - SAFE_AREA_PX["right"],      # 900
    "y_min": SAFE_AREA_PX["top"],                # 250
    "y_max": HEIGHT - SAFE_AREA_PX["bottom"],    # 1420
}

# Fractions (some consumers, e.g. the ASS caption model, work in fractions).
SAFE_TOP_FRAC = SAFE_AREA_PX["top"] / HEIGHT           # ~0.130
SAFE_BOTTOM_FRAC = SAFE_AREA_PX["bottom"] / HEIGHT     # ~0.260
SAFE_LEFT_FRAC = SAFE_AREA_PX["left"] / WIDTH          # ~0.074
SAFE_RIGHT_FRAC = SAFE_AREA_PX["right"] / WIDTH        # ~0.167

RESOLUTION = f"{WIDTH}x{HEIGHT}"


def ffmpeg_video_args() -> list[str]:
    """The `-c:v … ` block every vertical encode uses. Order-independent.

    Rec.709 is written three ways on purpose: the container-level -color_*
    output flags, and the x264 VUI via -x264-params (colorprim/transfer/
    colormatrix) — the -color_* flags alone are silently dropped by some
    ffmpeg builds, so the x264-params line is the one that actually lands in
    the bitstream."""
    return [
        "-c:v", VIDEO_CODEC,
        "-profile:v", VIDEO_PROFILE,
        "-level:v", VIDEO_LEVEL,
        "-pix_fmt", VIDEO_PIX_FMT,
        "-r", str(FRAME_RATE),
        "-crf", str(VIDEO_CRF),
        "-b:v", VIDEO_TARGET_BITRATE,
        "-maxrate", VIDEO_MAXRATE,
        "-bufsize", VIDEO_BUFSIZE,
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-colorspace", COLOR_SPACE,
        "-color_primaries", COLOR_PRIMARIES,
        "-color_trc", COLOR_TRC,
        "-color_range", COLOR_RANGE,
        "-movflags", "+faststart",
    ]


def ffmpeg_audio_args() -> list[str]:
    """The `-c:a … ` block every vertical encode uses."""
    return [
        "-c:a", AUDIO_CODEC,
        "-b:a", AUDIO_BITRATE,
        "-ar", str(AUDIO_SAMPLE_RATE),
        "-ac", str(AUDIO_CHANNELS),
    ]


def conformance_findings(probe: dict) -> list[str]:
    """Given an ffprobe-style dict (keys: width, height, codec_name, profile,
    pix_fmt, r_frame_rate, sample_rate, channels, duration_s,
    color_primaries/transfer/space), return a list of spec violations —
    empty means the file is VERTICAL_MASTER-conformant. Unknown/missing
    fields are reported, never passed.
    """
    f: list[str] = []
    w, h = probe.get("width"), probe.get("height")
    if (w, h) != (WIDTH, HEIGHT):
        f.append(f"resolution {w}x{h} != {RESOLUTION}")
    vc = (probe.get("codec_name") or "").lower()
    if vc not in ("h264", "libx264", "avc1"):
        f.append(f"video codec {vc!r} != H.264")
    prof = (probe.get("profile") or "").lower()
    if prof and "high" not in prof:
        f.append(f"H.264 profile {prof!r} != High")
    if (probe.get("pix_fmt") or "") not in ("yuv420p", "yuvj420p"):
        f.append(f"pix_fmt {probe.get('pix_fmt')!r} != yuv420p")
    fr = str(probe.get("r_frame_rate") or "")
    if fr not in (f"{FRAME_RATE}/1", str(FRAME_RATE), f"{FRAME_RATE}000/1000"):
        f.append(f"frame rate {fr!r} != {FRAME_RATE}")
    sr = probe.get("sample_rate")
    try:
        if int(sr) != AUDIO_SAMPLE_RATE:
            f.append(f"audio sample rate {sr} != {AUDIO_SAMPLE_RATE}")
    except (TypeError, ValueError):
        f.append(f"audio sample rate unreadable ({sr!r})")
    ch = probe.get("channels")
    try:
        if int(ch) != AUDIO_CHANNELS:
            f.append(f"audio channels {ch} != {AUDIO_CHANNELS}")
    except (TypeError, ValueError):
        f.append(f"audio channels unreadable ({ch!r})")
    dur = probe.get("duration_s")
    try:
        if float(dur) > MAX_DURATION_SECONDS + 0.5:
            f.append(f"duration {float(dur):.2f}s > {MAX_DURATION_SECONDS}s")
    except (TypeError, ValueError):
        f.append(f"duration unreadable ({dur!r})")
    cp = (probe.get("color_primaries") or "").lower()
    if cp not in ("bt709", "unknown", ""):
        f.append(f"color primaries {cp!r} != bt709")
    if cp in ("unknown", ""):
        f.append("colour not tagged Rec.709 (primaries unknown)")
    return f
