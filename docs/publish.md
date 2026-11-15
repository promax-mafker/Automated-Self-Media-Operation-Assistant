# 发布包、发布记录与数据回流（T08）

本文说明 `workbench/publish/`、`wb pack` 与发布包格式。数据模型与适配器契约以
[`docs/adr/0001-mvp-architecture.md`](adr/0001-mvp-architecture.md) §5、§6.4 为准；
AI 标识与合规红线见 [`docs/adr/0002-oss-and-compliance.md`](adr/0002-oss-and-compliance.md) §5.1。

## 1. 能力与边界

- `wb pack build`：把一条 `copy_set` 与若干 `asset` 打成 `<WB_HOME>/packs/<pack_id>/`
  和同名 zip。
- `wb pack list` / `wb pack show`：查看包、状态、manifest 与已记录的发布 URL。
- `wb pack log`：人工发布后记录平台 URL，状态 `draft → exported → published`。
- `wb pack backfill`：把平台导出的 CSV 按作品 URL 回灌成 `post` + `metric`，
  供 T02 周报复盘。
- **不做**登录平台、自动发布、调平台 API、定时发布、多账号授权管理。发布永远由人完成，
  本包只负责「导出 + 记录 + 回灌」。

## 2. 包结构

```text
<WB_HOME>/packs/<pack_id>/
  文案.md          标题 + 正文 + 话题标签（平台格式见 §5）
  图片/01.png...   与 copy_set 关联的图片；按传入顺序编号
  视频/video.mp4   若有视频 asset
  manifest.json    机器可读的发布包元数据（见 §3）
  发布说明.md       给人看的 AI 声明、合规结果、建议发布时间与平台注意事项
<WB_HOME>/packs/<pack_id>.zip
```

- `wb pack build --out <dir>` 可以改包目录；zip 仍写在目录同级（`<dir>.zip`）。
  目录必须不存在或为空，避免覆盖已有文件。
- `--assets id,id` 省略时自动取该 `copy_set` 关联的全部 `asset`；没有 asset 时生成
  纯文案包并打印 warning。
- `manifest.assets[].path` 是**包内相对路径**（如 `图片/01.png`），另带
  `source_path` 指回 `asset.path`；这样解压后可直接按 manifest 找到文件。
- `checksums` 覆盖包内除 `manifest.json` 自身外的全部文件。manifest 无法包含自己的
  SHA-256（写入后哈希会变），这是唯一的例外；`wb pack show --json` 或测试可用
  `verify_checksums()` 校验。

## 3. manifest 字段表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `pack_id` | string | `pack_<12hex>` |
| `platform` | string | `xhs` / `douyin` / `shipinhao` / `wechat` |
| `copy_set_id` | string | 来源文案 ID |
| `title` | string | 标题（取 `copy_set.title`，缺省用 `hook`） |
| `body` | string | 正文 |
| `tags` | string[] | 话题标签，不带 `#` |
| `assets` | object[] | `{id, path, width, height, duration_sec, source_path, kind}`；`path` 为包内相对路径 |
| `ai_generated` | bool | 固定为 `true` |
| `compliance` | object | `{passed: bool, hits: [{word, kind, index}]}` |
| `suggested_publish_at` | string | ISO 8601 带时区 |
| `suggested_publish_reason` | string | 建议依据；无数据时以「无数据，按默认」开头 |
| `suggested_publish_default` | bool | 是否使用了默认时间 |
| `checksums` | object | `{包内相对路径: sha256}`，不含 `manifest.json` |
| `created_at` | string | 打包时间，ISO 8601 带时区 |
| `published_url` | string | 仅 `wb pack log` 后出现，最新一条 |
| `published_at` | string | 仅 `wb pack log` 后出现，最新一条 |

`manifest` 同时写入 `pack.manifest` JSON 列；`pack.status` 由 `wb pack build` 置为
`exported`，由 `wb pack log` 置为 `published`。同一 pack 重复 `log` 只覆盖
`published_url` / `published_at`，不会新增 `pack` 行。

## 4. 建议发布时间

`wb pack build` 会读当前 DB 里同平台的 `post` + 最新一条 `metric`：

1. 每篇作品取 `captured_at` 最新的一次快照；
2. `interaction_rate = (likes + comments + saves + shares) / views`；
3. 先按 `(星期, 时段)` 分组，取样本量 ≥ 3 且均值最高的组；
4. 同星期样本不足时退到「时段」；仍不足则用默认「次日 19:00」；
5. 建议时间落在对应时段的代表小时：早 09:00 / 午 13:00 / 晚 19:00 / 深夜 02:00。

样本门槛沿用 T02 的 `MIN_GROUP_SAMPLE = 3`，避免用一两篇作品得出「最佳时段」。
无可用指标时，`发布说明.md` 会明确写「无数据，按默认」，不会假装是数据结论。

## 5. 平台文案格式

`文案.md` 由三部分组成：标题、正文、话题标签。当前实现：

- 小红书：首行是标题（不带 Markdown `#`），空行后正文，末尾是 `#标签` 行；图片按
  `01.png`、`02.png` 排序，建议 3:4（1080×1440）、1–9 张。
- 抖音：首行是标题，正文按口播稿录制，末尾是 `#标签` 行；视频在 `视频/video.mp4`，
  建议 1080×1920 竖版。
- 视频号 / 公众号：同样保留标题、正文、标签三部分；具体字段按平台发布页填写。

`发布说明.md` 还会给出可复制到平台声明处的 AI 标识话术，并列出
`compliance.passed` 的实际值和命中项。`compliance.passed=false` 不阻断打包，但会在
CLI stderr 与说明里高亮，要求人工复核后再发布。

## 6. 命令行

```sh
# 1. 构建：--assets 可省略（自动取 copy 关联的 asset）
wb pack build --copy copy_xxx --platform xhs
wb pack build --copy copy_xxx --platform xhs --assets asset_a,asset_b
wb pack build --copy copy_xxx --platform douyin --out /tmp/my-pack

# 2. 查看
wb pack list
wb pack list --status published
wb pack show --pack pack_xxx
wb pack show --pack pack_xxx --json

# 3. 人工发布后记录 URL（--at 可省略，默认当前时间）
wb pack log --pack pack_xxx --url https://example.com/p/1
wb pack log --pack pack_xxx --url https://example.com/p/2 --at 2026-11-15T21:00:00+08:00

# 4. 数据回流
wb pack backfill --csv tests/fixtures/export_sample.csv
wb pack backfill --csv /path/export.csv --account demo_xhs --platform xhs

# 5. 走 pack 流水线（stage: pack_build）
wb run pack --input copy=copy_xxx --input platform=xhs
```

## 7. 数据回流（backfill）

与 `wb review import`（T02）的分工：

- `wb review import` 按 CSV 的 `post_id` 匹配，适合完整作品库导入；
- `wb pack backfill` 按作品 `url` 匹配，适合「发布包 → 人工发布 → 平台导出」的增量回流。

规则：

1. URL 已存在：只补一条 `metric`，不修改 `post`。
2. URL 未命中：新建 `account` + `post` + `metric`；账号取自 CSV 的账号列，缺列时用
   `--account`。已有账号按 `(platform, handle)` 大小写不敏感复用。CSV 有 `post_id` 列时
   新建 post 直接使用它（便于后续 T02 按 `post_id` 导入），没有时生成 `post_<12hex>`。
3. 幂等键是 `(post_id, captured_at)`；`captured_at` 取 CSV 的抓取时间列，缺省用今天
   00:00（本地时区）。重复行计入 `skipped`，不重复插入。
4. CSV 至少要有 `url` 列；新建 post 时还需要 `title`、`published_at`、`content_type`。
   平台导出列名支持中文别名（与 T02 共用同一套 `ALIASES`）。
5. 先完整解析并校验，再写库；解析失败不会留下半截导入。

输出 `matched=<命中 URL 的行数> created=<新建 post 数> metrics=<实际插入的 metric 数>`。
验收样例需要先让 `post_xhs_001` / `post_xhs_002` 已存在（例如先跑
`wb review import --csv data/seed/metrics_sample.csv --account demo_xhs`）：

```sh
wb review import --csv data/seed/metrics_sample.csv --account demo_xhs
wb pack backfill --csv tests/fixtures/export_sample.csv   # matched=2 created=1 metrics=3
wb pack backfill --csv tests/fixtures/export_sample.csv   # matched=3 created=0 metrics=0
```

## 8. 对接 social-auto-upload 而不违反 R2.6

`AGENTS.md` R2.6 禁止登录平台、发布、调用需要凭据的第三方服务。因此本 Ticket 只保留
`SocialAutoUploadPublisher` 的**函数签名**：

```python
class SocialAutoUploadPublisher:
    def publish(self, *, pack_id: str, platform: str, bundle_dir: Path,
                url: str | None = None) -> NoReturn:
        raise NotImplementedError(
            "本项目不自动发布，请人工发布后用 wb pack log 记录"
        )
```

将来若要接 `social-auto-upload`，必须另开 Ticket 并至少回答 ADR 0002 §4 的三问：
数据出不出本机、许可证是否允许、失败时如何降级。接入时也应满足：

- 默认 `dry-run`，只有显式环境变量才允许真实发布；
- 只在本机、只在自己账号、不代理他人账号；
- 仍保留 `wb pack log` 作为人工发布与自动发布共同的记录入口；
- 不把平台 Cookie / 凭据写进 `manifest.json` 或发布包目录。

## 9. 验收与测试

```sh
bash scripts/check.sh
pytest -q tests/test_publish.py
```

重点断言：

- 包目录结构齐全，`manifest.json` 可 `json.load`，`checksums` 覆盖除自身外的全部文件且
  校验一致；
- `发布说明.md` 含 AI 声明、`compliance.passed` 实际值、建议发布时间及依据；
- 重复 `wb pack log` 只保留最新 URL，`pack` 表仍 1 行；
- backfill 命中 / 未命中 / 重复三条分支与幂等；
- `Publisher` 自动发布入口调用即抛 `NotImplementedError`，消息固定为
  「本项目不自动发布，请人工发布后用 wb pack log 记录」。
