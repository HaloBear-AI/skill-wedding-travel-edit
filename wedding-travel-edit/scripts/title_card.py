"""Render a typeset, silent title card for one edition of a wedding or travel film."""

import argparse
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from edit_plan import EDITIONS, PROFILES, frame_size


def text_lines(value, label):
    lines = value.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    lines = [line.strip().replace("\t", " ") for line in lines]
    if len(lines) > 2 or any(not line for line in lines):
        raise ValueError(f"{label}可写一至两行，请删去空行或缩短文案")
    if any(ord(char) < 32 for line in lines for char in line):
        raise ValueError(f"{label}含不可显示的控制字符，请检查文案")
    return lines


def ffmpeg(work, args):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n", *args],
        cwd=work, capture_output=True,
    )
    if result.returncode:
        reason = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg 执行失败。请检查字体、drawtext 滤镜和 libx264 编码器。\n{reason}")
    return result.stdout


def fits_width(work, draw, available):
    # Have drawtext measure the shaped text in the actual font. An overwide line
    # paints the 4×4 probe white. A fitting line is drawn fully off the probe.
    marker = (
        f"{draw}:x='if(gt(text_w,{available}),0,text_w+{available}+1000)':y=0"
        ":box=1:boxborderw=4:boxcolor=white:fontcolor=white"
    )
    pixels = ffmpeg(work, [
        "-f", "lavfi", "-i", "color=black:s=4x4:r=1",
        "-vf", marker, "-frames:v", "1", "-pix_fmt", "gray",
        "-threads:v", "1", "-f", "rawvideo", "pipe:1",
    ])
    if len(pixels) != 16:
        raise RuntimeError("FFmpeg 未能测量文字宽度")
    return max(pixels) < 128


def typography(edition, width, height, titles, subtitles):
    landscape = edition == "landscape"
    short = min(width, height)
    title_size = round(short * (0.099 if landscape else 0.087))
    subtitle_size = round(short * (0.034 if landscape else 0.040))
    gap = round(short * (0.041 if landscape else 0.057)) if subtitles else 0
    title_step = round(title_size * 1.28)
    subtitle_step = round(subtitle_size * 1.42)
    total = len(titles) * title_step + gap + len(subtitles) * subtitle_step
    top = round(height * (0.48 if landscape else 0.46) - total / 2)
    margin = round(width * (0.12 if landscape else 0.10))
    x = "(w-text_w)/2" if landscape else str(margin)
    rows = []
    for label, lines, size, step, color in (
        ("片名", titles, title_size, title_step, "0x282725"),
        ("副题", subtitles, subtitle_size, subtitle_step, "0x59544E"),
    ):
        if label == "副题":
            top += gap
        for number, line in enumerate(lines, 1):
            baseline = top + round(size * 0.95)
            rows.append((f"{label}第 {number} 行", line, size, color, x, baseline))
            top += step
    return rows, width - 2 * margin


def render(args):
    target = args.output.expanduser().absolute()
    if target.suffix.lower() != ".mp4":
        raise ValueError("输出文件请使用 .mp4 扩展名")
    if os.path.lexists(target):
        raise FileExistsError(f"输出已存在，请换一个文件名：{target}")
    font = args.font.expanduser().resolve()
    if not font.is_file():
        raise ValueError(f"找不到字体文件：{font}")
    if not 1 <= args.fps <= 120:
        raise ValueError("帧率需要 1–120 的整数")
    titles = text_lines(args.title, "片名")
    subtitles = text_lines(args.subtitle, "副题") if args.subtitle.strip() else []
    seconds = args.seconds
    if seconds is None:
        seconds = (2.6 if args.edition == "landscape" else 2.2)
        seconds += 0.6 * (len(titles) - 1) + 0.55 * len(subtitles)
    if not math.isfinite(seconds * args.fps) or seconds <= 0:
        raise ValueError("时长需要正的有限秒数")
    frames = math.floor(seconds * args.fps + 0.5)
    if frames < 1:
        raise ValueError("所选时长不足一帧")
    width, height = frame_size(args.edition, args.profile)
    rows, available = typography(args.edition, width, height, titles, subtitles)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = args.scratch.expanduser().resolve() if args.scratch else target.parent
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".title-card-", dir=scratch) as tmp:
        work = Path(tmp)
        # ASCII filter-local names keep quotes, colons and Unicode paths out of FFmpeg syntax.
        shutil.copyfile(font, work / "face.font")
        ramp = max(1, min(round(args.fps * 0.16), frames // 6))
        opacity = f"min(1,min((n+1)/{ramp},({frames}-n)/{ramp}))"
        filters = []
        for i, (label, line, size, color, x, baseline) in enumerate(rows):
            textfile = f"line-{i}.txt"
            (work / textfile).write_text(line, encoding="utf-8")
            draw = f"drawtext=fontfile=face.font:textfile={textfile}:expansion=none:fontsize={size}"
            if not fits_width(work, draw, available):
                raise ValueError(f"{label}超出本版 {available} 像素的文字区，请在词组间换行或缩短文案")
            filters.append(f"{draw}:fontcolor={color}:x={x}:y={baseline}-ascent:alpha='{opacity}'")
        ffmpeg(work, [
            "-f", "lavfi", "-i", f"color=0xF4F0E8:s={width}x{height}:r={args.fps}",
            "-vf", ",".join(filters), "-frames:v", str(frames), "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "17", "-threads:v", "4",
            "-movflags", "+faststart", "card.mp4",
        ])
        # Exclusive creation works across volumes, including exFAT without hard links.
        with (work / "card.mp4").open("rb") as src:
            dest = target.open("xb")
            try:
                with dest:
                    shutil.copyfileobj(src, dest)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
    print(f"{target} · {width}×{height} · {frames / args.fps:g} 秒 · {args.fps} fps")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="用指定字体生成横版或竖版的无声 MP4 片名卡。")
    parser.add_argument("edition", choices=EDITIONS)
    parser.add_argument("output", type=Path, metavar="OUTPUT.mp4")
    parser.add_argument("--title", required=True, help="片名，最多两行，可直接输入换行")
    parser.add_argument("--subtitle", default="", help="可选副题，最多两行")
    parser.add_argument("--font", required=True, type=Path, help="包含文案所用字形的本地字体文件")
    parser.add_argument("--seconds", type=float, help="横版默认 2.6 秒起，竖版 2.2 秒起，副题和第二行延长默认时长")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--profile", choices=PROFILES, default="hd")
    parser.add_argument("--scratch", type=Path, help="临时文件目录，默认用成片所在目录")
    try:
        render(parser.parse_args())
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
