"""Resolve one editorial timeline for both the contact sheet and the renderer."""

import argparse
import csv
import json
import math
import re
from pathlib import Path

from media_info import probe


EDITIONS = ("landscape", "portrait")
PROFILES = {"draft": (640, 360), "hd": (1920, 1080), "uhd": (3840, 2160)}


def frame_size(edition, profile):
    w, h = PROFILES[profile]
    return (w, h) if edition == "landscape" else (h, w)


def crop_rect(w, h, target_w, target_h, focus):
    """Center the crop on a point in display coordinates and keep it inside the frame."""
    cw = max(2, 2 * round(min(w, h * target_w / target_h) / 2))
    ch = max(2, 2 * round(min(h, w * target_h / target_w) / 2))
    cw, ch = min(cw, w - w % 2), min(ch, h - h % 2)
    x = min(max(0, 2 * round((focus[0] * w - cw / 2) / 2)), (w - cw) // 2 * 2)
    y = min(max(0, 2 * round((focus[1] * h - ch / 2) / 2)), (h - ch) // 2 * 2)
    return x, y, cw, ch


def obj(value, name, keys):
    if not isinstance(value, dict):
        raise ValueError(f"{name} 应为 JSON 对象")
    extra = value.keys() - set(keys.split()) - {"note"}
    if extra:
        raise ValueError(f"{name} 有未知字段 {', '.join(sorted(extra))}")
    return value


def number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 需要一个有限数值")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} 的值超出范围 ({value})")
    return float(value)


def local_file(base, value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少 {field} 文件路径")
    path = (base / value).resolve()
    if not path.is_file():
        raise ValueError(f"{field} 文件不存在：{path}")
    return str(path)


def clip(base, shots, edit, fps):
    obj(edit, "cuts[]", "shot focus fit sound_db fade_in fade_out")
    name = edit.get("shot")
    if not isinstance(name, str) or name not in shots:
        raise ValueError(f"找不到镜头 {name!r}")
    shot = obj(shots[name], name, "file in out fit focus sound_db fade_in fade_out")
    src = local_file(base, shot.get("file"), name)
    info = probe(src)
    if min(info["width"], info["height"]) < 2:
        raise ValueError(f"{name} 需要画面，请把独立录音放在 voice 或 music")
    source_end = info.get("video_duration") or info["duration"]
    start = number(shot.get("in", 0), f"{name}.in", 0)
    end = number(shot.get("out", source_end), f"{name}.out", 0)
    if end > source_end + 0.001 or start >= end:
        raise ValueError(f"{name} 的入出点 {start:g}–{end:g}s 超出有效区间 0–{source_end:g}s")
    frames = math.floor((end - start) * fps + 0.5 + 1e-9)
    if frames < 1:
        raise ValueError(f"{name} 的选段不足一个输出帧")
    settings = {**shot, **edit}
    focus = settings.get("focus", [0.5, 0.5])
    if not isinstance(focus, list) or len(focus) != 2:
        raise ValueError(f"{name}.focus 应为 [x, y]")
    focus = [number(p, f"{name}.focus", 0, 1) for p in focus]
    fit = settings.get("fit", "cover")
    if fit not in ("contain", "cover"):
        raise ValueError(f"{name}.fit 应为 contain 或 cover")
    gain = settings.get("sound_db", 0)
    if gain is not None:
        gain = number(gain, f"{name}.sound_db")
    fades = {key: number(settings.get(key, 0), f"{name}.{key}", 0)
             for key in ("fade_in", "fade_out")}
    if sum(fades.values()) > frames / fps + 1e-9:
        raise ValueError(f"{name} 的淡入、淡出合计超过本镜头时长")
    return {**info, "id": name, "file": src, "in": start, "out": end, "frames": frames,
            "focus": focus, "fit": fit, "sound_db": gain, **fades}


def audio_track(base, value, name, length):
    if value is None:
        return None
    obj(value, name, "file in at duration gain_db")
    src = local_file(base, value.get("file"), name)
    info = probe(src)
    if not info["has_audio"]:
        raise ValueError(f"{name} 文件没有音轨：{src}")
    start = number(value.get("in", 0), name + ".in", 0)
    at = number(value.get("at", 0), name + ".at", 0)
    available = min((info.get("audio_duration") or info["duration"]) - start, length - at)
    duration = number(value.get("duration", available), name + ".duration", 0)
    if duration <= 0 or duration > available + 0.001:
        raise ValueError(f"{name} 选段超出源音频或当前版本时长，可用 {max(0, available):g}s")
    gain = number(value.get("gain_db", -14 if name == "music" else 0), name + ".gain_db")
    return {**info, "file": src, "in": start, "at": at, "duration": duration, "gain_db": gain}


def cue_windows(value, length):
    if not isinstance(value, list):
        raise ValueError("duck 需要一组 [开始秒, 结束秒]")
    result = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("duck 每项应为 [开始秒, 结束秒]")
        a, b = [number(t, "duck", 0, length) for t in pair]
        if a >= b:
            raise ValueError("duck 的结束时间应晚于开始时间")
        result.append((a, b))
    # Merge overlapping or adjacent protected lines; music should stay down between them.
    merged = []
    for a, b in sorted(result):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def check_srt(path, length):
    stamp = r"(\d+):([0-5]\d):([0-5]\d),(\d{3})"
    def seconds(parts):
        h, m, s, ms = map(int, parts)
        return h * 3600 + m * 60 + s + ms / 1000

    content = Path(path).read_text(encoding="utf-8-sig").strip()
    for block in re.split(r"\n[ \t]*\n", content):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        timing = re.fullmatch(stamp + r"\s*-->\s*" + stamp + r"(?:\s+.*)?", lines[0].strip()) if lines else None
        if not timing or len(lines) < 2:
            raise ValueError(f"字幕缺少有效时间码或正文：{path}")
        a, b = seconds(timing.groups()[:4]), seconds(timing.groups()[4:])
        if a >= b or b > length + 0.001:
            raise ValueError(f"字幕时间超出本版正片 {length:g}s：{lines[0]}")


def read_edition(plan_path, edition):
    path = Path(plan_path).resolve()
    data = obj(json.loads(path.read_text(encoding="utf-8-sig")), "计划",
               "version mode fps shots editions lut target_lufs")
    if data.get("version") != 1 or isinstance(data["version"], bool):
        raise ValueError("version 应为 1")
    if data.get("mode") not in ("wedding", "travel"):
        raise ValueError("mode 应为 wedding 或 travel")
    fps = data.get("fps", 25)
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError("fps 需要 1–120 的整数时间线帧率")
    shots = data.get("shots")
    versions = obj(data.get("editions"), "editions", "landscape portrait")
    if not isinstance(shots, dict) or not shots or edition not in EDITIONS or edition not in versions:
        raise ValueError(f"请提供 shots 以及 editions.{edition}")
    version = obj(versions[edition], edition, "cuts music voice duck duck_db srt")
    edits = version.get("cuts")
    if not isinstance(edits, list) or not edits:
        raise ValueError(f"{edition}.cuts 至少需要一个镜头")
    cuts = [clip(path.parent, shots, item, fps) for item in edits]
    length = sum(c["frames"] for c in cuts) / fps
    tracks = {k: audio_track(path.parent, version.get(k), k, length) for k in ("voice", "music")}
    windows = cue_windows(version.get("duck", []), length)
    if windows and tracks["music"] is None:
        raise ValueError("填写 duck 需要本版的 music 音轨")
    srt = local_file(path.parent, version["srt"], "srt") if version.get("srt") is not None else None
    if srt:
        check_srt(srt, length)
    lut = local_file(path.parent, data["lut"], "lut") if data.get("lut") is not None else None
    if lut and Path(lut).suffix.lower() != ".cube":
        raise ValueError("lut 需要 .cube 文件")
    return {**tracks, "mode": data["mode"], "fps": fps, "cuts": cuts, "seconds": length,
            "duck": windows, "duck_db": number(version.get("duck_db", -9), "duck_db", maximum=0),
            "srt": srt, "lut": lut, "target_lufs": number(data.get("target_lufs", -16), "target_lufs", -70, -5)}


def export_timeline(cfg, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = output.open("x", encoding="utf-8-sig", newline="")
    try:
        with handle:
            writer = csv.writer(handle)
            writer.writerow(["order", "shot", "source", "source_in_s", "source_out_s",
                             "record_in_frame", "record_out_frame_exclusive", "record_in_s",
                             "record_out_s", "fit", "focus_x", "focus_y", "sound_db"])
            cursor = 0
            for n, cut in enumerate(cfg["cuts"], 1):
                end = cursor + cut["frames"]
                writer.writerow([n, cut["id"], cut["file"], cut["in"], cut["out"], cursor, end,
                                 f"{cursor / cfg['fps']:.6f}", f"{end / cfg['fps']:.6f}",
                                 cut["fit"], *cut["focus"], cut["sound_db"]])
                cursor = end
    except BaseException:
        output.unlink()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="校验当前版本，按需要导出源时间与成片时间对照 CSV。")
    parser.add_argument("plan")
    parser.add_argument("edition", choices=EDITIONS)
    parser.add_argument("--timeline", metavar="FILE.csv")
    args = parser.parse_args()
    try:
        config = read_edition(args.plan, args.edition)
        if args.timeline:
            export_timeline(config, args.timeline)
        print(f"{config['mode']} / {args.edition} · {len(config['cuts'])} 镜 · "
              f"{config['seconds']:.3f} 秒 · {config['fps']} fps")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
