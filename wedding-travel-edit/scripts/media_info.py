"""Read local media metadata and match the displayed geometry in every export."""

import json
import math
import subprocess
from fractions import Fraction
from functools import lru_cache
from pathlib import Path


def _ratio(value):
    try:
        result = Fraction(str(value).replace(":", "/"))
        return result if result > 0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def _seconds(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _duration(stream):
    if not stream:
        return 0.0
    seconds = _seconds(stream.get("duration"))
    if seconds:
        return seconds
    time_base = _ratio(stream.get("time_base"))
    if time_base:
        seconds = _seconds(stream.get("duration_ts")) * float(time_base)
        if seconds:
            return seconds
    # Matroska often stores duration only as a HH:MM:SS.fraction tag.
    tag = stream.get("tags", {}).get("DURATION", "")
    try:
        hours, minutes, seconds = tag.split(":")
        return _seconds(int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    except (AttributeError, ValueError):
        return 0.0


def _stream(streams, kind):
    candidates = [s for s in streams if s.get("codec_type") == kind]
    if kind == "video":
        candidates = [
            s for s in candidates
            if not s.get("disposition", {}).get("attached_pic")
            and not s.get("disposition", {}).get("timed_thumbnails")
            and s.get("width", 0) > 0 and s.get("height", 0) > 0
        ]
    return max(candidates, key=lambda s: s.get("disposition", {}).get("default", 0),
               default=None)


def _rotation(video):
    for data in video.get("side_data_list", []):
        if "rotation" in data:
            angle = data["rotation"]
            break
    else:
        angle = video.get("tags", {}).get("rotate", 0)
    try:
        angle = float(angle)
        return angle % 360 if math.isfinite(angle) else 0.0
    except (ValueError, TypeError):
        return 0.0


@lru_cache(maxsize=128)
def _probe_cached(filename, identity):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
             filename],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Install ffprobe and make it available on PATH.") from exc
    if result.returncode:
        details = result.stderr.strip()[-500:] or "ffprobe returned no details"
        raise ValueError(f"Cannot read {filename} ({details})")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe returned invalid metadata for {filename}") from exc
    streams = metadata.get("streams", [])
    video, audio = _stream(streams, "video"), _stream(streams, "audio")
    if not video and not audio:
        raise ValueError(f"No playable video or audio stream in {filename}")

    width = height = 0
    rotation = fps = 0.0
    video_duration, audio_duration = _duration(video), _duration(audio)
    if video:
        coded_w, coded_h = video["width"], video["height"]
        sar = _ratio(video.get("sample_aspect_ratio")) or Fraction(1)
        width = max(1, int(coded_w * sar + Fraction(1, 2)))
        height = coded_h
        rotation = _rotation(video)
        # The FFmpeg input autorotation filter transposes quarter turns.
        if abs(rotation - 90) < 1 or abs(rotation - 270) < 1:
            width, height = height, width
        rate = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
        fps = float(rate) if rate else 0.0
        if not video_duration and fps:
            video_duration = _seconds(video.get("nb_frames")) / fps
    format_duration = _seconds(metadata.get("format", {}).get("duration"))
    return {
        "has_video": bool(video), "has_audio": bool(audio),
        "width": width, "height": height, "rotation": rotation, "fps": fps,
        "color_transfer": (video or {}).get("color_transfer") or "",
        "duration": format_duration or max(video_duration, audio_duration),
        "video_duration": video_duration, "audio_duration": audio_duration,
        "video_stream_index": video["index"] if video else None,
        "audio_stream_index": audio["index"] if audio else None,
    }


def probe(path):
    """Return display dimensions, seconds, rates and the selected absolute stream indices."""
    filename = Path(path).expanduser().resolve(strict=True)
    if not filename.is_file():
        raise ValueError(f"Expected a media file at {filename}")
    stat = filename.stat()
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return _probe_cached(str(filename), identity).copy()


def framing_filter(cut, target_w, target_h, crop_rect):
    """Return the same square-pixel FFmpeg framing chain for rendering and contact sheets."""
    width, height = cut["width"], cut["height"]
    if cut.get("fit", "cover") == "contain":
        ratio = min(Fraction(target_w, width), Fraction(target_h, height))
        # Even scaled dimensions keep the entire image inside a 4:2:0 delivery frame.
        scaled_w = min(target_w, max(2, 2 * int(width * ratio / 2)))
        scaled_h = min(target_h, max(2, 2 * int(height * ratio / 2)))
        return (f"scale={scaled_w}:{scaled_h}:flags=lanczos,setsar=1,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black")
    x, y, crop_w, crop_h = crop_rect(width, height, target_w, target_h, cut["focus"])
    return (f"scale={width}:{height}:flags=lanczos,setsar=1,"
            f"crop=w={crop_w}:h={crop_h}:x={x}:y={y}:exact=1,"
            f"scale={target_w}:{target_h}:flags=lanczos,setsar=1")
