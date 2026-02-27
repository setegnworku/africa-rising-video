#!/usr/bin/env python3
"""
create_video.py
═══════════════════════════════════════════════════════════════════════════════
Automated pipeline: PowerPoint PNG slides + narration script → narrated video.

Workflow:
  1. Parse narration script (script.txt) → per-slide text
  2. Generate MP3 audio for each slide via text-to-speech API
  3. Create MP4 video per slide (PNG image + audio) via ffmpeg
  4. Concatenate slide videos → single output
  5. Apply professional fade-in / fade-out

Requirements:
  pip install -r requirements.txt
  ffmpeg ≥ 4.x and ffprobe installed (available on PATH)
  TTS API key set as environment variable

Usage:
  export ELEVENLABS_API_KEY="your-key-here"
  python3 create_video.py

Author: Setegn Worku Alemu, PhD | ILRI / CGIAR
        Climate-Smart Cattle — Africa Rising Together
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — Edit these values as needed
# ═══════════════════════════════════════════════════════════════════════════════

# ElevenLabs API key. Reads from environment variable first; edit the fallback
# string below if you prefer to hardcode it (not recommended for shared repos).
# Set via terminal:  export ELEVENLABS_API_KEY="sk-..."
ELEVENLABS_API_KEY: str = os.environ.get("ELEVENLABS_API_KEY", "YOUR_API_KEY_HERE")

# ── Voice settings ────────────────────────────────────────────────────────────
VOICE_ID: str      = "pNInz6obpgDQGcFmaJgB"  # Adam — deep, warm, authoritative
MODEL_ID: str      = "eleven_multilingual_v2"
OUTPUT_FORMAT: str = "mp3_44100_128"           # 44.1 kHz, 128 kbps MP3

VOICE_STABILITY: float         = 0.5
VOICE_SIMILARITY_BOOST: float  = 0.75
VOICE_STYLE: float             = 0.0
VOICE_USE_SPEAKER_BOOST: bool  = True

# ── Video output settings ─────────────────────────────────────────────────────
TARGET_WIDTH:  int = 1920
TARGET_HEIGHT: int = 1080
VIDEO_FPS:     int = 24
VIDEO_CRF:     int = 18         # Constant Rate Factor: 0–51 (lower = higher quality)
VIDEO_PRESET: str  = "slow"     # ultrafast … veryslow; slower = better compression
AUDIO_BITRATE: str = "192k"     # AAC audio bitrate in final video

# ── Timing ────────────────────────────────────────────────────────────────────
SILENCE_PADDING: float = 0.5   # Seconds of silence appended to each slide's audio
FADE_DURATION: float   = 0.5   # Seconds for fade-in and fade-out on final video

# ── API reliability ───────────────────────────────────────────────────────────
MAX_RETRIES: int     = 3
RETRY_BACKOFF: float = 2.0     # Initial wait (seconds); doubles on each retry

# ── Behaviour flags ───────────────────────────────────────────────────────────
SKIP_EXISTING_AUDIO: bool = True   # Reuse existing .mp3 files (saves API credits)
CLEANUP: bool             = True   # Delete intermediate files after final video

# ── File names ────────────────────────────────────────────────────────────────
SCRIPT_FILENAME: str = "script.txt"
FINAL_OUTPUT: str    = "final_video.mp4"
_RAW_CONCAT: str     = "_raw_concat.mp4"   # temporary; deleted after fades applied
_CONCAT_LIST: str    = "_concat_list.txt"  # temporary; deleted after concatenation


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Parse narration script
# ═══════════════════════════════════════════════════════════════════════════════

def parse_script(script_path: Path) -> dict[int, str]:
    """
    Parse script.txt and return {slide_number: narration_text}.

    Expected file format:
        ===...
        SLIDE N — Title
        ===...
        Narration body text for slide N…

        ===...
        SLIDE N+1 — Next Title
        …

    Separator lines (=====, -----), the SLIDE header line itself, and the
    "END OF SCRIPT" footer are all stripped; only narration body text is kept.
    """
    text = script_path.read_text(encoding="utf-8")

    # Match "SLIDE <number>" at the start of a line (case-insensitive)
    slide_re = re.compile(r"^SLIDE\s+(\d+)\b", re.MULTILINE | re.IGNORECASE)
    matches = list(slide_re.finditer(text))

    if not matches:
        raise ValueError(f"No SLIDE markers found in '{script_path}'")

    slides: dict[int, str] = {}

    for idx, match in enumerate(matches):
        slide_num = int(match.group(1))

        # Body begins after the SLIDE header line ends
        line_end   = text.index("\n", match.start())
        body_start = line_end + 1

        # Body ends at the start of the next SLIDE marker (or EOF)
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)

        raw_body = text[body_start:body_end]

        # Remove separator lines and known footer tokens
        cleaned_lines: list[str] = []
        for line in raw_body.splitlines():
            stripped = line.strip()
            if re.fullmatch(r"[=\-]{3,}", stripped):
                continue                               # separator line (===, ---)
            if stripped.upper() in ("END OF SCRIPT", "END OF NARRATION"):
                continue
            cleaned_lines.append(stripped)

        # Collapse 3+ consecutive blank lines → single blank; trim outer whitespace
        body = "\n".join(cleaned_lines).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)

        if not body:
            print(f"  WARNING: Slide {slide_num} narration is empty.")

        slides[slide_num] = body

    return slides


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Generate audio via ElevenLabs TTS
# ═══════════════════════════════════════════════════════════════════════════════

def generate_audio(
    client: object,
    text: str,
    output_path: Path,
    slide_num: int,
    total_slides: int,
) -> None:
    """
    Convert `text` to speech and save as MP3 at `output_path`.

    Uses client.text_to_speech.convert() from the current ElevenLabs SDK.
    Retries up to MAX_RETRIES times with exponential back-off on any failure.
    """
    if SKIP_EXISTING_AUDIO and output_path.is_file() and output_path.stat().st_size > 0:
        print(f"  [skip] {output_path.name} already exists — reusing cached audio")
        return

    label = f"Slide {slide_num}/{total_slides}"
    print(f"  Generating audio for {label}…", end="", flush=True)

    # Import VoiceSettings — handle both current and older SDK package layouts
    VoiceSettings = _import_voice_settings()

    voice_settings = VoiceSettings(
        stability=VOICE_STABILITY,
        similarity_boost=VOICE_SIMILARITY_BOOST,
        style=VOICE_STYLE,
        use_speaker_boost=VOICE_USE_SPEAKER_BOOST,
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            audio_stream: Iterator[bytes] = client.text_to_speech.convert(
                voice_id=VOICE_ID,
                text=text,
                model_id=MODEL_ID,
                output_format=OUTPUT_FORMAT,
                voice_settings=voice_settings,
            )
            # Collect all streamed byte chunks into a single MP3 file
            audio_bytes = b"".join(audio_stream)
            output_path.write_bytes(audio_bytes)
            size_kb = output_path.stat().st_size / 1024
            print(f" done ({size_kb:.0f} KB)")
            return

        except Exception as exc:
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                print(
                    f"\n    Attempt {attempt} failed: {exc}. "
                    f"Retrying in {wait:.0f}s…"
                )
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"ElevenLabs TTS failed for Slide {slide_num} "
                    f"after {MAX_RETRIES} attempts: {exc}"
                ) from exc


def _import_voice_settings() -> type:
    """Import VoiceSettings, tolerating different ElevenLabs SDK versions."""
    for module_path in (
        "elevenlabs",
        "elevenlabs.types",
        "elevenlabs.models",
    ):
        try:
            import importlib
            mod = importlib.import_module(module_path)
            if hasattr(mod, "VoiceSettings"):
                return mod.VoiceSettings
        except ImportError:
            continue
    raise ImportError(
        "Cannot find VoiceSettings in the ElevenLabs SDK. "
        "Run: pip install --upgrade elevenlabs"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Create individual slide videos with ffmpeg
# ═══════════════════════════════════════════════════════════════════════════════

def get_duration(path: Path) -> float:
    """
    Return the duration of any media file (audio or video) in seconds.
    Uses ffprobe with JSON output; prefers format-level duration (most reliable).
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    # Format-level duration is the most reliable source
    fmt = data.get("format", {})
    if "duration" in fmt:
        return float(fmt["duration"])

    # Fall back to the first stream that reports a duration
    for stream in data.get("streams", []):
        if "duration" in stream:
            return float(stream["duration"])

    raise ValueError(f"Cannot determine duration for: {path}")


def pad_audio(src: Path, dst: Path, extra_seconds: float) -> None:
    """
    Append `extra_seconds` of silence to `src` and write the result to `dst`.
    Creates a brief audio buffer between slides for smoother transitions.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-af", f"apad=pad_dur={extra_seconds}",
        "-c:a", "libmp3lame",
        "-b:a", AUDIO_BITRATE,
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def create_slide_video(
    image_path: Path,
    audio_path: Path,
    output_path: Path,
    slide_num: int,
) -> None:
    """
    Combine a static PNG image and an MP3 audio file into a Full-HD MP4 segment.

    - Image is scaled to fit 1920×1080, with black letterbox/pillarbox bars.
    - Output duration exactly matches audio duration.
    - Uses libx264 + AAC for maximum compatibility.
    """
    duration = get_duration(audio_path)
    print(f"  Slide {slide_num:>2}: {duration:.2f}s  →  {output_path.name}")

    # Video filter chain:
    #   scale   → fit image inside 1920×1080, preserving aspect ratio
    #   pad     → add black bars to fill the remaining space
    #   format  → convert to yuv420p for maximum player compatibility
    vf = (
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",                         # loop still image as video input
        "-framerate", str(VIDEO_FPS),         # input frame rate for the image
        "-i", str(image_path),                # input 1: PNG image
        "-i", str(audio_path),                # input 2: MP3 audio
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-r", str(VIDEO_FPS),                 # explicit output frame rate
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-t", f"{duration:.6f}",              # limit output to exact audio duration
        "-shortest",                          # safety: stop at shortest stream
        "-movflags", "+faststart",            # optimise MP4 atom placement for web
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Concatenate slide videos
# ═══════════════════════════════════════════════════════════════════════════════

def concatenate_videos(video_paths: list[Path], output_path: Path) -> None:
    """
    Concatenate MP4 segments using the ffmpeg concat demuxer (stream copy, fast).
    All segments must share identical codec, resolution, and sample-rate settings,
    which is guaranteed because they were all created by create_slide_video().
    """
    concat_list = output_path.parent / _CONCAT_LIST

    with concat_list.open("w", encoding="utf-8") as fh:
        for vp in video_paths:
            # Absolute path with forward slashes; no single quotes in our paths
            path_str = str(vp.resolve()).replace("\\", "/")
            fh.write(f"file '{path_str}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",           # allow absolute paths in the concat list
        "-i", str(concat_list),
        "-c", "copy",           # stream copy — no re-encode; preserves quality
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)

    concat_list.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Apply professional fade-in / fade-out
# ═══════════════════════════════════════════════════════════════════════════════

def apply_fades(input_path: Path, output_path: Path) -> None:
    """
    Re-encode the concatenated video adding:
      - Fade-in  from black at the very start  (FADE_DURATION seconds)
      - Fade-out to   black at the very end    (FADE_DURATION seconds)
    Both video and audio tracks are faded.
    """
    total_dur      = get_duration(input_path)
    fade_out_start = max(0.0, total_dur - FADE_DURATION)

    vf = (
        f"fade=t=in:st=0:d={FADE_DURATION:.3f},"
        f"fade=t=out:st={fade_out_start:.3f}:d={FADE_DURATION:.3f}"
    )
    af = (
        f"afade=t=in:st=0:d={FADE_DURATION:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={FADE_DURATION:.3f}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def find_script_file(work_dir: Path) -> Path:
    """
    Search for SCRIPT_FILENAME in `work_dir`, then its parent directory.
    Returns the first match found.
    """
    for directory in (work_dir, work_dir.parent):
        candidate = directory / SCRIPT_FILENAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"'{SCRIPT_FILENAME}' not found in:\n"
        f"  {work_dir}\n"
        f"  {work_dir.parent}"
    )


def find_slide_images(work_dir: Path) -> dict[int, Path]:
    """
    Scan `work_dir` for PNG files whose names match slide<N>.png
    (case-insensitive, e.g. Slide1.PNG, slide12.png).
    Returns a sorted dict: {slide_number: Path}.
    """
    pattern = re.compile(r"^slide(\d+)\.png$", re.IGNORECASE)
    images: dict[int, Path] = {}
    for f in work_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            images[int(m.group(1))] = f
    return dict(sorted(images.items()))


def seconds_to_hms(seconds: float) -> str:
    """Format a duration as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def require_tool(name: str) -> None:
    """Exit with a clear error message if `name` is not on PATH."""
    if not shutil.which(name):
        sys.exit(
            f"ERROR: '{name}' is not installed or not on PATH.\n"
            f"       Install it:  sudo apt install ffmpeg"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    banner = "Narrated Video Generator — Africa Rising: Climate-Smart Cattle"
    line   = "=" * len(banner)
    print(line)
    print(banner)
    print(line)

    # ── Validate environment ──────────────────────────────────────────────────
    require_tool("ffmpeg")
    require_tool("ffprobe")

    if ELEVENLABS_API_KEY in ("", "YOUR_API_KEY_HERE"):
        sys.exit(
            "ERROR: ElevenLabs API key is not set.\n"
            "       Run:  export ELEVENLABS_API_KEY='your-key-here'\n"
            "       Or edit ELEVENLABS_API_KEY at the top of this script."
        )

    try:
        from elevenlabs import ElevenLabs
    except ImportError:
        sys.exit(
            "ERROR: ElevenLabs SDK not installed.\n"
            "       Run:  pip install elevenlabs"
        )

    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    work_dir = Path(__file__).parent.resolve()

    # ── Step 1: Locate files and parse script ─────────────────────────────────
    print("\n[1/5] Locating files and parsing narration script …")

    script_path = find_script_file(work_dir)
    print(f"      Script   : {script_path}")

    slide_images = find_slide_images(work_dir)
    if not slide_images:
        sys.exit(f"ERROR: No Slide*.PNG files found in {work_dir}")

    total_slides = len(slide_images)
    print(
        f"      Images   : {total_slides} PNG file(s) "
        f"(Slides {min(slide_images)}–{max(slide_images)})"
    )

    narration = parse_script(script_path)
    print(f"      Narration: {len(narration)} slide section(s) parsed")

    # Validate that every PNG has a matching narration section (and vice versa)
    only_images    = sorted(set(slide_images) - set(narration))
    only_narration = sorted(set(narration)    - set(slide_images))

    if only_images:
        sys.exit(
            f"ERROR: Slides {only_images} have PNG images but no narration text.\n"
            f"       Check that script.txt has matching SLIDE markers."
        )
    if only_narration:
        # Non-fatal: just warn if there's narration for slides without images
        print(
            f"      WARNING: Narration found for slides {only_narration} "
            f"but no matching PNG — those slides will be skipped."
        )

    # Only process slides that have both image and narration
    slide_numbers = sorted(set(slide_images) & set(narration))

    # ── Step 2: Generate audio ────────────────────────────────────────────────
    print(f"\n[2/5] Generating narration audio via ElevenLabs …")

    audio_files: dict[int, Path] = {}
    for n in slide_numbers:
        mp3_path = work_dir / f"slide{n}.mp3"
        generate_audio(client, narration[n], mp3_path, n, total_slides)
        audio_files[n] = mp3_path

    # ── Step 3: Pad audio and build slide video segments ──────────────────────
    print(f"\n[3/5] Building {total_slides} slide video segment(s) …")

    padded_files: list[Path] = []
    slide_videos:  list[Path] = []

    for n in slide_numbers:
        raw_mp3    = audio_files[n]
        padded_mp3 = work_dir / f"slide{n}_padded.mp3"
        slide_mp4  = work_dir / f"slide{n}.mp4"

        # Append 0.5s of silence so adjacent slides don't feel abrupt
        pad_audio(raw_mp3, padded_mp3, SILENCE_PADDING)
        padded_files.append(padded_mp3)

        create_slide_video(slide_images[n], padded_mp3, slide_mp4, n)
        slide_videos.append(slide_mp4)

    # ── Step 4: Concatenate all slide videos ──────────────────────────────────
    print(f"\n[4/5] Concatenating {total_slides} segments …")

    raw_concat = work_dir / _RAW_CONCAT
    concatenate_videos(slide_videos, raw_concat)

    raw_concat_dur = get_duration(raw_concat)
    print(f"      Raw concat: {seconds_to_hms(raw_concat_dur)}  ({raw_concat_dur:.1f}s)")

    # ── Step 5: Apply fade-in / fade-out ─────────────────────────────────────
    print(f"\n[5/5] Applying {FADE_DURATION}s fade-in / fade-out …")

    final_output = work_dir / FINAL_OUTPUT
    apply_fades(raw_concat, final_output)
    raw_concat.unlink(missing_ok=True)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_dur = get_duration(final_output)
    size_mb   = final_output.stat().st_size / (1024 * 1024)

    print()
    print(f"  Output file  : {final_output.name}")
    print(f"  Duration     : {seconds_to_hms(total_dur)}  ({total_dur:.1f}s)")
    print(f"  File size    : {size_mb:.1f} MB")
    print(f"  Resolution   : {TARGET_WIDTH}×{TARGET_HEIGHT} @ {VIDEO_FPS} fps")

    # ── Cleanup intermediate files ────────────────────────────────────────────
    if CLEANUP:
        print("\nCleaning up intermediate files …")
        to_delete = (
            list(audio_files.values())  # slide1.mp3 … slide12.mp3
            + padded_files              # slide1_padded.mp3 …
            + slide_videos              # slide1.mp4 …
        )
        for f in to_delete:
            if f.exists():
                f.unlink()
                print(f"  deleted {f.name}")

    print()
    print(line)
    print("  DONE!  Your video is ready:")
    print(f"  {final_output}")
    print(line)


if __name__ == "__main__":
    main()
