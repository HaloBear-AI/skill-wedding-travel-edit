"""Create local synthetic footage for both included plans; no stock media or downloads."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def generate(folder):
    media = folder / "media"
    media.mkdir()

    def ffmpeg(*args):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-n", *args], check=True)

    def video(name, source, filters=None, tone=None):
        args = ["-f", "lavfi", "-i", source]
        if tone:
            args.extend(["-f", "lavfi", "-i", f"sine=f={tone}:r=48000:d=4", "-c:a", "aac"])
        if filters:
            args.extend(["-vf", filters])
        ffmpeg(*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "4", str(media / name))

    video("wide.mp4", "color=c=0x243B4D:s=640x360:r=25:d=4",
          "drawbox=x=70:y=78:w=90:h=214:color=0xD4A36E:t=fill,"
          "drawbox=x=455:y=63:w=94:h=229:color=0x82B7A4:t=fill", tone=330)
    video("action.mp4", "testsrc2=s=640x360:r=30:d=4", tone=370)
    video("detail.mp4", "color=c=0x774C52:s=360x640:r=25:d=4",
          "drawbox=x=77:y=220:w=205:h=175:color=0xE7C398:t=12")
    for name, hz in (("voice", 660), ("music", 196)):
        ffmpeg("-f", "lavfi", "-i", f"sine=f={hz}:r=48000:d=9", "-c:a", "pcm_s16le",
               str(media / f"{name}.wav"))
    examples = Path(__file__).resolve().parents[1] / "assets" / "examples"
    for name in ("wedding.json", "travel.json", "wedding-landscape.srt", "wedding-portrait.srt"):
        shutil.copyfile(examples / name, folder / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="在一个新目录中生成测试图、测试音及两套可直接运行的方案。")
    parser.add_argument("output_directory", type=Path)
    dest = parser.parse_args().output_directory.resolve()
    if dest.exists():
        parser.exit(1, f"请选择一个新目录，目标已经存在：{dest}\n")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="demo-", dir=dest.parent) as temp:
            staged = Path(temp) / "example"
            staged.mkdir()
            generate(staged)
            # An exclusive mkdir also protects a destination created while FFmpeg was running.
            dest.mkdir()
            moved = []
            try:
                for entry in sorted(staged.iterdir()):
                    if entry.name.startswith("."):
                        continue
                    entry.rename(dest / entry.name)
                    moved.append(entry.name)
            except BaseException:
                for name in reversed(moved):
                    (dest / name).rename(staged / name)
                if not any(dest.iterdir()):
                    dest.rmdir()
                raise
        print(dest)
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")
