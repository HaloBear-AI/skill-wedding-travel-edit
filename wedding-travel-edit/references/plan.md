# 剪辑计划和本地命令

需要 Python 3.9+、可在终端运行的 FFmpeg 与 ffprobe。下列命令从含 `SKILL.md` 的目录执行。例中的 `demo` 是新的工作目录，成果可以放在独立的交付盘，`--scratch` 可为渲染、片名卡和预览图指定临时文件的位置；默认使用输出文件所在目录。给每次输出起一个新文件名。

```sh
python3 scripts/make_demo.py demo
python3 scripts/review.py inventory demo/media demo/ingest.csv
python3 scripts/edit_plan.py demo/wedding.json landscape --timeline demo/wedding-landscape.csv
python3 scripts/review.py sheet demo/wedding.json portrait demo/wedding-portrait.jpg --scratch demo
python3 scripts/render.py demo/wedding.json landscape demo/wedding-wide.mp4 --profile hd --scratch demo
python3 scripts/render.py demo/wedding.json portrait demo/wedding-tall.mp4 --profile hd --scratch demo
python3 scripts/render.py demo/travel.json landscape demo/travel-wide.mp4 --profile draft --scratch demo
python3 scripts/render.py demo/travel.json portrait demo/travel-tall.mp4 --profile draft --scratch demo
```

示例里的几何图形、测试信号和 SRT 只用来验证时间与构图。换用实拍时用实际素材重写入出点和声音区间。素材盘点 CSV 中 `integrated_lufs` 是整段综合响度，`true_peak_dbtp` 是真峰值，空的选片列留给看过素材的人填写。时间线 CSV 中 `record_out_frame_exclusive` 是下一镜第一帧的位置。构图预览每镜显示约 10%、50%、90% 三处，超过八镜自动分页。

## 最小计划

```json
{
  "version": 1,
  "mode": "travel",
  "fps": 25,
  "shots": {
    "arrival": {"file": "media/wide.mp4", "in": 0.2, "out": 2.2}
  },
  "editions": {
    "landscape": {"cuts": [{"shot": "arrival"}]},
    "portrait": {"cuts": [{"shot": "arrival", "focus": [0.72, 0.5]}]}
  }
}
```

所有路径相对于计划文件，也可以用绝对路径。时间用秒，可以带小数。每个版本的成片时间从零开始，独立计算。源入点含该时刻，出点到该时刻为止；每个片段四舍五入到最近的一个输出帧再拼接。

| 字段 | 含义 |
| --- | --- |
| `version`、`mode`、`fps` | 版本号 1，模式 `wedding` 或 `travel`。输出时间线默认为 25 帧，支持 1–120 整数帧率。源素材可有不同帧率。按现场和交付约定设置时间线。 |
| `shots` | 用稳定的镜头编号对应源文件、可用区间 `in/out`。省略入出点会取整条。要对同一源文件用不同区间，把它记为两个编号。 |
| `editions`、`cuts` | `landscape` 与 `portrait` 各自一个对象，按该版的顺序填 `cuts`。可以只填一个版本。每项用 `shot` 选源镜，可覆盖下面的构图、增益和淡黑值。 |
| `focus`、`fit` | 焦点 `[x,y]` 在已摆正画面的 0–1 坐标内，默认居中。`cover` 围绕焦点等比裁满，裁框遇边会停；`contain` 保全幅并补黑。焦点在一个选段内固定。 |
| `sound_db` | 该镜机内原声增益，默认 0，`null` 为静音。有领夹声覆盖同一句话时可将机内声静音，避免双声。 |
| `fade_in`、`fade_out` | 当前镜头由黑渐入或向黑渐出的秒数，默认 0，只作用于画面。二者合计须能放进该镜。 |
| `lut`、`target_lufs` | 顶层可选。自备的 .cube 风格 LUT 作用于全片；需分别做输入转换的相机素材先处理。混音目标默认 -16 LUFS，导出后以实测为准。 |
| `note` | 可在计划、镜头、版本和剪辑项中记下选片理由、备选机位或音效设计。 |

## 连续声音与字幕

每版可设一条 `voice` 和一条 `music`。前者可承载同步完成的领夹、旁白或预混的环境声。两者都接受 `file`、`in`（声音文件起点）、`at`（本版何时响起）、`duration` 和 `gain_db`。时间默认从零开始，省略时长会取源素材与成片剩余长度中的较短者。原声默认各镜独立，`voice` 可以连续跨过剪点。音乐默认增益 -14 dB，主声轨默认 0 dB。

```json
{
  "voice": {"file": "audio/vows-mix.wav", "in": 1.2, "at": 2.0, "duration": 4.5},
  "music": {"file": "audio/music.wav", "gain_db": -12},
  "duck": [[1.8, 6.8], [9.0, 10.2]],
  "duck_db": -9,
  "srt": "subtitles/portrait.srt"
}
```

上段字段填在相应版本内。音乐只在 `duck` 标出的成片时间附近平滑降低并恢复，重叠区间会合并。可为一段誓词或需要听见的脚步留位。SRT 使用 UTF-8 和该版本的最终时间轴，脚本检查时间范围，并将它复制到与 MP4 同名的字幕文件。源片上的旧字幕或横版字幕需要先重新对时。

## 画幅与片名卡

| 规格 | 横版 | 竖版 |
| --- | --- | --- |
| `draft` | 640×360 | 360×640 |
| `hd`（默认） | 1920×1080 | 1080×1920 |
| `uhd` | 3840×2160 | 2160×3840 |

渲染器输出 SDR、H.264 画面和 AAC 立体声。横竖版分别生成和复查。制作片名卡时替换命令中的字体路径，选择这次作品可以使用的中文字体。

```sh
python3 scripts/title_card.py portrait demo/opening.mp4 --title "海岸来信" --subtitle "夏日旅拍" --font "/path/to/font.ttf" --seconds 2 --fps 25 --scratch demo
```

将片名卡登记进 `shots`，放在所需版本的 `cuts` 首位。若两个版本都用片头，分别渲染各自画幅。导出后把成片放进单独的目录，再用 `review.py inventory` 复测，避免把成片混入原来盘点的素材。
