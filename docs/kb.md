# 知识库（T03）

把品牌资料、产品信息、历史爆款与常见问答变成可被文案引用的检索层。

## 1. 快速开始

```sh
# 导入 seed 资料（7 篇虚构品牌资料）
wb kb add data/kb/seed
# docs=7 chunks=21

# 再跑一次：按 sha256 幂等
wb kb add data/kb/seed
# docs=0 skipped=7 chunks=0

# 提问：默认返回 5 条命中
wb kb ask "客单价怎么解释"
# 或输出结构化 JSON
wb kb ask "客单价怎么解释" --json

# 离线评测
wb kb eval
# recall@3=0.90 (>=0.70) 通过
```

`WB_HOME` 决定数据库位置（默认 `./.wb`），`wb kb add` 会自动建库并执行 T01 的迁移。

## 2. 文件与职责

| 文件 | 职责 |
| --- | --- |
| `workbench/knowledge/chunk.py` | Markdown 切块（纯函数） |
| `workbench/knowledge/index.py` | FTS5 + jieba 索引与检索 |
| `workbench/knowledge/build.py` | `build_kb(cfg) -> KB` 工厂 |
| `workbench/knowledge/answer.py` | 引用回答、80 字片段、recall@k 评测 |
| `workbench/stages/kb.py` | `kb_ingest`（导入）、`kb_ask`（检索） |
| `workbench/cli_ext/kb.py` | `wb kb add/ask/eval` |
| `data/kb/seed/*.md` | 7 篇虚构品牌资料 |
| `data/kb/eval.jsonl` | 20 条评测问答 |

## 3. 切块规则

- 按 Markdown 标题层级切：H1 作为文档标题，H2/H3/... 组成 `heading` 路径，
  例如 `## 产品` + `### 价格` → `产品/价格`。H1 本身不进入路径。
- 同一标题下的多个段落合并为一块；**目标** 300-800 字，标题边界优先。
  - 合并后达到 300 字即落块，因此相邻段落不会无限累积。
  - 再合并会超过 800 字时先落块，不跨标题合并。
  - 单个标题下的内容不足 300 字时保留短块；短块不跨标题拼接，
    避免把 `产品/价格` 的内容并进 `产品/服务`。
- 单个段落超过 800 字时按 `。！？；` 等句末标点切开，再按 300-800 字合并；
  没有句末标点的超长串按 800 字硬切。
- 没有标题的文档用文件名作为标题，`heading` 为 `None`；第一个 H2 之前的引言也会成为独立块。
- 标题、正文与文档标题都会进入检索串（见下节），但 `kb_chunk.heading` 只存路径。

切块不读写数据库，可单独用 `workbench.knowledge.chunk.parse_markdown` 测试。

## 4. 分词、索引与打分

### 写入

`kb_fts(chunk_id UNINDEXED, seg)` 的 `seg` 为：

```python
" ".join(jieba.cut_for_search(f"{heading}\n{title}\n{body}"))
```

- 使用 jieba 搜索模式，保留至少 2 个字符的中文词、英文词和数字，过滤标点与单字。
- 不使用 `tokenize='trigram'`：FTS5 的 trigram 对 2 字中文词无效（ADR §3 已记录）。
- 同一段文本内的重复词只保留一次，减少索引体积。

### 查询

1. 查询串先做同样的分词；为空或全被过滤时直接返回 `[]`（不抛异常）。
2. 用 `MATCH '"词1" OR "词2" ...'` 匹配，词用双引号包裹，避免 FTS5 把词当成运算符。
3. `ORDER BY bm25(kb_fts) ASC`：bm25 越小越相关。
4. 对本次结果集的 bm25 值做 min-max 归一化：
   `score = (worst - rank) / (worst - best)`，最相关为 1.0，最不相关为 0.0；
   只有 1 条命中时记为 1.0。`KBHit.score` 因此恒在 0-1 且越大越相关。
5. `--k` 默认 5，范围 1-20；`AskResult.hint` 在空查询、单字查询或无命中时给出提示。

### 去重与更新

- `kb_doc.sha256` 相同直接跳过，`add()` 重复执行不会产生重复块（幂等）。
- 同一 `source_path` 内容变化时，先删除旧文档的 chunk 与 FTS 行，再写入新版本；
  这是本 Ticket 提供的唯一“增量更新”能力，不做更多运维功能。
- `kb_doc.meta` 存 `{"tags": [...], "title": ...}`；`kind` 按文件名推断为
  `note | product | case | faq | other`。

## 5. 引用输出

`wb kb ask` 的每行（以及 `--json` 的每个 `hits[]`）包含：

- `doc_title`：`KBHit.title`，文档标题；
- `heading`：小节路径，可能为 `null`（无标题引言）；
- `score`：0-1 归一化得分，保留 3 位小数展示；
- `snippet`：压平空白后的前 80 字片段。

`KBHit` 的完整字段以 `workbench/contracts.py` 为准：
`doc_id / chunk_id / title / body / heading / score`。

## 6. 离线评测

`data/kb/eval.jsonl` 共 20 条，每行：

```json
{"q": "客单价怎么解释", "doc": "价格与客单价（虚构）", "note": "指标口径"}
```

评测口径是**文档级 recall@3**：用 `q` 检索前 3 条命中，只要期望文档 `doc`
的标题出现在其中就算命中；`recall@3 = 命中数 / 总数`。

```sh
wb kb eval                       # 默认阈值 0.70，低于阈值退出码 1
wb kb eval --threshold 1.0       # 故意拉满：0.90 < 1.0，退出码 1
wb kb eval --json                # 结构化结果与未命中明细
```

当前 seed 资料的实测结果为 `recall@3=0.90`。评测集保留了 2 条词面不重合的困难改写
（“消费者一单大概花多少钱”“老客户为什么愿意复购”），它们暴露了 FTS5 词面检索的
局限，因此 recall 不会达到 1.0；阈值默认 0.70 而不是 1.0。要提升这类问题，需要后续
Ticket 评估同义词表或向量检索，本 Ticket 不做。

## 7. 如何换成自己的资料

1. 把自己的 md/txt 放进任意目录（不要放进 `inputs/`，该目录只读）。
2. 执行 `wb kb add /path/to/your-docs`；子目录会递归扫描，只读取 `.md/.markdown/.txt`。
3. 用 `wb kb ask "你的问题" --k 5 --json` 检查命中；必要时调整标题层级和段落长度。
4. 把评测集换成自己的问答对（保持 `q`/`doc` 字段，`doc` 写文档标题），再跑
   `wb kb eval --eval /path/to/eval.jsonl`。
5. 内容更新后重新执行 `wb kb add`；同一路径内容变化会替换旧版本，sha256 不变则跳过。

## 8. stage 用法

`kb_ingest` 与 `kb_ask` 已注册到 stage 注册表；由于 `workbench/pipeline/definitions.py`
属于内核只读文件，本 Ticket 不修改固定流水线，因此这两个 stage 暂时不会出现在
`wb doctor` 的“流水线 stage 覆盖”表里，也不能通过 `wb run <pipeline>` 调用。

后续 Ticket 把 `kb_ingest` / `kb_ask` 加进 `copy` 或 `topic` 流水线后，可用
`--input path=data/kb/seed`、`--input q=...`、`--input k=3` 传参。当前可这样直接验证：

```python
from pathlib import Path

from workbench.config import Config
from workbench.llm.stub import StubLLM
from workbench.pipeline.stage import RunContext
from workbench.stages.kb import KbAsk, KbIngest
from workbench.store.db import init_db

cfg = Config.from_env()
conn = init_db(cfg.db_path)
ctx = RunContext(
    run_id="manual",
    pipeline="copy",
    home=cfg.home,
    out=cfg.home / "runs" / "manual",
    db=conn,
    llm=StubLLM(),
    cfg=cfg,
    params={"path": "data/kb/seed", "q": "客单价怎么解释", "k": "3"},
    log=print,
)
print(KbIngest().run(ctx))
print(KbAsk().run(ctx))
conn.close()
```

## 9. 已知限制

- 纯词面检索：同义改写、口语化提问可能检索不到（评测中的 2 条困难样本即为例证）。
- 只支持 md/txt，不支持 PDF、docx、网页抓取；不做 OCR。
- 不做向量检索、不引入 embedding 依赖。
- 单机单用户，不提供权限与并发写入之外的运维能力。
- seed 资料全部为虚构品牌（`demo_` 前缀或带“虚构”标记），价格与案例指标均为假设测算，
  不得当作真实数据对外发布。
