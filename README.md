# 婚礼和旅拍视频剪辑 · Wedding & Travel Edit

给婚礼工作室的两套后期流程。婚礼快剪从誓词和听者反应选片，旅拍短片从人物动作、路线和环境声选片。两版各自决定镜头、字幕和声音时间线，可以交付 16:9 横版、9:16 竖版或其中一版。

| | 婚礼快剪 | 旅拍短片 |
| --- | --- | --- |
| 选片 | 以仪式实况为序，给戒指、回答、亲属反应找到同一时刻的可用机位 | 按地点、行进方向、动作起落和光向选片，让人物带观众走完一段路 |
| 声音 | 对齐领夹与机内声，保存原句和停顿，给问答、掌声各留位置 | 用环境底声连机位，用脚步或门铃落动作，在音乐乐句中给现场声留位 |
| 横竖版 | 横版保留人物关系，竖版为说话人、听者和手部动作另选角度 | 横版交代地理空间，竖版可另选起镜、缩短双人并肩的区间或保留全幅 |

编辑方法可直接在剪映、达芬奇等软件中使用。本地脚本可盘点素材、测响度、导出源时码与成片帧对照表、生成多帧构图预览、制作中文片名卡，以及将确定的剪辑计划渲染为 H.264/AAC 视频。HD 和 UHD 均有横竖两个尺寸。多机同步、逐镜校色、语音转写和镜头取舍由剪辑师或宿主工具完成。

## 安装

从 [v1.0.0 发布页](https://github.com/HaloBear-AI/skill-wedding-travel-edit/releases/tag/v1.0.0) 下载 Skill ZIP 并解压，或从本仓库复制 [wedding-travel-edit](wedding-travel-edit) 整个文件夹。把同名文件夹放入支持 Agent Skills 的宿主目录，并保留其内部结构。例如 Codex 的用户技能目录为 `~/.codex/skills/`。只读剪辑方法时可以直接打开 [技能入口](wedding-travel-edit/SKILL.md)。

运行本地脚本需 Python 3.9+，PATH 中可找到 `ffmpeg` 和 `ffprobe`。使用包含 libx264、AAC、loudnorm、lut3d、drawtext 的常规构建；生成中文片名卡时提供自己的中文字体。无需 Python 第三方包、云端账号或 API 密钥。素材、工作目录、成片位置均由使用者指定。

## 最小使用示例

> 用 $wedding-travel-edit 剪一条 60 秒的婚礼横版。保留新人互相回答时的停顿。另剪一条 30 秒竖版旅拍预告，从两人下船的脚步声开始。先给我两份选片单和构图预览。

要试跑出片，在本仓库根目录执行下面的命令。生成器会在新的 `demo` 目录创建合成测试画面与声音，并放入两份可编辑的 JSON 计划。

```sh
cd wedding-travel-edit
python3 scripts/make_demo.py demo
python3 scripts/review.py inventory demo/media demo/素材清单.csv
python3 scripts/render.py demo/wedding.json landscape demo/婚礼横版.mp4 --profile draft
python3 scripts/render.py demo/travel.json portrait demo/旅拍竖版.mp4 --profile draft
```

婚礼示例同时输出该版独立 SRT。实际使用时，把计划中的素材路径、入出点、构图焦点和声音区间改为你的素材。脚本会拒绝覆盖已有交付文件。

- [婚礼快剪的选片、声音与双版分镜](wedding-travel-edit/references/wedding.md)
- [旅拍短片的选片、声音与双版分镜](wedding-travel-edit/references/travel.md)
- [计划字段、两种画幅的命令与输出规格](wedding-travel-edit/references/plan.md)
- [声音修复、调色、字幕与手机复查](wedding-travel-edit/references/finishing.md)
- [Walter Murch 与 Thelma Schoonmaker 的剪辑方法及两项工作室练习](wedding-travel-edit/references/editors.md)

## 方法来源

本 Skill 的工作方法借鉴 Miller 资料库《AI 短剧后期剪辑技能包》中的「短剧剪辑师」（duanju-houqi）。沿用了逐段体检、源时码与成片时码对照、对白优先、逐镜检查裁切、先统调后定风格、片名与字幕单独制作，以及导出复听复看的做法。两套实拍指南、脚本、计划、片名卡和合成测试素材生成器由本项目重新编写。原包中的 LUT、模板、示例图和原始文件由原作者分发。[Miller 原始资料库](https://miliagent.com/library/)

本仓库的原创代码、文档和示例按 [MIT 许可证](LICENSE) 分发。实片所用字体、音乐、LUT 和客片素材由使用者提供。
