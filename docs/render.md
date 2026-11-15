# 图文与海报渲染（T04）

本文说明 `workbench/render/`、`templates/` 与 `wb render` 的用法。数据模型与适配器契约以
[`docs/adr/0001-mvp-architecture.md`](adr/0001-mvp-architecture.md) §3、§6.4 为准。

## 1. 能力与边界

- 5 套海报模板 + 3 套图文模板，覆盖 3:4（1080×1440）、1:1（1080×1080）、9:16（1080×1920）。
- 版式由 JSON 模板声明；文字按 `max_width` 自动换行，超过 `max_lines` 截断并追加 `…`，同时记
  warning。
- 渲染前做「所有块 bbox ⊂ 画布 − 安全边距」检查，越界直接抛 `LayoutError`；文本类块之间重叠
  （叠字）同样抛 `LayoutError`。这是模板作者的错误，不是数据的错误。
- 同一份数据 + 同一套模板，两次渲染的尺寸、块位置与文本内容一致（PNG 字节可能因 Pillow 版本
  略有差异，测试断言结构而不是像素哈希）。
- **不做**浏览器 / HTML / Playwright 渲染；`image` 块只支持本地文件或纯色占位；不做动图、不做
  视频（T07）。渲染过程不访问网络、不依赖系统字体。

## 2. 命令行

```sh
# 列出 5 套 poster + 3 套 carousel 的 id / 类型 / 尺寸 / 页数
wb render list

# 单页海报：--out 是文件路径
wb render poster --template poster-hero \
  --data tests/fixtures/poster_data.json --out /tmp/r/poster-hero.png

# 九图/多页图文：--out 是目录，产出 01.png ... 0N.png + manifest.json
wb render carousel --template xhs-nine \
  --data tests/fixtures/carousel_data.json --out /tmp/r/nine

# 走 visual 流水线的 stage（产物写入 <out>/assets/ 并落 asset 表）
wb run visual --input template=poster-hero \
  --input data=tests/fixtures/poster_data.json --out /tmp/wb-visual
wb run visual --input template=xhs-nine \
  --input data=tests/fixtures/carousel_data.json --out /tmp/wb-visual
```

- `wb render poster` 只接受 poster 模板，`wb render carousel` 只接受 carousel 模板；用错类型会
  提示应使用的子命令。
- 字体目录：`WB_FONT_DIR`，默认仓库内 `assets/fonts/`。找不到
  `NotoSansSC-Regular.otf` / `NotoSansSC-Bold.otf` 时抛 `Unavailable`（`MissingFont`），提示把
  CJK 字体放到 `assets/fonts/`。
- 模板目录：`WB_TEMPLATE_DIR`，默认仓库内 `templates/`。

## 3. 渲染输入（数据 JSON）

```json
{
  "slots": {
    "title": "一周复盘：把内容流水线跑通",
    "subtitle": "从选题到发布，一个人也能稳定产出",
    "body": "正文可以包含换行。",
    "points": ["第一条", "第二条", "第三条"],
    "tags": ["内容运营", "效率工具"],
    "footer": "虚构示例数据 · 仅用于渲染测试"
  },
  "meta": {"source": "手动整理", "note": "可选，原样透传"},
  "pages": [
    {"title": "第 1 页标题", "body": "第 1 页正文"},
    {"title": "第 2 页标题", "body": "第 2 页正文"}
  ]
}
```

- `slots` 是文案槽位；模板引用的 slot 缺失或为空时用 `templates/tokens.yaml` 的
  `placeholder`（默认 `「待补充」`）。`list` / `chips` 块取到列表时按元素渲染；取到字符串时按
  换行、逗号、顿号、分号切分。
- `pages` 只对 carousel 生效：第 N 页的槽位 = 顶层 `slots` 与 `pages[N-1]` 的浅合并
  （`pages[N-1]` 可以是槽位对象，也可以写成 `{"slots": {...}}`）。页数少于模板页数时，剩余页
  沿用顶层槽位；多出的页忽略。
- `meta` 不参与绘制，仅由调用方使用。

## 4. 模板格式

模板放在 `templates/poster/<id>.json` 或 `templates/carousel/<id>.json`，`id` 必须与文件名
一致：

```json
{
  "id": "poster-hero",
  "label": "深色主视觉海报",
  "canvas": {"width": 1080, "height": 1440, "background": "#101820", "pad": 72},
  "blocks": [
    {"type": "text", "slot": "title", "x": 72, "y": 216, "size": 96, "weight": "bold",
     "color": "#FFFFFF", "max_width": 936, "max_lines": 3, "line_height": 128, "align": "left"}
  ]
}
```

carousel 用 `pages` 代替顶层 `blocks`，每页一个对象：

```json
{
  "id": "xhs-nine",
  "label": "小红书九图",
  "canvas": {"width": 1080, "height": 1440, "background": "#FFF7F0", "pad": 72},
  "pages": [
    {"name": "封面", "blocks": ["..."]},
    {"name": "内容 1", "blocks": ["..."]}
  ]
}
```

### 4.1 画布与坐标

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `canvas.width` / `canvas.height` | int | 输出像素尺寸，必须 > 0 |
| `canvas.background` | 颜色 | 背景纯色 |
| `canvas.pad` | int | 安全边距；同时决定安全区 `[pad, W-pad] × [pad, H-pad]` |

**块坐标 `x` / `y` 是相对画布左上角的像素坐标，并且包含 `pad` 偏移**：`pad=72` 时内容从
`x=72`、`y=72` 开始写，而不是从 0 开始。渲染器不再额外加偏移；越界检查就是检查
`bbox ⊂ [pad, W-pad] × [pad, H-pad]`。

每个块都必须显式给出 `x`、`y`。`bbox` 按模板声明的最大范围预留（`text` 为
`max_lines × line_height`，`list` / `chips` 按最坏情况预留行数），因此**检查结果与数据长短
无关**，数据再长也不会把版式挤出安全区；放不下的文字会被截断并 warning。

### 4.2 六种块类型

| 类型 | 必填 | 可选（括号内为默认值） | 行为 |
| --- | --- | --- | --- |
| `text` | `slot` | `x/y`(0)、`size`(32)、`weight`(regular)、`color`(tokens ink)、`max_width`(画布宽 − 2×pad)、`max_lines`(8)、`line_height`(size×1.5)、`align`(left) | 自动换行；超过 `max_lines` 截断并追加 `…` |
| `list` | `slot` | 同上，另 `bullet`(`·`)、`item_gap`(16) | 每项前画项目符号，整块行数受 `max_lines` 限制 |
| `panel` | `x/y/width/height` | `fill`(tokens panel)、`radius`(32)、`stroke`、`stroke_width`；可选 `slot` 在面板内写字，另 `size`、`color`、`padding`(32)、`max_lines`、`line_height`、`align` | 圆角矩形；带 `slot` 时文字超出面板可用行数会截断并 warning |
| `chips` | `slot` | `x/y`、`size`(24)、`weight`、`color`(on_brand)、`background`(brand)、`max_width`、`max_rows`(2)、`gap`(16)、`padding`(10)、`radius`(999)、`align` | 话题标签药丸，按行自动折行；超过 `max_rows` 截断并 warning |
| `divider` | `x/y/width` | `thickness`(2)、`color`(tokens line) | 分隔线 |
| `image` | `x/y/width/height` | `slot`（值为本地图片路径，相对当前工作目录）、`path`（模板固定路径）、`fill`、`stroke`、`radius`(tokens image)、`label`、`label_slot`、`label_size`(24)、`label_weight`、`label_color` | 有可读本地文件时等比裁切填充；缺失时画占位框 + 文案，不报错 |

说明：

- `size` 是字号（像素），`line_height` 是行高（像素），`max_width` 是文本可用宽度。
- `align` 取 `left` / `center` / `right`，相对 `max_width` 对齐。
- 颜色支持 Pillow `ImageColor` 能解析的写法（`#RRGGBB`、`#RGB`、颜色名）；alpha 通道忽略。
- 字重只有 `regular` / `bold` 两种，对应内置的两个字体文件。
- `weight`、`align`、颜色、宽高等字段写错会抛 `TemplateError`；版式越界/叠字抛 `LayoutError`；
  两者都是模板作者的错，不是数据的错。

### 4.3 `tokens.yaml`

`templates/tokens.yaml` 放品牌色、字号档位、间距、圆角与占位文案，模板里没写的块字段回退到
这里。文件内容使用 **JSON 语法**（JSON 是 YAML 1.2 的子集，仍然是一个合法的 YAML 文件），
这样渲染层只用标准库 `json` 解析，不新增 `PyYAML` 依赖。

## 5. 新增一套模板

1. 在 `templates/poster/` 或 `templates/carousel/` 新建 `<id>.json`，`id` 与文件名一致。
2. 从最接近的现有模板复制，先定 `canvas` 尺寸和 `pad`（见 §6 建议值）。
3. 按视觉层级从上到下列块。`x`/`y` 从 `pad` 起算，`x + max_width` 不要超过 `W − pad`，
   `y + max_lines × line_height` 不要超过 `H − pad`；相邻文本块的预留高度不要重叠。
4. `wb render list` 确认新模板已被发现，`wb render ...` 渲染一次，确认无 `LayoutError`、图片
   尺寸与声明一致、文字没有被截断（需要时看 warning）。
5. 九图模板要让相邻页的块结构一致，只换文案槽位；页数由 `pages` 数组长度决定。
6. 在 `tests/test_render.py` 的模板遍历测试里会自动覆盖新模板（测试遍历 `list_templates()`）。

## 6. 字号与安全边距建议

| 用途 | 1080 宽画布建议 |
| --- | --- |
| 主标题 | 72–96 px，`max_lines` 2–3，`line_height` ≈ 1.2–1.35 × `size` |
| 副标题 | 36–44 px，`line_height` ≈ 1.5 × `size` |
| 正文 | 30–36 px，`line_height` ≈ 1.6 × `size` |
| 小字/页脚 | 24–28 px，单行，`max_lines=1` |
| 标签 | 24–28 px，`padding` 10–12 |

- 安全边距 `pad` 建议 64–96（1080 宽）；本轮模板统一用 72。
- 同屏保留至少 1 个字号档的间距，避免文字贴边或挤在一起。
- 一行中文超过约 24 字（96 px 标题约 9 字）就应拆行；靠 `max_lines` 兜底不如调小字号。
- 长文案优先调 `max_lines` + 字号，不要依赖截断；截断只用于兜底。

## 7. 产物与落库

- `wb render poster`：`--out` 指定的 PNG 文件。
- `wb render carousel`：`--out` 目录下 `01.png ... 0N.png` + `manifest.json`。manifest 记录页码
  顺序、每页文件名、每页用到的 slots、块数、尺寸与 warning。
- `wb run visual`：文件写到 `<out>/assets/`，并给每个 PNG 落一行 `asset`：poster 一行；carousel
  每页一行（`kind=carousel`，`meta` 里带 `page` / `page_count` / `manifest`）。`copy_set_id`
  是可选的 `--input`，用于把产物关联到文案。

## 8. 错误语义

| 情况 | 异常 | CLI 退出码 |
| --- | --- | --- |
| 模板/数据不合法、模板不存在、类型用错 | `TemplateError` / `WorkbenchError` | 1 |
| 版式越界、文本块重叠 | `LayoutError` | 1 |
| 字体文件缺失 | `MissingFont`（`Unavailable` 子类） | 1 |
| stage 缺 `--input data` 或 `copy_set_id` | `WorkbenchError` | 1 |
| stage 缺 `--input template` | `MissingParam`（runner） | 2 |
