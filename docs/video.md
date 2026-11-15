# 60 秒短视频合成（T07）

本文说明 `workbench/video/`、`templates/video/` 与 `wb video` 的用法。适配器与数据模型契约以
[`docs/adr/0001-mvp-architecture.md`](adr/0001-mvp-architecture.md) §5、§6.4 为准。

## 1. 能力与边界

- 输入一份分镜 JSON，输出 9:16 竖版 MP4：1080×1920、H.264（`yuv420p`）、25fps、CRF 23。
- 总时长上限 60 秒；超出时按比例压缩到 60 秒，并在输出与日志里 warning，不会报错。
- 三种卡片：`title`（大标题）、`points`（要点卡，逐条出现）、`cta`（结尾引导）。
- 可选配音：`edge-tts`（需要网络）。无网络或调用失败时自动降级为静音 + 字幕条，退出码仍为 0。
- 产物写一行 `asset`（`kind='video'`），带实际时长、尺寸、字节数；meta 为
  `{"scenes": N, "fps": 25, "tts": <实际是否带音轨>}`。
- **不做**数字人、自动剪辑、转场特效、BGM、素材库；也不登录平台或发布视频。发布交给人。

## 2. 命令行

```sh
# 打印内置分镜模板（stdout 是纯 JSON，可直接重定向）
wb video template > /tmp/sb.json

# 合成竖版 MP4；--tts 强制启用配音（失败自动降级）
wb video build --storyboard /tmp/sb.json --out /tmp/v.mp4
wb video build --storyboard /tmp/sb.json --out /tmp/v.mp4 --tts

# 走 video 流水线的 stage：产物写入 <out>/assets/ 并落 asset 表
wb run video --input storyboard=/tmp/sb.json --out /tmp/wb-video
wb run video --input storyboard=/tmp/sb.json --input tts=true --out /tmp/wb-video
```

- `wb video build` 只产文件，不写数据库（与 `wb render` 的分工一致）；需要 `asset` 行时用
  `wb run video`（可选 `--input copy_set_id=<id>` 关联文案，`--input template=<名字>` 记录模板名）。
- `wb video build` 的 `--tts` 会覆盖分镜里的 `tts` 字段。
- 输出文件可以写在 `WB_HOME` 之外；stage 写库时路径相对 `WB_HOME`，在 `WB_HOME` 之外则存绝对路径。

## 3. 分镜 JSON

```json
{
  "title": "一个人也能稳定产出内容",
  "tts": false,
  "scenes": [
    {"type": "title", "text": "把重复动作交给流水线", "seconds": 3},
    {
      "type": "points",
      "text": "一条内容流水线的四个动作",
      "points": ["选题先看证据", "文案先出骨架", "图文与视频共用素材", "发布包一次导出"],
      "seconds": 6
    },
    {"type": "cta", "text": "先跑通一条流水线，再谈放大", "seconds": 3}
  ]
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `title` | string | 必填、非空；也用作页脚文案 |
| `tts` | bool | 可选，默认 `false`；`true` 时尝试配音 |
| `scenes` | array | 非空；按数组顺序播放 |
| `scenes[].type` | string | `title` / `points` / `cta` |
| `scenes[].text` | string | 必填、非空；空串或纯空白抛 `ValidationError` |
| `scenes[].points` | string[] | 仅 `points` 卡使用；最多 5 条，且至少 1 条 |
| `scenes[].seconds` | number | 必填、大于 0；合计超过 60 秒时按比例压缩 |

校验失败会以 `ValidationError` 报出并让 CLI 退出码为 1；超长不是失败，只 warning。压缩后的总时长
精确为 60 秒，warning 同时给出原始时长与压缩后时长。

## 4. 模板怎么改

内置模板在 `templates/video/default.json`，`wb video template` 直接读取它。修改方式：

1. 直接编辑该文件；或用 `WB_TEMPLATE_DIR` 指向另一个包含 `video/default.json` 的目录。
2. 保持字段名与卡片类型不变；`points` 最多 5 条，单条太长会被截断并加省略号。
3. 想要更长的演示就增加 `seconds`，但合计超过 60 秒会被压缩，压缩比例会打印在 warning 里。
4. 改完先跑 `wb video template | python3 -m json.tool` 确认是合法 JSON，再执行 `wb video build`。

模板只描述「内容 + 时长 + 卡片类型」；卡片版式（字号、换行、进度条、配色）固定在
`workbench/video/frame.py` 里，避免分镜作者改出越界版式。

## 5. 卡片渲染与成片

- `title` / `cta`：各渲染 1 张 1080×1920 PNG。
- `points`：按 2-3 个进度帧渲染（先 1-2 条、再多几条、最后全部），按权重切分该场景的
  `seconds`，形成「逐条出现」的静态动画。
- 每段 PNG 用 ffmpeg `-loop 1 -t <seconds>` 作为独立输入，再用 `concat` filter 拼接，统一
  `scale=1080:1920,format=yuv420p` 后以 `libx264 -crf 23 -preset veryfast` 编码。
- ffmpeg 只用 `imageio_ffmpeg.get_ffmpeg_exe()` 返回的二进制，不要求系统安装 ffmpeg 或
  ffprobe。成片时长以 `ffmpeg -i` 解析出的实际值为准写回 `asset.duration_sec`。
- 为什么不用 concat demuxer：本机 ffmpeg 9 对 `duration` 指令会在末帧多播约 1 秒，导致
  12 秒分镜输出 13.48 秒；改用 `-loop 1 -t` + concat filter 后时长与分镜一致。

## 6. 配音与降级

- 启用 `tts=true` 时，把各卡标题与要点拼成配音稿，调用
  `edge-tts --voice zh-CN-XiaoxiaoNeural --text ... --write-media narration.mp3`。
- 配音需要网络。以下情况都只记 warning 并继续合成静音视频，退出码 0：
  - 找不到 `edge-tts` 可执行文件（`FileNotFoundError`）；
  - 网络异常、非零退出码、没有生成音频文件；
  - 音频合入 ffmpeg 失败（重试一次纯视频合成）。
- 成功配音时用 `-af apad` 补长短音频、`-t <分镜总时长>` 截断长音频，**视频时长始终以分镜为准**，
  不会因为配音变长或变短。
- warning 会写入 CLI 的 stderr、stage 的 `ctx.log`，以及 composer 返回的 `warnings` 列表。

## 7. 字体来源

- 只读 `WB_FONT_DIR`（默认仓库 `assets/fonts/`）里的 Noto Sans SC：
  `NotoSansSC-Regular.otf`、`NotoSansSC-Bold.otf`。
- 字体随仓库提供，许可见 `assets/fonts/LICENSE-OFL.txt`（SIL Open Font License）；不依赖
  `fc-list`、系统字体或网络。
- 换字体时保持两个文件名不变，或用 `WB_FONT_DIR` 指向新目录；缺文件会报可读错误，不会画出
  方框乱码。
- 为什么不用 TTF：内置的是 OTF（CFF）版本，Pillow/FreeType 可直接读取；无需转换。

## 8. 为什么不做 AI 剪辑

- 本 Ticket 的目标是「可重复、可离线验证的卡片分镜」，不是智能剪辑。自动挑素材、配乐、
  转场会引入模型与素材版权问题，也无法在 `WB_LLM=stub` 下稳定测试。
- 卡片视频的审核成本低：人只需看 60 秒成片与发布包，改文案或改模板即可重跑，不需要重新
  训练或调参。
- 后续若要扩展，优先在 `templates/video/` 与 `frame.py` 增加版式，而不是引入剪辑库。

## 9. 验收命令

```sh
# 1. 模板（≥3 个场景）
wb video template > /tmp/sb.json
python3 -c "import json; d=json.load(open('/tmp/sb.json')); print(len(d['scenes']))"

# 2. / 3. 成片与 ffmpeg 元数据
wb video build --storyboard /tmp/sb.json --out /tmp/v.mp4
ffmpeg -hide_banner -i /tmp/v.mp4 2>&1 | grep -E "Duration|Video:"

# 4. 超长分镜压缩到 60 秒（合成 90 秒分镜后确认 warning 与实际时长）
# 5. 无网络配音降级（PATH 里去掉 edge-tts 或在测试里 monkeypatch）
wb video build --storyboard /tmp/sb.json --out /tmp/v-tts.mp4 --tts

# 6. asset 入库
WB_HOME=/tmp/wb-video wb run video --input storyboard=/tmp/sb.json --out /tmp/wb-video/run
python3 -c "import sqlite3; c=sqlite3.connect('/tmp/wb-video/wb.sqlite3'); print(c.execute('select kind,duration_sec,width,height from asset').fetchall())"

# 7. 测试
pytest -q tests/test_video.py
```

无人值守测试不会联网：配音失败路径通过替换 `workbench.video.tts.subprocess_run` 模拟；ffmpeg
使用随依赖提供的本地二进制。
