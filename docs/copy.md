# 文案生成与合规闸门（T06）

一次调用把一张选题卡变成 **5 类 × 2 长度 = 10 条**可直接进入审核的文案，并为每条做两件事：

1. 用知识库命中片段给 prompt 取证，正文里的数字必须能在片段里找到出处；
2. 过极限词、敏感词与数字出处检查，结果写进 `copy_set.compliance`。

生成失败不阻断落库（人要能看见问题），但 CLI 用退出码 3 提示脚本；发布决定始终由人做。

## 1. 快速开始

```sh
# 0. 准备一张选题卡（T05 的 wb topic suggest 会写入 topic 表）；
#    本 Ticket 只读取 topic，不创建 topic。
# 1. 生成五类 × 两长度，共 10 条
WB_LLM=stub wb copy gen --topic <topic_id> --out /tmp/cp
# copies=10 copy_set=10 evidence=5
#   spoken/short  ...
#   ...

# 2. 只生成笔记类（short + long = 2 条）
WB_LLM=stub wb copy gen --topic <topic_id> --kinds note --out /tmp/cp2

# 3. 重新检查已有 copies.json；未通过时退出码 3
wb copy lint --file /tmp/cp/copies.json
```

产物：

- `<out>/copies.json`：10 条（或裁剪后的条数）+ 本次 evidence / citations + 每条 compliance；
- `<out>/manifest.json`：pipeline runner 写的 stage 执行记录；
- `copy_set` 表：每条一行，同一 run 的 10 行共用同一个 `created_at`。

`WB_HOME` 默认 `./.wb`。数据库迁移由 T01 的 `wb db init` 自动执行。

> 本 Ticket 只读取 `topic`，不创建选题卡。若当前分支还没有 T05 的 `wb topic`
> 命令，可以先跑 `wb run demo`，`demo_topic` 会写入一条虚构选题；再从
> `WB_HOME/wb.sqlite3` 的 `topic` 表查出它的 `id` 作为 `--topic`。

## 2. 五类文案的结构要求

| kind | 用途 | 公共字段 | 额外字段 | short | long |
| --- | --- | --- | --- | --- | --- |
| `spoken` | 口播脚本（含镜头提示） | `title/hook/body/tags` | `shots: list[str]` | 100 字内，15-30 秒 | 400-800 字，60-90 秒 |
| `note` | 小红书图文笔记正文 | 同上 | — | 100 字内 | 400-800 字 |
| `seed` | 种草/带货（卖点 + 异议处理） | 同上 | — | 100 字内 | 400-800 字 |
| `explain` | 知识科普/干货 | 同上 | — | 100 字内 | 400-800 字 |
| `hook` | 评论区话术 / 私信 / 标题钩子 | 同上 | `variants: list[str]`（至少 5 条） | 100 字内 | 400-800 字 |

- `title`、`hook`、`body` 必填；缺字段时 `generate` 抛
  `CopyValidationError`，消息含 `kind`、`length` 与缺失字段名。
- 长度是写作目标，不是落库门槛；`copies.json` 里保留 `length` 与
  `target_chars`，长文案过短不会阻断，但会在评审时明显可见。
- `stub` 模式对 `hook.variants` 补足 5 条确定性占位变体；真实模型不足 5 条时直接报错，
  不替模型编造内容。

## 3. prompt 文件

路径：`prompts/copy/<kind>.md`，5 个文件一一对应 5 个 kind。

结构：

```text
---
version: 1
model: deepseek-chat
temperature: 0.8
max_tokens: 1800
---

（正文模板，只允许 {brief} / {evidence} / {brand} 三个占位符）
```

| 占位符 | 内容 |
| --- | --- |
| `{brief}` | 选题卡 JSON + 本次 `kind` / `length` / `target_chars` |
| `{evidence}` | KB 命中片段，最多 5 条；为空时写“不要写具体数字”的提示 |
| `{brand}` | 品牌/语气摘要，可为空；为空时提示不要编造品牌 |

- `model` 只记录在 prompt 文件里，不强制切换 LLM；实际模型以 `WB_LLM` / `WB_LLM_MODEL` 为准。
- `temperature`、`max_tokens` 会传给 `LLMClient.json()`。
- `prompt_version` = `v<version>+<文件 sha256 前 8 位>`，写进每条
  `copy_set.prompt_version`，可回滚到旧 prompt。

## 4. 知识库取证

`workbench/copy/grounding.py`：

1. 用选题卡的 `title + angle + hook` 调 `KB.search(query, k=1..5)`；
2. 用 `品牌 定位 目标人群 语气` 再取 1 条品牌摘要（可为空）；
3. 命中片段去重后最多 5 条，按 `[{doc_id, chunk_id, quote}]` 写进
   `copy_set.citations`；
4. 知识库为空或检索失败时降级为空 evidence，生成继续，但正文里的数字会被判无出处。

`evidence` 与 `brand` 的原文都会保存到 `copies.json`，所以 `copy lint` 可以离线复算，
不需要再次检索。

## 5. 合规与引用校验

`workbench/copy/lint.py` 的检查顺序：

1. **数字出处**：正文（含标题、钩子、标签、镜头/变体）中每个阿拉伯数字，必须能在
   evidence 或 brand 原文中找到同一个数；`99` 与 `99.0` 视为同一个数。找不到即
   `kind=ungrounded_number`，`passed=false`。
2. **极限词**：命中 `data/compliance/absolute_terms.txt` 的 `source` 即
   `kind=absolute`，`passed=false`，并输出 `target` 作为替换建议。
3. **敏感词**：命中 `data/compliance/sensitive_terms.txt` 即 `kind=sensitive`，
   `needs_human=true`；按 ADR 0002 §5.2“lint 命中即 passed=false”的口径同时置
   `passed=false`，交给人判断。

`copy_set.compliance`：

```json
{
  "passed": false,
  "needs_human": false,
  "hits": [
    {
      "word": "全网最低价",
      "kind": "absolute",
      "index": 0,
      "note": "极限词",
      "suggestion": "更有性价比"
    }
  ],
  "checked_at": "2026-11-15T21:20:57+08:00"
}
```

- `passed=false` 仍然落库；`wb copy gen` 在任一未通过时打印替换建议并返回退出码 3。
- `wb copy lint --file <copies.json>` 重新检查并写回文件与 `copy_set.compliance`；
  未通过同样退出码 3。
- 词表格式：一行一个 `source: target`；`#` 开头为注释，空行忽略。敏感词右侧是
  人工确认提示，可省略。

## 6. 文件与职责

| 文件 | 职责 |
| --- | --- |
| `workbench/copy/schema.py` | 五类 pydantic 模型、kind/length 解析、字段错误转换 |
| `workbench/copy/prompts.py` | 读取 front-matter、校验占位符、渲染 prompt 与版本号 |
| `workbench/copy/grounding.py` | KB 检索、evidence/brand 灌注、citations |
| `workbench/copy/lint.py` | 数字出处、极限词、敏感词、compliance 写回 |
| `workbench/copy/generate.py` | 10 个组合的生成编排、copies.json、copy_set 落库 |
| `workbench/stages/copy.py` | `copy_generate`、`copy_lint` 两个 stage |
| `workbench/cli_ext/copy.py` | `wb copy gen` / `wb copy lint` |
| `prompts/copy/*.md` | 5 个带版本的 prompt |
| `data/compliance/*.txt` | 极限词与敏感词表 |
| `tests/test_copy.py` | 组合齐全、裁剪、字段缺失、极限词、数字出处、版本号等测试 |

## 7. 改 prompt 的流程

1. 改 `prompts/copy/<kind>.md` 的正文或 front-matter 参数；结构要求变化时先改
   `docs/copy.md` 与 `schema.py`，再改 prompt。
2. 如果字段有变，先更新 `schema.py` 的模型与 `tests/test_copy.py` 的断言；不要只改 prompt，
   否则真实模型输出会因字段不匹配被判失败。
3. 递增 `version`（例如 `1` → `2`），保留 `model/temperature/max_tokens`；
   文件 sha256 会自动进入 `prompt_version`，不必手写哈希。
4. 运行：

   ```sh
   WB_LLM=stub wb copy gen --topic <id> --out /tmp/cp
   wb copy lint --file /tmp/cp/copies.json
   bash scripts/check.sh
   ```

5. 用真实模型（`WB_LLM=openai` + `WB_LLM_API_KEY`）各生成一次，人工阅读 10 条文案；
   这一步不在 CI 中执行。
6. 在 PR 描述里记录 prompt 版本变化与人工抽检结论；旧 `copy_set` 行保留旧
   `prompt_version`，便于对比。

## 8. 如何加一类新文案

1. 在 `schema.py` 的 `KINDS` 增加 kind，并新增模型（公共字段继承 `CopyBase`）。
2. 在 `prompts.py` 的 `KINDS` 校验范围内新增 `prompts/copy/<kind>.md`，写清结构、
   长度与禁用表达。
3. 如需额外字段，在 `schema.py` 的 `finalize_payload` 补校验；stub 模式需要补确定性占位值。
4. 更新本文件的“五类文案的结构要求”表与 `tests/test_copy.py` 的组合断言。
5. 运行 `bash scripts/check.sh`，确认 `copies.json` 组合数与 `copy_set` 行数同步增加。

## 9. 已知限制与决策

- **只读取 topic，不创建 topic**：选题卡由 T05 负责；本 Ticket 不复制 T05 的打分逻辑。
- **每个组合一次模型调用**：10 次调用便于分别约束结构，但真实模型成本是 10 次；
  若后续要合并为一次调用，必须保证字段仍可逐条校验。
- **数字检查是严格子串口径**：`2026-11-15` 这样的日期也会被拆成多个数字；正文要写日期时，
  应确保 evidence 里也有同样文本。中文数字（“三天”“九成”）不会被抽出。
- **极限词按子串匹配**：“第一步”会命中“第一”；prompt 已要求避免这类表达，人工审核时
  可按 hit 的 `index` 判断是否为误伤。
- **证据只来自 KB**：topic 卡里的 evidence 不会自动当作文案数字出处；如需引用选题卡里的
  数据，应先导入知识库或写进品牌资料。
- **stub 产物是占位文本**：`title/body` 为 `【stub】...`，用于验证结构、落库与闸门，不代表
  可发布内容；真实文案必须用真实模型或人工撰写。
- 测试禁止联网、禁止真实 key；真实模型路径用 `pytest.mark.skipif` 跳过。
