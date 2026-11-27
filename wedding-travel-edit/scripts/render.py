#!/usr/bin/env python3
"""Render a validated wedding-travel edition with local FFmpeg and ffprobe."""

import argparse
import errno
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from edit_plan import crop_rect, frame_size, read_edition
from media_info import framing_filter


RATE = 48000
STEREO_FLOAT_BYTES = 8
TRUE_PEAK_DB = -1.5
PROFILES = {"draft": ("veryfast", 24), "hd": ("fast", 19), "uhd": ("fast", 18)}


def number(value):
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def units_at_frame(frames, units_per_second, fps):
    return (frames * units_per_second + fps // 2) // fps


def source_window(start, end):
    """Include a short handle for non-keyframe cuts, held frames and audio delay."""
    seek = max(0.0, start - 1.0)
    return seek, end - seek + 0.1, start - seek


def video_timing(offset, span, frames, fps):
    return ",".join([
        f"trim=end={number(offset + span)}",
        f"setpts=PTS-{number(offset)}/TB",
        f"fps=fps={fps}:start_time=0:round=near:eof_action=pass",
        f"tpad=stop_mode=clone:stop={frames}",
        f"trim=end_frame={frames}",
        f"setpts=N/({fps}*TB)",
    ])


def video_fades(cut, fps):
    frames = cut["frames"]
    filters = []
    for direction in ("in", "out"):
        seconds = cut.get("fade_" + direction, 0)
        if seconds <= 0:
            continue
        if frames == 1:
            return ["fade=t=in:s=0:n=1"]
        length = min(frames - 1, max(1, round(seconds * fps)))
        start = 0 if direction == "in" else frames - 1 - length
        filters.append(f"fade=t={direction}:s={start}:n={length}")
    return filters


def run_tool(command, work, action):
    env = os.environ.copy()
    env.update(TMPDIR=str(work), TMP=str(work), TEMP=str(work))
    result = subprocess.run(
        [str(arg) for arg in command], cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3500:]
        raise RuntimeError(f"{action}失败 ({command[0]} {result.returncode})\n{detail}")
    return result


def ffmpeg(level="error"):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-xerror", "-loglevel", level,
            "-n", "-filter_complex_threads", "4"]


def stream_label(input_number, source, kind):
    index = source.get(kind + "_stream_index")
    selector = index if index is not None else {"video": "V:0", "audio": "a:0"}[kind]
    return f"[{input_number}:{selector}]"


def window_input(path, seek, read_time):
    return ["-ss", number(seek), "-t", number(read_time), "-i", path]


def audio_window(offset, span, samples, gain_db):
    start = round(offset * RATE)
    end = max(start + 1, round((offset + span) * RATE))
    return ",".join([
        f"aresample={RATE}:async=1:first_pts=0",
        "aformat=sample_fmts=fltp:channel_layouts=stereo",
        f"atrim=start_sample={start}:end_sample={end}",
        "asetpts=N/SR/TB", f"volume={number(gain_db)}dB",
        f"apad=whole_len={samples}", f"atrim=end_sample={samples}", "asetpts=N/SR/TB",
    ])


def render_cut(work, cut, index, fps, size, profile, use_lut, samples, bed):
    start, end, frames = cut["in"], cut["out"], cut["frames"]
    seek, read_time, offset = source_window(start, end)
    video = [video_timing(offset, end - start, frames, fps)]
    if use_lut:
        video.append("lut3d=file=grade.cube")
    video.append(framing_filter(cut, *size, crop_rect))
    video.extend(video_fades(cut, fps))
    video.append("format=yuv420p")
    graph = [stream_label(0, cut, "video") + ",".join(video) + "[picture]"]
    sound = cut["has_audio"] and cut["sound_db"] is not None
    if sound:
        graph.append(stream_label(0, cut, "audio") +
                     audio_window(offset, end - start, samples, cut["sound_db"]) + "[sound]")

    name = f"shot-{index:06d}.mp4"
    preset, crf = PROFILES[profile]
    command = ffmpeg() + window_input(cut["file"], seek, read_time)
    command += [
        "-filter_complex", ";".join(graph), "-map", "[picture]", "-an",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-bf", "0",
        "-threads", str(min(os.cpu_count() or 1, 8)), "-pix_fmt", "yuv420p",
        "-video_track_timescale", str(fps * 1000), "-movie_timescale", str(math.lcm(RATE, fps)),
        "-map_metadata", "-1", "-map_chapters", "-1", "-metadata:s:v:0", "rotate=0", name,
    ]
    pcm = work / "shot.f32"
    if sound:
        command += ["-map", "[sound]", "-vn", "-c:a", "pcm_f32le", "-f", "f32le", pcm]
    action = f"镜头 {index + 1} {cut['id']} ({cut['file']})"
    run_tool(command, work, action)
    count = run_tool(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=nb_frames", "-of", "csv=p=0", name], work, action,
    ).stdout.strip()
    if count != str(frames):
        raise ValueError(f"{action} 需要 {frames} 帧，实际得到 {count or 0} 帧。请核查素材时间范围。")

    expected = samples * STEREO_FLOAT_BYTES
    copied = 0
    if sound:
        copied = pcm.stat().st_size
        if copied > expected or copied % STEREO_FLOAT_BYTES:
            raise RuntimeError(f"{action} 返回了错误的原声采样长度")
        with pcm.open("rb") as source:
            shutil.copyfileobj(source, bed, 1024 * 1024)
        pcm.unlink()
    # Sparse zero-filled gaps also cover shots whose audio starts after this cut.
    if copied < expected:
        bed.seek(expected - copied, os.SEEK_CUR)
        bed.truncate()
    return name


def duck_filters(windows, attenuation_db):
    if not windows or attenuation_db == 0:
        return []
    # Bound frame length for timeline enables. Within each window the gain is
    # evaluated per sample, including partial frames at both boundaries.
    filters = ["asetnsamples=n=480:p=0"]
    gain_delta = number(10 ** (attenuation_db / 20) - 1)
    for start, end in windows:
        if round(end * RATE) <= round(start * RATE):
            continue
        attack = min(0.15, (end - start) / 3)
        release = min(0.28, (end - start) / 3)
        depth = (f"clip(min((t-{number(start)})/{number(attack)},"
                 f"({number(end)}-t)/{number(release)}),0,1)")
        gain = f"(1+({gain_delta})*{depth})"
        filters.append(
            f"aeval=exprs='val(0)*{gain}|val(1)*{gain}':c=same:"
            f"enable='between(t,{number(start - 0.01)},{number(end)})'"
        )
    return filters


def raw_input(path):
    return ["-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", path]


def loudnorm(target):
    return f"loudnorm=I={number(target)}:TP={TRUE_PEAK_DB}:LRA=11:print_format=json"


def loudness_stats(log):
    # loudnorm prints a JSON object on stderr alongside ordinary FFmpeg messages.
    marker = log.rfind('"input_i"')
    if marker >= 0:
        start = log.rfind("{", 0, marker)
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(log[start:])[0]
            except ValueError:
                pass
    raise RuntimeError("无法读取 FFmpeg 的响度测量结果")


def measure_mix(work, cfg, samples):
    command = ffmpeg("info") + raw_input("location.f32")
    graph = []
    labels = ["[0:a:0]"]
    for role in ("voice", "music"):
        track = cfg[role]
        if track is None:
            continue
        span = track["duration"]
        seek, read_time, offset = source_window(track["in"], track["in"] + span)
        command += window_input(track["file"], seek, read_time)
        filters = [
            audio_window(offset, span, max(1, round(span * RATE)), track["gain_db"]),
            f"adelay=delays={round(track['at'] * RATE)}S:all=1",
        ]
        if role == "music":
            filters.extend(duck_filters(cfg["duck"], cfg["duck_db"]))
        label = f"[{role}]"
        graph.append(stream_label(len(labels), track, "audio") + ",".join(filters) + label)
        labels.append(label)
    full_mix = "".join(labels)
    if len(labels) > 1:
        full_mix += f"amix=inputs={len(labels)}:duration=first:normalize=0:dropout_transition=0,"
    fade = max(1, min(round(0.24 * RATE), samples // 8))
    graph.append(full_mix + f"afade=t=out:ss={samples - fade}:ns={fade},"
                 "asplit=2[original][measure]")
    graph.append("[measure]" + loudnorm(cfg["target_lufs"]) + "[meter]")
    (work / "mix.ffgraph").write_text(";\n".join(graph), encoding="utf-8")
    command += [
        "-filter_complex_script", "mix.ffgraph", "-map", "[original]", "-c:a", "pcm_f32le",
        "-f", "f32le", "mix.f32", "-map", "[meter]", "-f", "null", "-",
    ]
    result = run_tool(command, work, "叠加声轨并测量响度")
    if (work / "mix.f32").stat().st_size != samples * STEREO_FLOAT_BYTES:
        raise RuntimeError("混音采样数与成片时长不一致")
    (work / "location.f32").unlink()
    return loudness_stats(result.stderr)


def normalization_filter(stats, target):
    fields = {"input_i": "measured_I", "input_tp": "measured_TP", "input_lra": "measured_LRA",
              "input_thresh": "measured_thresh", "target_offset": "offset"}
    values = {key: float(stats[key]) for key in fields}
    if all(math.isfinite(v) for v in values.values()):
        limits = {"input_i": (-99, 0), "input_tp": (-99, 99), "input_lra": (0, 99),
                  "input_thresh": (-99, 0), "target_offset": (-99, 99)}
        if all(low <= values[k] <= high for k, (low, high) in limits.items()):
            params = ":".join(f"{field}={number(values[key])}" for key, field in fields.items())
            return loudnorm(target) + ":" + params + ":linear=true", True
        return loudnorm(target), True
    # EBU integrated loudness is undefined below the gate or for very short
    # programs. Preserve the planned gains, with attenuation for a hot peak.
    peak = values["input_tp"]
    gain = min(0.0, TRUE_PEAK_DB - peak) if math.isfinite(peak) else 0.0
    return f"volume={number(gain)}dB", False


def mux_master(work, stage, cfg, samples, stats):
    normalizer, measured = normalization_filter(stats, cfg["target_lufs"])
    audio_filter = (normalizer + f",aresample={RATE},apad=whole_len={samples},"
                    f"atrim=end_sample={samples},asetpts=N/SR/TB")
    fps = cfg["fps"]
    command = ffmpeg("info") + ["-f", "concat", "-safe", "1", "-i", "pictures.ffconcat"]
    command += raw_input("mix.f32") + [
        "-map", "0:v:0", "-c:v", "copy", "-map", "1:a:0", "-af", audio_filter,
        "-c:a", "aac", "-b:a", "256k", "-ar", str(RATE), "-ac", "2",
        "-video_track_timescale", str(fps * 1000), "-movie_timescale", str(math.lcm(RATE, fps)),
        "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", stage,
    ]
    result = run_tool(command, work, "封装 H.264/AAC 成片")
    if measured:
        final = loudness_stats(result.stderr)
        return f"编码前响度 {final['output_i']} LUFS，目标 {cfg['target_lufs']} LUFS"
    return "节目过短、静音或低于响度门限，已保留混音增益并保护峰值"


def verify_master(work, stage, size, frames, fps, samples):
    result = run_tool(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", stage], work, "核验成片",
    )
    streams = json.loads(result.stdout)["streams"]
    if len(streams) != 2:
        raise RuntimeError("成片需要一条画面和一条声音轨")
    video, audio = streams
    if not (video["codec_name"] == "h264" and (video["width"], video["height"]) == size and
            int(video.get("nb_frames", 0)) == frames and Fraction(video["avg_frame_rate"]) == fps and
            audio["codec_name"] == "aac" and audio["channels"] == 2 and int(audio["sample_rate"]) == RATE):
        raise RuntimeError("H.264/AAC 编码、尺寸、帧数、帧率或声道未通过成片核验")
    video_time = int(video["duration_ts"]) * Fraction(video["time_base"])
    audio_time = int(audio["duration_ts"]) * Fraction(audio["time_base"])
    if video_time != Fraction(frames, fps) or abs(audio_time - Fraction(samples, RATE)) > Fraction(1, RATE):
        raise RuntimeError(f"成片音画时长与计划不符（画面 {float(video_time):.6f}s，"
                           f"声音 {float(audio_time):.6f}s，计划 {frames / fps:.6f}s）")


def remove_owned(path, identity):
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def publish_new(stage, destination):
    """Publish without replacing an existing name, including on exFAT."""
    info = stage.stat()
    try:
        os.link(stage, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        unsupported = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EMLINK,
                       errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
        if exc.errno not in unsupported:
            raise
    else:
        return info.st_dev, info.st_ino

    with stage.open("rb") as source:
        final = destination.open("xb")
        info = os.fstat(final.fileno())
        identity = (info.st_dev, info.st_ino)
        try:
            with final:
                shutil.copyfileobj(source, final, 1024 * 1024)
                final.flush()
                os.fsync(final.fileno())
        except BaseException:
            remove_owned(destination, identity)
            raise
    return identity


def render(plan, edition, output, profile, scratch):
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise ValueError(f"请先安装 {tool} 并将其加入 PATH")
    output = Path(output).expanduser()
    output = output.parent.resolve() / output.name
    if output.suffix.lower() != ".mp4":
        raise ValueError("输出文件名需要使用 .mp4 扩展名")
    cfg = read_edition(plan, edition)
    hdr = dict.fromkeys(cut["id"] for cut in cfg["cuts"]
                        if cut.get("color_transfer") in ("smpte2084", "arib-std-b67"))
    if hdr:
        raise ValueError(f"请先将 HDR (PQ/HLG) 镜头转换为 SDR，再渲染 8-bit 成片：{', '.join(hdr)}")
    size = tuple(frame_size(edition, profile))
    sidecar = output.with_suffix(".srt") if cfg["srt"] else None
    for path in (output, sidecar):
        if path is not None and os.path.lexists(path):
            raise FileExistsError(f"输出已存在，请使用新文件名：{path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(scratch).expanduser().resolve() if scratch else output.parent
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".travel-edit-", dir=scratch) as scratch_dir, \
            tempfile.TemporaryDirectory(prefix=".travel-export-", dir=output.parent) as stage_dir:
        work = Path(scratch_dir)
        stage = Path(stage_dir) / "master.mp4"
        if cfg["lut"]:
            shutil.copyfile(cfg["lut"], work / "grade.cube")
        if sidecar:
            shutil.copyfile(cfg["srt"], stage.with_suffix(".srt"))

        fps = cfg["fps"]
        frames = 0
        manifest = ["ffconcat version 1.0"]
        with (work / "location.f32").open("wb") as bed:
            for index, cut in enumerate(cfg["cuts"]):
                print(f"[{index + 1}/{len(cfg['cuts'])}] {cut['id']}", file=sys.stderr, flush=True)
                end = frames + cut["frames"]
                samples = units_at_frame(end, RATE, fps) - units_at_frame(frames, RATE, fps)
                name = render_cut(work, cut, index, fps, size, profile, cfg["lut"], samples, bed)
                # Quantize cumulative boundaries so microsecond rounding never accumulates.
                micros = units_at_frame(end, 1000000, fps) - units_at_frame(frames, 1000000, fps)
                manifest.extend([f"file {name}", f"duration {micros / 1000000:.6f}"])
                frames = end
        (work / "pictures.ffconcat").write_text("\n".join(manifest) + "\n", encoding="ascii")
        samples = units_at_frame(frames, RATE, fps)
        stats = measure_mix(work, cfg, samples)
        message = mux_master(work, stage, cfg, samples, stats)
        verify_master(work, stage, size, frames, fps, samples)

        outputs = []
        if sidecar:
            outputs.append((stage.with_suffix(".srt"), sidecar))
        outputs.append((stage, output))
        published = []
        try:
            for source, dest in outputs:
                published.append((dest, publish_new(source, dest)))
        except BaseException:
            for dest, identity in reversed(published):
                remove_owned(dest, identity)
            raise
    print(f"{output}\n{size[0]}×{size[1]}，{frames} 帧，{fps} fps，{frames / fps:.3f} 秒。{message}。")
    if sidecar:
        print(sidecar)


def main():
    parser = argparse.ArgumentParser(description="按已校验的剪辑计划渲染婚礼旅拍成片")
    parser.add_argument("plan", type=Path)
    parser.add_argument("edition", choices=("landscape", "portrait"))
    parser.add_argument("output", type=Path, metavar="OUTPUT.mp4")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="hd")
    parser.add_argument("--scratch", type=Path, help="可再生中间文件的父目录，默认采用成片所在目录")
    args = parser.parse_args()
    try:
        render(args.plan, args.edition, args.output, args.profile, args.scratch)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"渲染失败：{exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "渲染已取消。\n")


if __name__ == "__main__":
    main()
