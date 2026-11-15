# 内置字体

| 文件 | 说明 |
| --- | --- |
| `NotoSansSC-Regular.otf` | Noto Sans CJK SC Regular 子集（拉丁 + 常用标点 + CJK 统一表意文字基本区） |
| `NotoSansSC-Bold.otf` | 同上，Bold |
| `LICENSE-OFL.txt` | SIL Open Font License 1.1 全文与版权声明 |

- 来源：`googlefonts/noto-cjk` 的 `Sans/OTF/SimplifiedChinese`（Noto Sans CJK SC），按 SIL OFL 1.1 分发，允许商用与再分发。
- 子集范围：`U+0020-007E`、`U+00A0-00FF`、`U+2000-206F`、`U+3000-303F`、`U+4E00-9FFF`、`U+FF00-FFEF`。
- 渲染层只通过 `WB_FONT_DIR`（默认 `assets/fonts`）查找这两个文件，**不依赖系统字体**。
- 换成别的字体时，保持文件名不变即可（或用 `WB_FONT_DIR` 指向新目录）。
