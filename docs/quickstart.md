# 快速开始（T09）

本文按「装好 → 跑 demo → 打开控制台 → 单条流水线 → 排错」的顺序写，全部离线可跑。
架构与数据契约以 [ADR 0001](adr/0001-mvp-architecture.md) 为准，本文只讲操作。

## 1. 前置条件

- Python 3.10+（`python3 --version`）。
- 不需要 Docker、Node、npm、浏览器自动化或系统字体；`imageio-ffmpeg` 自带 ffmpeg 二进制。
- 数据与产物都在本地；没有云服务、没有平台凭据。

## 2. 安装

```sh
bash scripts/setup.sh
```

`setup.sh` 优先创建仓库内 `.venv` 并用清华镜像 `pip install -e ".[dev]"`；venv 不可用时
退回 `pip --user`。安装完成后：

```sh
source .venv/bin/activate   # 或确认 ~/.local/bin 在 PATH
wb doctor
```

`wb doctor` 应输出 `user_version`、`table_count`，以及每条流水线的 stage 覆盖
（`ok` / `missing`）。`missing` 只表示对应 Ticket 未实现，不是安装失败。

## 3. 五分钟看到效果

```sh
bash scripts/demo.sh
```

它用 `WB_HOME=out/demo-<时间戳>/.wb` 隔离数据，`WB_LLM=stub` 保证离线，依次跑：

1. `wb db init` + `wb kb add data/kb/seed`：建库、导入知识库样例。
2. `wb topic suggest --count 10`：本地热榜 / RSS / 对标 fixture → 10 条带证据的选题卡。
3. `wb copy gen --topic <id>`：5 类 × 2 长度 = 10 条文案（stub 为确定性占位文案）。
4. `wb run visual --input template=poster-checklist`：Pillow 渲染海报 PNG。
5. `wb run video --input storyboard=...`：ffmpeg 合成竖版 MP4。
6. `wb pack build --copy <id>`：导出发布包（**不发布**）。
7. `wb review import` + `wb review week`：导入虚构指标 CSV，生成周报 `report.md`。
8. 汇总产物并打印清单；缺任何一个必有产物都会以非 0 退出。

产物目录：

```text
out/demo-<时间戳>/
  wb.sqlite3          demo 数据库快照
  topics.json         选题卡
  copies.json         文案（含 evidence / compliance）
  report.md           周报
  packs/<pack_id>/    发布包目录 + 同级 zip
  artifacts/          海报 PNG、视频 MP4、各 run 的 manifest、周报副本
  .wb/                demo 实际使用的 WB_HOME
  storyboard.json / poster-data.json / metrics-demo.csv   本次演示输入
```

数据全部是 `data/seed` 的虚构 fixture，日期会平移到本周，方便看到「本周」指标；
不代表任何真实账号表现。

只检查已有目录的产物是否齐全（不跑流水线）：

```sh
bash scripts/demo.sh --list out/demo-<时间戳>
```

## 4. 打开控制台

```sh
WB_HOME=out/demo-<时间戳>/.wb wb serve --port 8077
# 浏览器打开 http://127.0.0.1:8077/
```

六个页面：概览 `/`、选题池 `/topics`、文案 `/copies`、素材 `/assets`、发布包 `/packs`、
知识库问答 `/kb`。空库启动时每个页面都会提示「先跑 `wb run demo`」。

无浏览器时的冒烟检查：

```sh
curl -s localhost:8077/ | head -n 5
curl -s -o /dev/null -w '%{http_code}\n' localhost:8077/topics
```

`wb serve` 默认只绑定 `127.0.0.1`，不要直接暴露到公网；本版没有登录与权限。

## 5. 单条流水线

```sh
# 选题 → 拿到 topic_id
wb topic suggest --count 5
wb topic list --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'

# 文案 → 拿到 copy_set_id
wb copy gen --topic <topic_id>
COPY_FILE=$(ls -t .wb/runs/*/copies.json | head -1)
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["copies"][0]["id"])' "$COPY_FILE"

# 海报 / 视频 / 发布包
wb run visual --input template=poster-quote --input copy_set_id=<copy_id>
wb video template > /tmp/storyboard.json
wb run video --input storyboard=/tmp/storyboard.json --input copy_set_id=<copy_id>
wb pack build --copy <copy_id>

# 回灌与周报
wb review import --csv data/seed/metrics_sample.csv --account demo_xhs
wb review week --account demo_xhs --week 2026-W45
```

`wb <子命令> --help` 都有完整参数说明；`wb doctor` 会列出每条流水线的 stage 覆盖。

## 6. 换模型

```sh
WB_LLM=openai WB_LLM_BASE_URL=https://api.deepseek.com/v1 \
  WB_LLM_API_KEY=sk-... WB_LLM_MODEL=deepseek-chat wb copy gen --topic <id>
```

未设置 `WB_LLM` 时：有 key 走 `openai`，无 key 自动 `stub`。stub 只保证**结构合法**，
正文是占位文案（`【stub】...`），不要直接拿去发布。

## 7. 质量门与排错

```sh
bash scripts/check.sh    # ruff check . + pytest -q（全部测试禁止联网）
bash scripts/verify.sh   # check.sh + demo.sh + wb doctor
```

| 现象 | 处理 |
| --- | --- |
| `FTS5` 迁移失败 | 换用带 FTS5 的 Python/SQLite；`wb kb` 依赖 FTS5 |
| `找不到 ruff/pytest` | 先跑 `bash scripts/setup.sh`，或激活 `.venv` |
| 字体缺失/渲染乱码 | 只用仓库内 `assets/fonts/`（`WB_FONT_DIR` 可覆盖），不要依赖系统字体 |
| 视频合成失败 | 检查 `imageio-ffmpeg` 是否安装成功；无系统 ffmpeg 时也走自带二进制 |
| `WB_LLM=bogus` | `ConfigError`，命令退出码非 0；demo/verify 也会失败（预期） |
| `/files/...` 返回 404 | 只允许 `WB_HOME` 下相对路径；`..`、绝对路径、软链越界都拒绝 |
| 端口被占用 | `wb serve --port 8078` |

## 8. 边界

不爬取、不登录、不自动发布；平台数据靠导出 CSV 或手动录入；发布包导出后由人发布并回填 URL。
详见 [README](../README.md) 与 [ADR 0002](adr/0002-oss-and-compliance.md)。
