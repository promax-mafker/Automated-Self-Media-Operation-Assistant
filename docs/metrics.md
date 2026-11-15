# 账号数据复盘与周报（T02）

本文是 T02 的契约文档：CSV 列映射、导入行为、指标口径、分组口径与周报模板。
实现位于 `workbench/metrics/`，stage 位于 `workbench/stages/metrics.py`，CLI 位于
`workbench/cli_ext/review.py`。

所有数据只来自**本地 CSV 导入**；本 Ticket 不抓取平台数据、不调用平台 API、不发布内容。

## 1. 命令

```sh
# 导入平台导出 CSV；--account 指定自己的账号，CSV 内其他 handle 标为对标账号
wb review import --csv data/seed/metrics_sample.csv --account demo_xhs

# 生成 2026-W45 的 Markdown 周报（<out>/report.md）
wb review week --account demo_xhs --week 2026-W45 --out /tmp/rev

# 也可以走 review 流水线（metrics_import → review_report）
wb run review \
  --input csv=data/seed/metrics_sample.csv \
  --input account=demo_xhs \
  --input week=2026-W45 \
  --out /tmp/rev-run
```

- `wb review import` 输出 `imported=<N> accounts=<N> skipped=<N>`；`accounts` 是 CSV 中出现的
  不同账号数，不是本次新建的账号数。
- 重复导入同一文件时，`(post_id, captured_at)` 已存在则计入 `skipped`，不新增行。
- `wb review week` 输出 `account=... week=... posts=... report=...`；周报固定写到
  `<out>/report.md`。
- 缺少 CSV、缺列、周格式错误、账号不存在等都退出码 2，错误写 stderr。

## 2. CSV 列映射

列名匹配忽略大小写、空格、下划线、连字符、斜杠、点与中英文括号，例如
`published_at`、`published-at`、`Published At` 视为同一列。

下表中「必需」为「是」的列缺一列就拒绝导入；错误会一次列出所有缺失列及每列支持的别名。
未识别的列不报错，会原样写入 `metric.raw`（紧凑 JSON），便于追溯平台原始行。

| 统一字段 | 必需 | 支持的列名（别名） | 说明 |
| --- | --- | --- | --- |
| `post_id` | 是 | `post_id` / `作品ID` / `笔记ID` / `视频ID` / `帖子ID` / `id` | 作品唯一 ID；同一 `post_id` 不能属于两个账号 |
| `account` | 否 | `account` / `账号` / `账号handle` / `博主` / `作者` / `handle` | 缺列时用 `--account` 兜底 |
| `display_name` | 否 | `display_name` / `账号名称` / `昵称` / `作者名称` | 首次创建账号时写入 |
| `niche` | 否 | `niche` / `领域` / `赛道` | 首次创建账号时写入 |
| `platform` | 否 | `platform` / `平台` | 缺省取 `--platform`，再缺省取 `WB_PLATFORM`；仅支持 xhs / douyin / shipinhao / wechat |
| `title` | 是 | `title` / `标题` / `作品标题` / `笔记标题` | 作品标题 |
| `url` | 否 | `url` / `链接` / `作品链接` | 作品链接 |
| `published_at` | 是 | `published_at` / `发布时间` / `发布日期` / `publish_time` / `post_time` | 发布时间，见 §2.1 |
| `content_type` | 是 | `content_type` / `内容类型` / `作品类型` / `类型` / `type` | 映射见 §2.2 |
| `topic_tags` | 否 | `topic_tags` / `话题` / `标签` / `tags` | JSON 数组或分隔符串，见 §2.1 |
| `duration_sec` | 否 | `duration_sec` / `时长` / `视频时长` / `duration` | 秒数、`mm:ss` 或 `hh:mm:ss` |
| `caption` | 否 | `caption` / `正文` / `描述` / `文案` | 正文/描述 |
| `captured_at` | 是 | `captured_at` / `采集时间` / `抓取时间` / `快照时间` / `snapshot_at` / `数据时间` | 指标快照时间，见 §2.1 |
| `views` | 是 | `views` / `播放量` / `观看量` / `阅读量` / `view_count` / `play_count` | 播放/阅读/观看量 |
| `likes` | 是 | `likes` / `点赞` / `点赞数` / `like_count` | 点赞数 |
| `comments` | 是 | `comments` / `评论` / `评论数` / `comment_count` | 评论数 |
| `shares` | 是 | `shares` / `分享` / `分享数` / `转发` / `转发数` / `share_count` | 分享/转发数 |
| `saves` | 是 | `saves` / `收藏` / `收藏数` / `save_count` / `collect_count` | 收藏数 |
| `follows` | 是 | `follows` / `关注` / `涨粉` / `新增关注` / `follow_count` | 本条内容带来的新增关注 |
| `completion_rate` | 否 | `completion_rate` / `完播率` / `完播` / `completion` | 0–1 小数或百分数，见 §2.3 |
| `avg_watch_sec` | 否 | `avg_watch_sec` / `平均观看时长` / `平均播放时长` / `avg_watch` | 平均观看秒数 |

### 2.1 数字、时间与标签

- **计数**：支持千分位逗号与空格，例如 `12,340`；支持 `1.2万`（×10000）、`3.5k`（×1000），
  结果四舍五入为整数。计数不能为负。
- **时间**：支持 ISO 8601（`2026-11-02T08:30:00+08:00`、`2026-11-02T00:30:00Z`）与
  `2026-11-02 08:30:00` / `2026-11-02`。没有时区的时间按 `WB_TZ` 解释；写入数据库前统一
  换算为 `WB_TZ` 的 ISO 8601 字符串。
- **时长**：支持 `15`（秒）、`00:15`、`01:02:03`，也接受 `15s` / `15秒`。
- **话题标签**：支持 JSON 字符串数组（如 `["复盘","效率"]`），也支持用 `|`、`,`、`，`、`、`
  分隔（如 `复盘|效率`）。空值写成空数组。

### 2.2 内容类型

| 导入值（大小写/空格不敏感） | 落库值 | 报表分组名 |
| --- | --- | --- |
| `image_text` / `图文` / `图片` / `note` | `image_text` | 图文 |
| `video` / `视频` / `短视频` | `video` | 视频 |
| `article` / `文章` / `长文` | `article` | 文章 |

其他值报错并列出支持的类型，不静默降级。

### 2.3 完播率

`completion_rate` 接受：

- `0.86`（0–1 小数，按 86% 处理）；
- `86%`（百分号，按 86% 处理）；
- `86`（大于 1 且无百分号，按 86% 处理）。

超出 0–100% 范围报错。若该列为空，计算时会尝试用 `avg_watch_sec / duration_sec` 估算并截断到
0–1；两者都缺失则该作品不计入完播率均值。

## 3. 导入行为

1. 先解析并校验全部行，再写数据库；任何一行格式错误都不会留下半截导入。
2. `--account` 指定自己的账号；CSV 内其他 handle 自动写成 `is_benchmark=1`。
   - CSV 没有账号列时，`--account` 必填。
   - CSV 有多个账号但没给 `--account` 时，报错并要求指定自己的账号。
   - 只含一个账号且没给 `--account` 时，按自己账号导入。
3. CSV 有账号列时，逐行使用该行 handle；空 handle 回退到 `--account`。
4. 账号按 `(platform, handle)` 查找；第一次导入时创建，后续导入复用。账号的
   `display_name` / `niche` / `is_benchmark` 取首次创建时的值。
5. 作品按 `post_id` 查找；已存在且属于其他账号时报错。
6. 指标按 `(post_id, captured_at)` 幂等：已存在则 `skipped += 1`，不覆盖旧快照。
7. `metric.raw` 保存 CSV 原始行（去掉空值）的紧凑 JSON。

缺列错误示例（stderr，退出码 2）：

```
CSV 缺少必需列：
  - views（支持别名：views / 播放量 / 观看量 / 阅读量 / view_count / play_count）
  - captured_at（支持别名：captured_at / 采集时间 / 抓取时间 / 快照时间 / snapshot_at / 数据时间）
```

## 4. 指标口径

周报里的每个数字都按下表计算；「本周」指 `published_at` 落在指定 ISO 周内的作品。

| 指标 | 公式 / 口径 | 缺失与边界 |
| --- | --- | --- |
| 发布量 | 本周作品数 | — |
| views 中位数 | 本周作品 `views` 的中位数（线性插值） | 无作品时不生成周报 |
| views P90（本周） | 本周作品 `views` 的 P90（线性插值） | 同上 |
| interaction_rate | `(likes + comments + saves + shares) / views`，先逐篇计算，再对本周作品取算术平均 | `views <= 0` 的作品该值为空，不参与平均 |
| save_rate | `saves / views`，先逐篇计算，再取平均 | 同上 |
| follow_rate | `follows / views`，先逐篇计算，再取平均 | 同上 |
| completion_rate | 逐篇取 `completion_rate`；缺失时用 `avg_watch_sec / duration_sec` 估算；再取平均 | 无值的作品不参与平均 |
| burst_ratio（爆款率） | 逐篇 `views / P90(同账号历史 views)`，再取平均 | 历史 P90 为 0 或为空时该值为空 |
| publish_gap（发布间隔） | 本周作品按发布时间排序，相邻间隔（小时）的中位数 | 少于 2 篇时为空 |
| 异常作品数 | 本周 `views` 的 z-score 绝对值 ≥ 2 的作品数 | 少于 2 篇时 z-score 无意义，记为 0 |

补充约定：

- **P90 与分位数**：使用线性插值，`rank = (n - 1) * p`，在相邻两个值之间插值。
- **z-score**：`(views - 均值) / 总体标准差`，总体标准差除以 `n`（不是 `n - 1`）。
  这样「本周作品」被视为总体，而不是从更大总体抽出的样本。阈值固定为 `|z| >= 2`。
- **同账号历史**：`burst_ratio` 的 P90 取该账号在数据库中**全部已导入作品**的最新一次指标快照，
  包含本周作品；多次快照只取 `captured_at`最新的一次。这样口径稳定、可复算。
- **互动计数缺失**：本 Ticket 的导入要求 likes / comments / shares / saves / follows 齐全；
  若其他来源写入 `NULL`，计算时按 0 计入分子。

## 5. 分组口径

固定分三个维度，零样本分组也会出现在表里并标 `样本不足`。

### 5.1 内容类型

图文（`image_text`）/ 视频（`video`）/ 文章（`article`）。

### 5.2 发布时段

按 `published_at` 换算到 `WB_TZ` 后的本地小时：

| 分组 | 小时范围 |
| --- | --- |
| 早 | 06:00–11:59 |
| 午 | 12:00–17:59 |
| 晚 | 18:00–23:59 |
| 深夜 | 00:00–05:59 |

### 5.3 时长

| 分组 | 秒数范围 |
| --- | --- |
| ≤15s | `duration_sec <= 15` |
| 15–30s | `15 < duration_sec <= 30` |
| 30–60s | `30 < duration_sec <= 60` |
| >60s | `duration_sec > 60` |
| 无时长 | `duration_sec` 为空（图文/文章） |

### 5.4 样本不足

- 样本量 `n < 3` 的分组在周报表里标 `样本不足`。
- `样本不足` 的分组不参与「下周三条动作」的推荐与对比。
- 每个分组会给出：样本量、views 中位数、interaction_rate 均值、save_rate 均值、
  completion_rate 均值、burst_ratio 均值。

## 6. 周报结构

`wb review week` 固定生成五个二级标题：

1. `## 概览`：账号、平台、周范围、发布量、总 views、异常作品、历史样本、缺指标作品。
2. `## 指标表`：至少 8 行指标，每行带口径说明。
3. `## 分组对比`：内容类型 / 发布时段 / 时长三张表，`样本不足` 标在备注列。
4. `## Top5 与 Bottom5`：按 views 排序，各最多 5 行，带 `作品 ID`、`views`、
   `interaction_rate`。
5. `## 下周三条动作`：恰好三条，每条都是「做什么 + 依据哪个数字」，且至少引用一个
   作品 id 或分组名；不会引用 `样本不足` 的分组。

周计算：

- 一律按 `WB_TZ` 归 ISO 周；`2026-W45` 表示 2026-11-02（周一）00:00 到
  2026-11-09（周一）00:00（不含）。
- 跨时区时间先换算到 `WB_TZ` 再判断属于哪一周。
- 指标取每篇作品 `captured_at` 最新的一次快照，不要求快照本身落在该周内。
- 该周没有可用作品时报错（退出码 2），不生成空周报。

周报片段示例：

```markdown
# 账号周报 · demo_xhs · 2026-W45

## 概览

- 账号：demo_xhs（示例账号（虚构））
- 周：2026-W45（2026-11-02 ~ 2026-11-08）
- 发布量：10 篇
- 总 views：53,100

## 指标表

| 指标 | 本周数值 | 口径 |
| --- | --- | --- |
| 发布量 | 10 篇 | 本周发布的作品数 |
| views 中位数 | 5,200 | 本周作品 views 的中位数 |
| views P90（本周） | 6,410 | 本周作品 views 的 P90（线性插值） |
| interaction_rate 均值 | 5.89% | (likes+comments+saves+shares)/views，先按作品算再平均 |

## 下周三条动作

1. 把下周 3 篇内容的发布时段从「晚」移到「午」：午时段 3 篇平均 interaction_rate 9.87%，晚时段 3 篇仅 3.58%，相差 6.29 个百分点。
2. 把下周 3 条视频统一到「≤15s」：该组 3 条平均 completion_rate 86.00%，比本周均值 64.83% 高 21.17 个百分点。
3. 复刻 post_xhs_010 的选题结构：views 11,000，爆款率 1.88（为账号历史 P90 的 1.88 倍），下周产出 1 篇同结构内容。
```

## 7. seed 数据（全部虚构）

| 文件 | 内容 |
| --- | --- |
| `data/seed/accounts.csv` | 3 个虚构账号：`demo_xhs`（自己）与 `demo_xhs_bench_a` / `demo_xhs_bench_b`（对标） |
| `data/seed/posts.csv` | 30 篇虚构作品的 post 字段（与 `metrics_sample.csv` 的公共列一致） |
| `data/seed/metrics_sample.csv` | 30 行自包含导出样例：post 字段 + 指标字段，可直接导入 |

- 账号 handle 全部以 `demo_` 开头，显示名全部带「（虚构）」。
- `accounts.csv` 是账号元数据参考（显示名、是否对标、领域）；实际导入只读
  `metrics_sample.csv`，其中已带 `display_name`，因此账号显示名会一起落库。
- 自己账号在 `2026-W45` 有 10 篇作品，另有 4 篇历史作品用于计算 P90；两个对标账号各 8 篇。
- 数据仅用于离线测试与演示，不是真实平台数据。

验证命令：

```sh
export WB_HOME=/tmp/wb-t02
wb review import --csv data/seed/metrics_sample.csv --account demo_xhs
# imported=30 accounts=3 skipped=0
wb review import --csv data/seed/metrics_sample.csv --account demo_xhs
# imported=0 accounts=3 skipped=30
wb review week --account demo_xhs --week 2026-W45 --out /tmp/rev
sed -n '1,30p' /tmp/rev/report.md
```

## 8. 测试覆盖

`tests/test_review.py` 覆盖：

- ISO 周计算（含 `WB_TZ` 边界与非法周）；
- 手算 fixture 的指标计算（中位数、P90、互动率、收藏率、发布间隔等）；
- 分组 `样本不足` 分支，且动作不引用不足样本；
- 30 行 seed 的导入、第二次导入幂等（`skipped=30`）；
- 缺列 CSV 退出码 2，stderr 含缺失列与别名；
- `wb review import` / `wb review week` 的验收输出与周报五个标题、指标表行数、
  Top5/Bottom5 行数、三条动作的数字与引用；
- `wb run review` 流水线两个 stage 的注册与产物；
- seed 账号名全部虚构（`demo_` 前缀或显示名含「虚构」）。

## 9. 已知限制

- 不抓取数据、不调用平台 API；数据质量完全取决于导入的 CSV。
- 完播率缺失时的 `avg_watch_sec / duration_sec` 只是估算，不等同平台口径。
- `burst_ratio` 的历史 P90 包含本周作品；历史样本很少时爆款率波动大。
- z-score 用总体标准差；本周作品少于 10 篇时极端值更容易触发 `|z| >= 2`。
- 对标账号只做标记与导入（`is_benchmark=1`）；跨账号大盘对标属于 T05。
- 周报是 Markdown 表格，不生成图表图片。
