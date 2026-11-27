#!/usr/bin/env python3
"""Inventory local footage and inspect the frames used by each edition."""

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

from media_info import framing_filter, probe


MEDIA_EXTENSIONS = set("""
    .mp4 .mov .m4v .mkv .avi .mts .m2ts .m2t .ts .webm .mxf .3gp .3g2
    .mpg .mpeg .m2v .vob .flv .wmv .ogv .m4a .m4b .mp3 .mp2 .wav .wave
    .bwf .aif .aiff .aifc .flac .aac .ogg .oga .opus .wma .caf .mka .amr
    .ape .alac .ac3 .eac3 .weba .au
""".split())
HEADERS = (
    "file", "status", "has_video", "width", "height", "fps", "duration_s",
    "has_audio", "integrated_lufs", "true_peak_dbtp", "audio_state",
    "event", "people", "location", "sync_sound", "usable_in_s", "usable_out_s", "notes",
)
PAGE_CUTS = 8
PERCENTAGES = (10, 50, 90)
STRIP_HEIGHT = 46


def _new_output(path, suffixes):
    path = Path(path).expanduser().absolute()
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"Expected an output ending in {' or '.join(suffixes)}")
    if os.path.lexists(path):
        raise FileExistsError(f"Output already exists at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _publish(pairs):
    # Exclusive creation also works on removable volumes that do not support hard links.
    created = []
    try:
        for source, destination in pairs:
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                created.append((destination, os.fstat(outgoing.fileno())))
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    except BaseException:
        for destination, ours in reversed(created):
            try:
                if os.path.samestat(ours, destination.lstat()):
                    destination.unlink()
            except FileNotFoundError:
                pass
        raise


def _local_media(root):
    def raise_walk_error(err):
        raise err

    for folder, dirs, names in os.walk(root, onerror=raise_walk_error):
        dirs[:] = sorted(n for n in dirs if n not in {".git", ".Trashes", ".Spotlight-V100",
                                                     ".fseventsd"})
        for name in sorted(names):
            path = Path(folder) / name
            if not name.startswith("._") and path.suffix.lower() in MEDIA_EXTENSIONS:
                yield path


def _ffmpeg(arguments, **kwargs):
    work = kwargs.get("cwd")
    if work is not None:
        env = os.environ.copy()
        env.update(TMPDIR=str(work), TMP=str(work), TEMP=str(work))
        kwargs["env"] = env
    try:
        return subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", *arguments], **kwargs
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Install ffmpeg and make it available on PATH.") from exc


def _log_tail(data):
    return " ".join(data.decode("utf-8", "replace").strip().splitlines()[-4:])[-500:]


def _audio_stats(path, stream_index, scratch):
    # Only the scanner's final JSON object is needed, even for feature-length sources.
    with tempfile.TemporaryFile(dir=scratch) as log:
        result = _ffmpeg(
            ["-v", "info", "-xerror", "-i", str(path), "-map", f"0:{stream_index}",
             "-vn", "-sn", "-dn", "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            cwd=scratch, stdout=subprocess.DEVNULL, stderr=log,
        )
        size = log.tell()
        log.seek(max(0, size - 65536))
        tail = log.read()
    if result.returncode:
        raise ValueError(_log_tail(tail) or "FFmpeg audio decoding failed")
    objects = re.findall(rb'\{\s*"input_i"\s*:[^{}]*\}', tail)
    if not objects:
        raise ValueError("FFmpeg produced no loudnorm measurement")
    try:
        stats = json.loads(objects[-1])
        loudness, peak = float(stats["input_i"]), float(stats["input_tp"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("FFmpeg produced an unreadable loudnorm measurement") from exc
    if peak == -math.inf and loudness == -math.inf:
        return "", "", "silent"
    if not math.isfinite(peak):
        raise ValueError("FFmpeg produced no finite true peak")
    if loudness == -math.inf:
        return "", f"{peak:.2f}", "below_gate"
    if not math.isfinite(loudness):
        raise ValueError("FFmpeg produced no finite integrated loudness")
    return f"{loudness:.2f}", f"{peak:.2f}", "measured"


def inventory(media_dir, output):
    root = Path(media_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Expected a media directory at {root}")
    destination = _new_output(output, (".csv",))
    count = errors = 0
    with tempfile.TemporaryDirectory(prefix=".wedding-inventory-", dir=destination.parent) as tmp:
        scratch = Path(tmp)
        report = scratch / "inventory.csv"
        with report.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS)
            writer.writeheader()
            for path in _local_media(root):
                count += 1
                # A leading ./ preserves round-trippable paths and safe spreadsheet text.
                row = {"file": "./" + path.relative_to(root).as_posix(), "status": "ok"}
                try:
                    info = probe(path)
                    row.update(has_video=int(info["has_video"]), has_audio=int(info["has_audio"]),
                               width=info["width"] or "", height=info["height"] or "",
                               fps=f'{info["fps"]:.6f}'.rstrip("0").rstrip(".") if info["fps"] else "",
                               duration_s=f'{info["duration"]:.6f}'.rstrip("0").rstrip(".")
                               if info["duration"] else "", audio_state="no_audio")
                    if info["has_audio"]:
                        row["audio_state"] = "measurement_error"
                        loudness, peak, state = _audio_stats(path, info["audio_stream_index"], scratch)
                        row.update(integrated_lufs=loudness, true_peak_dbtp=peak, audio_state=state)
                except (OSError, ValueError) as exc:
                    row.update(status="error", notes=f"Error ({exc})")
                    errors += 1
                writer.writerow(row)
        if not count:
            raise ValueError(f"No supported audio or video files found under {root}")
        _publish([(report, destination)])
    print(f"{destination} ({count} media files, {errors} with errors)")
    return 1 if errors else 0


# Numerals remain available when an FFmpeg distribution has no font renderer.
GLYPHS = {
    "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31), "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2), "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (6, 8, 16, 30, 17, 17, 14), "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14), "9": (14, 17, 17, 15, 1, 2, 12),
    "#": (10, 10, 31, 10, 31, 10, 10), "%": (25, 26, 2, 4, 8, 11, 19),
    ":": (0, 4, 4, 0, 4, 4, 0), ".": (0, 0, 0, 0, 0, 6, 6),
    " ": (0, 0, 0, 0, 0, 0, 0),
}


def _draw_numbers(strip, width, text, top, color):
    for i, char in enumerate(text):
        left = 9 + i * 12
        for y, bits in enumerate(GLYPHS[char]):
            for x in range(5):
                if not bits & (16 >> x) or left + x * 2 + 2 > width:
                    continue
                for dy in (0, 1):
                    start = ((top + 2 * y + dy) * width + left + 2 * x) * 3
                    strip[start:start + 6] = color * 2


def _timecode(seconds):
    total_ms = round(1000 * seconds)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def _label(width, shot, percent, when):
    strip = bytearray(bytes((32, 37, 40)) * (width * STRIP_HEIGHT))
    _draw_numbers(strip, width, f"#{shot:03d}  {percent}%", 6, bytes((245, 242, 233)))
    _draw_numbers(strip, width, _timecode(when), 26, bytes((169, 211, 206)))
    return strip


def _representatives(cut, fps, frame, thumb, scratch, use_lut):
    from edit_plan import crop_rect
    from render import source_window, video_fades, video_timing

    nframes = cut["frames"]
    indexes = [min(nframes - 1, nframes * percent // 100) for percent in PERCENTAGES]
    unique = sorted(set(indexes))
    seek, read_time, offset = source_window(cut["in"], cut["out"])
    selection = "select='" + "+".join(f"eq(n,{i})" for i in unique) + "'"
    fades = video_fades(cut, fps)
    filters = [video_timing(offset, cut["out"] - cut["in"], nframes, fps)]
    # Spatial transforms are independent of frame number. Fades count every input frame.
    if not fades:
        filters.append(selection)
    if use_lut:
        filters.append("lut3d=file=grade.cube")
    filters.append(framing_filter(cut, *frame, crop_rect))
    if fades:
        filters.extend([*fades, selection])
    filters.extend(["format=yuv420p", f"scale={thumb[0]}:{thumb[1]}:flags=lanczos", "setsar=1"])
    stream = cut.get("video_stream_index")
    result = _ffmpeg(
        ["-v", "error", "-xerror", "-ss", f"{seek:.9f}", "-t", f"{read_time:.9f}",
         "-i", str(cut["file"]), "-map", f"0:{stream}" if stream is not None else "0:V:0",
         "-an", "-sn", "-dn", "-filter_threads", "1", "-vf", ",".join(filters),
         "-vsync", "0", "-frames:v", str(len(unique)), "-pix_fmt", "rgb24",
         "-f", "rawvideo", "pipe:1"],
        capture_output=True, cwd=scratch,
    )
    if result.returncode:
        raise ValueError(f"Cannot decode shot {cut['id']} ({_log_tail(result.stderr)})")
    frame_bytes = thumb[0] * thumb[1] * 3
    if len(result.stdout) != frame_bytes * len(unique):
        raise ValueError(f"Missing representative frames for shot {cut['id']} in {cut['file']}")
    pictures = {
        index: result.stdout[i * frame_bytes:(i + 1) * frame_bytes]
        for i, index in enumerate(unique)
    }
    return [(pictures[i], cut["in"] + i / fps) for i in indexes]


def _font_option():
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    options = (
        Path("/System/Library/Fonts/PingFang.ttc"), Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), windows / "msyh.ttc",
        windows / "arial.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for font in options:
        if font.is_file():
            name = font.as_posix().replace(":", r"\:")
            return f"fontfile='{name}'"
    return "font=Sans"


def _short_id(value, units):
    text = " ".join(str(value).split())
    width = 0
    for i, char in enumerate(text):
        width += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        if width > units:
            return text[:max(0, i - 1)] + "…"
    return text


def _encode_page(scratch, raw, cuts, first_shot, thumb, page_number):
    width, height = thumb
    font = _font_option()
    layers = []
    for i, cut in enumerate(cuts):
        x = 9 + 12 * len(f"#{first_shot + i:03d}  90%") + 10
        label = _short_id(cut["id"], max(0, (width - x - 8) // 9))
        if label:
            name = f"id-{i}.txt"
            (scratch / name).write_text(label, encoding="utf-8")
            layers.append(
                f"drawtext={font}:textfile={name}:expansion=none:fontsize=16:"
                f"fontcolor=0xC3C9CF:x={x}:y={height + 4}:enable='between(n,{3*i},{3*i+2})'"
            )
    tile = f"tile=3x{len(cuts)}:nb_frames={3 * len(cuts)}:margin=12:padding=8:color=0x131719"
    path = scratch / f"page-{page_number}.jpg"
    args = ["-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{width}x{height + STRIP_HEIGHT}", "-framerate", "1", "-i", raw.name,
            "-filter_threads", "1"]
    end = ["-frames:v", "1", "-q:v", "2", "-pix_fmt", "yuvj444p", "-update", "1", path.name]
    result = _ffmpeg([*args, "-vf", ",".join([*layers, tile]), *end],
                     cwd=scratch, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode and layers:
        # Static shot numbers and source timecodes were baked before the optional font pass.
        result = _ffmpeg([*args, "-vf", tile, *end], cwd=scratch, stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE)
        if not result.returncode:
            print("Font rendering unavailable. The sheet uses numbered shots.", file=sys.stderr)
    if result.returncode or not path.is_file() or path.stat().st_size < 4:
        raise ValueError(f"Cannot assemble contact sheet page {page_number} ({_log_tail(result.stderr)})")
    return path


def sheet(plan, edition, output, scratch_dir=None):
    from edit_plan import frame_size, read_edition

    page_one = _new_output(output, (".jpg", ".jpeg"))
    cfg = read_edition(plan, edition)
    cuts, fps = cfg["cuts"], cfg["fps"]
    if not cuts:
        raise ValueError("The edition must contain at least one shot")
    frame = frame_size(edition, "draft")
    thumb = (448, 252) if edition == "landscape" else (252, 448)
    pages = [page_one]
    for page in range(2, (len(cuts) + PAGE_CUTS - 1) // PAGE_CUTS + 1):
        name = page_one.with_name(f"{page_one.stem}-{page:03d}{page_one.suffix}")
        pages.append(_new_output(name, (".jpg", ".jpeg")))
    base = Path(scratch_dir).expanduser().absolute() if scratch_dir else page_one.parent
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wedding-sheet-", dir=base) as tmp:
        scratch = Path(tmp)
        if cfg.get("lut"):
            shutil.copyfile(cfg["lut"], scratch / "grade.cube")
        ready = []
        for index, destination in enumerate(pages):
            start = PAGE_CUTS * index
            group = cuts[start:start + PAGE_CUTS]
            raw = scratch / "tiles.rgb"
            with raw.open("wb") as stream:
                for i, cut in enumerate(group):
                    frames = _representatives(cut, fps, frame, thumb, scratch, bool(cfg.get("lut")))
                    for percent, (picture, when) in zip(PERCENTAGES, frames):
                        stream.write(picture)
                        stream.write(_label(thumb[0], start + i + 1, percent, when))
            page = _encode_page(scratch, raw, group, start + 1, thumb, index + 1)
            raw.unlink()
            ready.append((page, destination))
        _publish(ready)
    for path in pages:
        print(path)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory", help="Inventory video, audio and whole-file loudness")
    inv.add_argument("media_dir")
    inv.add_argument("output", help="New CSV file")
    contact = commands.add_parser("sheet", help="Preview three frames from every shot")
    contact.add_argument("plan")
    contact.add_argument("edition", choices=("landscape", "portrait"))
    contact.add_argument("output", help="New JPEG file (additional pages end in -002, -003, …)")
    contact.add_argument("--scratch", help="Parent directory for temporary thumbnails")
    args = parser.parse_args()
    try:
        if args.command == "inventory":
            return inventory(args.media_dir, args.output)
        return sheet(args.plan, args.edition, args.output, args.scratch)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Review failed. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
