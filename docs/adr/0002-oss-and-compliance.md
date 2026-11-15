# ADR 0002 · 开源组件、许可证与合规红线

- 状态：已接受（2026-11-15）
- 依据：superagent 调研《开源方案-AIHOT与开源项目命中清单.md》（2026-11-14）与《超级个体获客工作台-技术栈与工具调研.md》
- 适用范围：本项目自建代码 + 将来可能接入的第三方组件

## 1. 结论先行

1. **MVP 不自托管任何「平台型」开源项目**。AiToEarn、Coze Studio、RAGFlow、poster-design、Superset 都不进 MVP 依赖。
2. 只用**库**，不用**平台**：`pillow`、`jieba`、`httpx`、`feedparser`、`imageio-ffmpeg`、`edge-tts`、`fastapi`、`jinja2` 等（全部 MIT/BSD/Apache/LGPL-兼容）。
3. 平台型开源项目留作**适配器实现**，接入时必须先在 PR 里回答 §4 的三问。

## 2. 为什么平台型项目不进 MVP

| 项目 | 许可证 | 不进 MVP 的理由 |
| --- | --- | --- |
| AiToEarn | MIT | OAuth 默认走官方 Relay，数据流一段不在自己手里；官方声明不接受外部 PR，出问题只能等官方 |
| Coze Studio | Apache-2.0 | Go + React + MySQL + 对象存储的完整平台，部署与加固成本高于本项目全部业务代码 |
| RAGFlow | Apache-2.0 | 需要 Docker + 向量库 + 文档解析服务；本项目 3 万字以内的知识库用 FTS5 足够 |
| poster-design | **AGPL-3.0** | 做成对外提供的服务有传染风险；且它是设计器，不是「批量出图」 |
| Superset | Apache-2.0 | 单机 BI，接进来又要一套服务；MVP 的周报是 Markdown |
| MediaCrawler | **非商用** | README 明确禁止商业用途，且抓取本身有法律与封号风险 → 本项目一律不抓 |
| social-auto-upload | MIT | 本质是浏览器自动化，账号风控与协议风险由使用者承担 → 只留适配位，默认关闭 |

## 3. 将来允许的接入点（按优先级）

1. **发布**：`Publisher` 适配器接 `social-auto-upload`，只在本机、只在自己账号、默认 dry-run。
2. **渲染**：`Renderer` 适配器加 `PlaywrightRenderer`（HTML/CSS → PNG），替代 Pillow 版式。
3. **数据**：平台官方导出（抖音创作服务平台、小红书蒲公英/千帆）CSV 优先；其次浏览器人工复制；**不做爬虫**。
4. **视频**：`VideoComposer` 适配器接 `VectCutAPI`（剪映草稿），用于复杂剪辑。
5. **知识库**：资料超过 500 篇或需要多模态时，再考虑 RAGFlow/AnythingLLM。

## 4. 接入任何第三方前必须回答

1. 它跑在谁的服务器上？（数据出不出本机）
2. 许可证是否允许我们这样用？（AGPL 是否构成对外服务；非商用条款）
3. 它挂了，本项目的降级路径是什么？（必须有 Null 实现兜底）

## 5. 合规红线（写进代码，不只写文档）

### 5.1 AI 生成内容标识

依据《人工智能生成合成内容标识办法》（2025-09-01 施行）：

- 所有 `copy_set` 与 `asset` 默认带 `ai_generated=true` 语义；发布包 `manifest.json` 必须含 `"ai_generated": true` 与「建议显式标识」提示文案。
- 发布包说明里固定给一段「可复制到平台声明处」的标识话术。

### 5.2 广告法与平台规则

- 极限词表（`data/compliance/absolute_terms.txt`）：「最好」「第一」「国家级」「100%」「绝对」等。
- 医疗/金融/教育等敏感承诺类表述进 `data/compliance/sensitive_terms.txt`，命中即 human review。
- 文案 lint 命中即 `copy_set.compliance.passed=false`，并不阻断生成（阻断交给人判断），但在发布包里高亮。

### 5.3 数据与个人信息

- 只处理自己账号与自己整理的公开对标数据；不存他人手机号、微信、私信内容。
- `post.caption` 里若出现第三方个人信息，导入时提示人工确认。

### 5.4 版权与字体

- 字体只内置 Noto Sans SC（SIL OFL 1.1），商用允许，随仓库分发许可文件。
- 图片素材：MVP 只生成纯色/渐变/文字版式，不使用来源不明的图片素材。
- BGM：MVP 视频无 BGM；后续接音乐时必须用可商用曲库。
