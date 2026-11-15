# T05 · 选题与爆款对标

> 从「热榜 JSON + RSS + 对标账号作品库」里算出这周该做的 10 个选题；每张选题卡都带可核对的对标证据。
> 全部计算确定性、可离线复算，不调 LLM，不联网。

## 1. 快速开始

```sh
# 1) 生成 10 条选题（默认 --offline 1，只读 data/seed 下的 fixture）
wb topic suggest --platform xhs --count 10 --offline 1 --out /tmp/tp
# 产物：
#   /tmp/tp/candidates.json  三个信源的原始候选（含来源计数）
#   /tmp/tp/topics.json      选题卡 + 平台对标画像
#   /tmp/tp/manifest.json    topic 流水线 manifest

# 2) 看某个对标账号的爆款画像（每项带样本数）
wb topic benchmark --account demo_bench_a --platform xhs

# 3) 把对标 fixture 幂等导入 account / post / metric
wb topic seed

# 4) 按分数查看已落库的选题
wb topic list --platform xhs --limit 20
```

等价的流水线调用：

```sh
wb run topic \
  --input platform=xhs --input count=10 --input offline=1 \
  --input hotlist=data/seed/hotlist_sample.json \
  --input rss=data/seed/rss_sample.xml \
  --input local=data/seed/benchmark --input benchmark_dir=data/seed/benchmark \
  --out /tmp/tp
```

`topic` 流水线只有两个 stage：

| stage | 职责 | 产物 |
| --- | --- | --- |
| `topic_source` | 调 hotlist / rss / local 适配器，过滤平台与 `since` | `<out>/candidates.json` |
| `topic_score` | 导入对标 fixture、算平台画像、打分、upsert `topic`、写选题卡 | `<out>/topics.json` |

### `suggest` 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--platform` | `WB_PLATFORM` | 只保留该平台的候选；`hotlist`/`rss`/`local` 都会带 platform |
| `--count` | 10 | 输出条数（1–50） |
| `--offline` | `1` | `1` 只读 fixture；`0` 才允许真实 HTTP（需在代码里提供 URL） |
| `--sources` | `hotlist,rss,local` | 可只启用部分信源做调试 |
| `--hotlist/--rss/--local` | `data/seed/...` | 覆盖 fixture 路径；`--local` 可传逗号分隔的文件/目录 |
| `--benchmark-dir` | `data/seed/benchmark` | 对标作品库目录（`*.jsonl`） |
| `--since` | 无 | 只取该 ISO 时间之后发布的条目 |
| `--days` | 90 | 画像回看天数 |
| `--as-of` | 对标库最新 `captured_at` | 画像与时效基准时间 |
| `--json` | 关 | 直接输出完整 `topics.json` |

`topics.json` 形如（空信源时只有 `platform/count/topics/message`）：

```json
{
  "platform": "xhs",
  "count": 10,
  "benchmark": { "hit_count": 9, "as_of": "...", "keywords": [], "...": "..." },
  "topics": [
    {
      "id": "topic_...",
      "platform": "xhs",
      "title": "...",
      "angle": "...",
      "hook": "...",
      "score": 0.70711,
      "score_parts": {"heat": 0.93, "fit": 0.2, "benchmark": 0.82, "timing": 1.0},
      "source": "hotlist",
      "source_ref": "https://example.invalid/hot/xhs/003",
      "status": "new",
      "evidence": [
        {"kind": "hotlist", "ref": "https://example.invalid/hot/xhs/003", "note": "热榜信源：..."},
        {"kind": "post", "ref": "post_bench_b_010", "note": "对标爆款：..."}
      ],
      "created_at": "..."
    }
  ]
}
```

`benchmark` 是平台聚合画像（字段见 §3），`topics` 按 `score` 降序。

## 2. 信源适配器

ADR §6.4 的契约是：

```python
class SourceAdapter(Protocol):
    name: str
    def fetch(self, *, since: str | None = None) -> list[RawItem]: ...
```

T05 的三个实现都满足它，并在内部额外提供 `fetch_candidates()`，返回带
`post_id / content_type / duration_sec / views` 的 `Candidate`，供对标打分使用。
`build_adapters(cfg)` 是 ADR 固定工厂，顺序固定为 `hotlist`、`rss`、`local`。

| 适配器 | `name` | 默认 fixture | 证据 `kind` | 备注 |
| --- | --- | --- | --- | --- |
| `HotlistSource` | `hotlist` | `data/seed/hotlist_sample.json` | `hotlist` | DailyHotApi 兼容形状 |
| `RssSource` | `rss` | `data/seed/rss_sample.xml` | `rss` | RSS / Atom，摘要去 HTML |
| `LocalSource` | `local` | `data/seed/benchmark/*.jsonl` | `post` | 本地对标作品库 |

### 2.1 热榜 JSON（DailyHotApi 兼容）

```json
{
  "code": 200, "name": "小红书", "type": "xhs",
  "data": [
    {"id": "xhs_hot_001", "title": "...", "desc": "...", "timestamp": 1763165160000,
     "hot": 51800, "url": "https://...", "mobileUrl": "https://...",
     "tags": ["职场", "效率"]}
  ]
}
```

解析容错：

- 根数组、`{data: [...]}`、`{data: {list: [...]}}` 都接受；缺 `title` 的条目跳过。
- `title` 也接受 `name / word / content`；`url` 接受 `url / link / mobileUrl`。
- `hot` 接受数字或 `1.2万 / 3.4k`；缺失时按 `heat=None`（打分用中性值）。
- `timestamp` 接受毫秒/秒时间戳或 ISO 字符串；坏时间按缺失处理，不让整批失败。
- `platform` 从条目的 `platform`、根对象的 `type/name` 或 `--platform` 推断。

### 2.2 RSS

`feedparser` 解析 bytes；`published_parsed / updated_parsed` 优先，其次原始
`published / updated` 字符串。摘要用 BeautifulSoup 去 HTML；`category` 映射为
`RawItem.tags`。缺 `link` 或 `pubDate` 的条目保留，`published_at=None`。

### 2.3 本地 fixture（对标作品库）

每行一个 JSON 对象：

```json
{"account": {"handle": "demo_bench_a", "display_name": "对标账号 A（虚构）",
             "platform": "xhs", "niche": "职场成长",
             "note": "T05 虚构对标数据，不是真实博主"},
 "post": {"id": "post_bench_a_009", "title": "...", "url": "https://example.invalid/...",
          "published_at": "2026-09-13T08:10:00+08:00", "content_type": "video",
          "topic_tags": ["职场", "周报", "效率"], "duration_sec": 42,
          "caption": "占位文案：..."},
 "metric": {"captured_at": "2026-11-10T10:00:00+08:00", "views": 9200, "likes": 645,
            "comments": 110, "shares": 92, "saves": 322, "follows": 184,
            "completion_rate": 0.78, "avg_watch_sec": 32.76}}
```

`LocalSource` 也接受根数组的 `.json`、扁平作品对象和 JSONL；缺 `title` 的条目跳过。
对标 fixture 的 `caption` 是**占位文本**，不参与关键词/拟合度打分（只用 title 与
`topic_tags`），避免占位词污染画像。

### 2.4 新增信源

1. 在 `workbench/topics/sources.py` 新增类，固定 `name`，实现 `fetch(since=...)`，
   建议再实现 `fetch_candidates(since=..., tz=...)`。
2. 在 `build_adapters()` 里追加实例，并同步 `SOURCE_NAMES` 与 CLI `--sources` 文档。
3. 默认必须离线可跑：fixture 路径从 `default_seed_dir()` 取；真实 HTTP 只在
   `offline=False` 且显式给 URL 时启用。
4. 测试只准用 fixture；`tests/conftest.py` 会禁用 socket。

## 3. 爆款画像（`benchmark.py`）

### 3.1 burst 与爆款集

对每个对标账号：

1. 取该账号近 `--days`（默认 90）天、有最新 `metric` 的作品；
2. `P50 = quantile(views, 0.5)`；
3. `burst = views / P50`（P50 缺失或非正时 burst=0）；
4. 先保留 `burst >= 1.5` 的作品，再按 burst 降序取 `ceil(样本数 × 10%)`（至少 1 条）作为**爆款集**。

「近 90 天」的基准是**对标库最新 `captured_at`**（数据快照时间），不是 wall clock。
这样静态 fixture 不会随真实日期推移而失效，同输入永远同结果；可用 `--as-of` 覆盖。

### 3.2 画像字段

`wb topic benchmark --account demo_bench_a` 输出（`--json` 为同结构 JSON）：

| 字段 | 含义 | 样本数 |
| --- | --- | --- |
| `post_count` | 窗口内有指标的作品数 | — |
| `hit_count` | 爆款集大小 | — |
| `p50_views` | 账号 views 中位数 | — |
| `duration` | `{n, median, p25, p75, min, max}`，仅视频有值 | `n` |
| `title_length` | 标题字符数的中位/四分位区间 | `n` |
| `keywords` | 爆款标题 + topic_tags 的 jieba 高频词 Top 10（`[{token,count}]`） | 每个词的 `count`，样本为 `hit_count` |
| `time_slots` | 早 / 午 / 晚 / 深夜 的 `{n, share}` | `n` |
| `content_types` | 图文 / 视频 / 文章 的 `{n, share}` | `n` |
| `top_posts` | 爆款优先、不足时按 views 补足的候选作品；`is_hit` 标识是否爆款 | — |

示例：

```
对标账号：demo_bench_a（平台 xhs）
样本：25 篇（近 90 天）  爆款：3 条（burst>=1.5，Top 10%）  P50 views：1310.0
时长（秒）：n=3 中位 42.0 P25–P75 40.0–48.5 全距 38.0–55.0
标题长度（字）：n=3 中位 17.0 P25–P75 16.0–17.5 全距 15.0–18.0
Top5 标签：效率×3、周报×2、复盘×2、职场×2、通勤×2（基于 3 条爆款）
发布时段（n=3）：早 2（66.7%）  午 0（0.0%）  晚 1（33.3%）  深夜 0（0.0%）
内容类型（n=3）：图文 0（0.0%）  视频 3（100.0%）  文章 0（0.0%）
```

怎么读：

- **先看 `hit_count`**。小于 3 时画像只供参考，不要把「共性」当结论。
- `duration.p25–p75` 是爆款最集中的时长区间；`min–max` 是全距，样本少时别用全距做硬约束。
- `keywords` 是词频，不是因果；它只说明这批爆款「常一起出现什么词」。
- `time_slots` / `content_types` 的分母都是 `hit_count`；`share` 保留 6 位小数。
- 平台画像（`topics.json.benchmark`）把各账号的 burst 合并；burst 已按账号归一化，
  所以不同粉丝量级的账号可以放在一起看。

`topic` 表的 `score_parts` 按 ADR §5 固定为四个 float：`heat / fit / benchmark / timing`。
完整画像不会塞进 `score_parts`（那会违反 `dict[str, float]` 契约），而是放在
`topics.json.benchmark` 与打卡命令输出里；选题卡里只用一条 `benchmark` 分数。

### 3.3 fixture 说明

- `data/seed/benchmark/demo_bench_{a,b,c}.jsonl` 各 25 条，账号名一律
  `demo_bench_*`，`display_name` 带「（虚构）」，作品文案是 `占位文案：...`；
- 不得写入真实博主、真实平台数据；指标全部是虚构 fixture。

## 4. 打分模型（`score.py`）

### 4.1 公式与权重

```
score = 0.35*heat + 0.25*fit + 0.25*benchmark + 0.15*timing
```

四个分项都归一到 `[0, 1]`，`score` 与分项保留 6 位小数。

| 分项 | 计算 | 缺失时 |
| --- | --- | --- |
| `heat` | `min(1, ln(1+h) / ln(1+100000))` | `None → 0.3`；`h<=0 → 0` |
| `fit` | `0.6 × 定位覆盖 + 0.4 × 历史覆盖`（jieba 词集覆盖率） | 无自己账号时用对标账号 `niche` 词兜底；无历史时历史覆盖=定位覆盖 |
| `benchmark` | `0.30×标题长度 + 0.35×关键词重叠 + 0.20×内容类型 + 0.15×时长` | 各子项无数据时给 0.5 中性值 |
| `timing` | `0.5 ** (age_hours / 36)` | `since` 或发布时间缺失时 age=0（timing=1） |

`fit` 的「历史」= 自己账号里 views 不低于中位数的作品的 `topic_tags`；「定位」=
自己账号的 `niche`。没有自己账号时，用同平台对标账号的 `niche` 词兜底并在
`wb topic benchmark`/文档里明确这是兜底值，不是真实定位。

`benchmark` 子项：

- 标题长度落在爆款集 `p25–p75` → 1.0；落在 `min–max` → 0.7；否则 0.3；
- 候选词命中爆款高频词，`min(1, 命中数/2)`；
- 内容类型等于爆款集里样本最多的类型 → 1.0，否则 0.4；无信息时 0.5；
- 视频时长落在爆款时长 `p25–p75` → 1.0；`min–max` → 0.7；否则 0.3；非视频给 0.5。

### 4.2 时效基准与确定性

- **时效基准**：优先 `--as-of`；否则取对标库最新 `captured_at`；没有对标数据时取候选里
  最新的发布时间。**不使用 wall clock**，所以同一份 fixture 两次运行分数完全一致。
- jieba 固定 `HMM=False`，关键词按 `(词频降序, 词升序)` 排序。
- 排序 tie-breaker：`(-score, title, source_ref)`；同输入同输出。
- 不用随机数、不用 LLM、不读网络。
- 同一 `(platform, source_ref)` 重复导入是 **update**，不新增行；`status` 保持用户已有值。

### 4.3 证据

每张选题卡的 `evidence` 至少 3 条，最多 5 条：

1. 候选本身：`kind` 为 `hotlist` / `rss` / `post`，`ref` 为 URL、fixture id 或对标作品 id；
2. 对标画像里的爆款/候选作品：按与候选的 jieba 词重叠排序，同分看 `is_hit`、burst、id；
   `ref` 是真实存在于 `post` 表的 id。

若某候选凑不满 3 条证据（例如没导对标库），该候选不会产出；全部候选都不足时
`topics.json` 给出「对标证据不足：请先导入对标 fixture（wb topic seed）」。
`ref` 必须是真实 id 或 URL，不编造。

### 4.4 为什么不用 LLM 打分

- **可复算**：同样输入必须得到同样 10 条选题与分数，才能做 A/B、回溯和验收；
  LLM 采样有温度、会漂移。
- **可解释**：四个分项都是显式公式，选题卡能直接回答「为什么排第 1」；
  LLM 打分只能给一段理由，无法核对权重。
- **离线**：R2 要求 `WB_LLM=stub` 全链路可跑，打分不应依赖 key/网络。
- **不做语义判断**：MVP 阶段信息量（标题、标签、指标）足够做排序；真正需要语义
  判断的部分留给 T06 文案生成与人工审核。

## 5. 空信源与错误处理

```sh
# fixture 为空数组
wb topic suggest --sources hotlist --hotlist /tmp/empty.json --offline 1 --out /tmp/tp
# 输出：信源无新内容；退出码 0；topics.json 里 topics=[]
```

- 信源 fixture 不存在/JSON 坏掉 → `TopicError`，退出码 2，不抛 traceback。
- 对标账号不存在 → `TopicError`（提示 `--account` 或 `wb topic seed`），退出码 2。
- `count` 不在 1–50、`days <= 0`、`--offline` 非布尔值 → `TopicError`，退出码 2。
- `--sources` 有未知名字 → 列出可用值，退出码 2。

以上退出码指 `wb topic ...` 子命令；`wb run topic` 走 runner，stage 异常按 ADR §6.1
统一包成 `PipelineFailed`（退出码 1），job 行会记录原始错误。

## 6. 数据契约与文件

| 路径 | 说明 |
| --- | --- |
| `workbench/topics/sources.py` | 三个适配器 + `build_adapters(cfg)` |
| `workbench/topics/benchmark.py` | fixture 导入、burst/画像、聚合 |
| `workbench/topics/score.py` | 四分项打分、证据、选题卡模板 |
| `workbench/stages/topic.py` | `topic_source` / `topic_score` |
| `workbench/cli_ext/topic.py` | `wb topic suggest/benchmark/list/seed` |
| `data/seed/hotlist_sample.json` | 18 条虚构热榜 |
| `data/seed/rss_sample.xml` | 12 条虚构 RSS |
| `data/seed/benchmark/*.jsonl` | 3 个虚构对标账号 × 25 条 |
| `tests/test_topic.py` | 适配器/画像/打分/CLI/边界 |

`topic` 行字段（ADR §5）：`title / angle / hook / score / score_parts / source /
source_ref / evidence / status / created_at`；`angle` 与 `hook` 是选题方向，不是最终文案
（文案由 T06 生成）。

## 7. 测试与验收命令

```sh
python -m pytest tests/test_topic.py -q
bash scripts/check.sh   # ruff check . + pytest -q
```

覆盖：

- 三个适配器的正常解析 + 字段缺失容错（热榜/ RSS / 本地 JSONL）；
- 边界：两个候选的 burst 恰好 1.5 与 2.0 进入爆款集，1.4 被阈值挡掉；
- 画像每项 `n` 样本数、Top5 标签、时段/内容类型分布；
- 打分确定性（两次运行 score 序列 `==`）、四个分项范围、证据 ≥3 且 ref 可查；
- 空信源退出码 0 + 「信源无新内容」；
- 时效衰减 age=0/36/72h ≈ 1.0/0.5/0.25；
- `wb topic` 子命令与 `wb run topic` 的 manifest/落库。

## 8. 已知限制与后续建议

- 真实 HTTP 抓取只留了适配器入口，未接具体站点；`--offline 0` 也不代表会自动找 URL。
- 对标画像依赖导入数据质量；fixture 只是形状演示，不能当真实爆款结论。
- `fit` 在「没有自己的账号数据」时是兜底信号，排序解释力有限；建议先 `wb review import`
  导入自己的账号与历史作品。
- 90 天窗口以数据快照时间为基准；跨数据集比较时要显式 `--as-of`。
- 后续可考虑：把 `topic` 选题卡接到 T06 文案生成、T08 发布包；增加「选题去重/相似度」
  与「对标账号分组画像」。
