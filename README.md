# 超级个体获客工作台（Automated Self-Media Operation Assistant）

> 在线站点（GitHub Pages）：<https://promax-mafker.github.io/Automated-Self-Media-Operation-Assistant/> · 含控制台静态快照与样例产物

一句话定位：给一个人（或 1-3 人小团队）用的**本地获客工作台**——把「选题 → 文案 → 图文/视频 →
发布包 → 数据回灌 → 复盘」串成六条可单独调用的流水线，再加一个只读本地数据的 Web 控制台。

- 单机、单用户、SQLite；不需要 Docker、Node、npm 或浏览器构建。
- `WB_LLM=stub` 时全链路离线可跑；接真实模型只改环境变量。
- 发布永远由人完成：本工具只导出发布包与记录，**不登录、不抓取、不自动发布**。

## 5 分钟快速开始

```sh
bash scripts/setup.sh        # 建 .venv 并 pip install -e ".[dev]"（走清华镜像）
wb doctor                    # 环境、数据库版本、stage 覆盖
bash scripts/demo.sh         # 离线跑通全链路，产物在 out/demo-<时间戳>/
WB_HOME=out/demo-<时间戳>/.wb wb serve --port 8077   # 打开 http://127.0.0.1:8077/
bash scripts/verify.sh       # check.sh + demo.sh + wb doctor，任一失败即非 0
```

demo 产物（`out/demo-<时间戳>/`）：`wb.sqlite3`、`packs/`、`artifacts/`（至少 1 张 PNG、
1 个 MP4）、`report.md`、`topics.json`、`copies.json`。全部是 `data/seed` 与 stub 模型
生成的**虚构演示数据**，不代表任何真实账号表现。

## 能力 ↔ 命令

| 能力 | 命令 |
| --- | --- |
| 数据复盘与账号洞察 | `wb review import --csv <导出.csv> --account <handle>`；`wb review week --account <handle> --week 2026-W45` |
| 选题策划与爆款对标 | `wb topic suggest`；`wb topic benchmark --account demo_bench_a`；`wb topic list` |
| 多类型文案生成 | `wb copy gen --topic <topic_id>`；`wb copy lint --file <copies.json>` |
| 图文与海报 | `wb render list`；`wb render poster --template poster-quote --data <json> --out <png>` |
| 短视频 | `wb video template > storyboard.json`；`wb video build --storyboard <json> --out <mp4>` |
| 发布包与回灌 | `wb pack build --copy <copy_set_id>`；`wb pack log --pack <id> --url <url>`；`wb pack backfill --csv <导出.csv>` |
| 知识库 | `wb kb add data/kb/seed`；`wb kb ask "周报 复盘"`；`wb kb eval` |
| 工作台 | `wb serve --port 8077`；`bash scripts/demo.sh` |

## Web 控制台

`wb serve` 起一个本地控制台（默认 `127.0.0.1:8077`，只读本地 store）：

| 页面 | 内容 |
| --- | --- |
| `/` | 概览：本周发布数、播放中位数、Top3 选题、待发布包数、最近一次 job |
| `/topics` | 选题池（分数 / 来源 / 状态 / 证据），可点「生成文案」 |
| `/copies` | 文案列表与合规徽标，点开看全文与引用 |
| `/assets` | 素材网格（缩略图走 `/files/<相对路径>`） |
| `/packs` | 发布包列表（状态 / 平台 / 时间），点开看 manifest |
| `/kb` | 知识库问答，返回命中片段与分数 |

空库时每个页面都会提示「先跑 `wb run demo`」或对应的单条命令。写操作全部是
`POST + 原生表单 + 303`，没有 JS 也能用。页面细节见 [docs/ui.md](docs/ui.md)。

## 目录结构

```text
workbench/
  config.py / ids.py / errors.py / contracts.py   环境、ID、异常、适配器协议
  store/      SQLite 连接、迁移、pydantic 模型与 repo
  pipeline/   stage 注册表、runner、流水线定义
  stages/     各 Ticket 的 stage（自动发现）
  cli.py      根命令；cli_ext/* 自动挂载子命令
  topics/ copy/ render/ video/ publish/ metrics/ knowledge/
  web/        FastAPI + Jinja2 控制台（本 Ticket）
scripts/      setup.sh / check.sh / demo.sh / verify.sh
templates/    poster / carousel / video 声明式模板
data/         seed（虚构 fixture）、kb（知识库样例）、compliance（词表）
docs/         ADR、各能力文档、quickstart.md、ui.md
tests/        pytest（默认禁网；Web 测试只放行事件循环自管道）
out/          demo.sh 的产物（不入库）
```

## 如何换模型

只改环境变量，不改代码（OpenAI 兼容 `/chat/completions`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WB_LLM` | 自动 | `stub` 或 `openai`；未设置时有 key 走 openai，无 key 走 stub |
| `WB_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 任意 OpenAI 兼容端点 |
| `WB_LLM_API_KEY` | 空 | 无 key 时自动 stub |
| `WB_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `WB_HOME` | `./.wb` | 数据根：`wb.sqlite3`、`artifacts/`、`runs/`、`packs/` |
| `WB_TZ` | `Asia/Shanghai` | 时间与 ISO 周 |
| `WB_PLATFORM` | `xhs` | 默认平台 |

```sh
WB_LLM=openai WB_LLM_API_KEY=sk-... WB_LLM_MODEL=deepseek-chat wb copy gen --topic <id>
```

## 边界与合规红线

- **不抓取**：不做爬虫、不登录任何平台；平台数据只来自官方导出文件或手动录入。
- **不自动发布**：只生成发布包（`packs/<pack_id>/` + zip）并记录；发布与回填 URL 由人完成。
- **AI 标识**：发布包 manifest 固定 `ai_generated: true`，并按《人工智能生成合成内容标识办法》
  在发布说明里给出可复制的显式标识话术。
- **不编造**：seed 数据全部标注「（虚构）」或 `demo_` 前缀；测算必须写明「假设」。
- **本地优先**：单机单用户，无多租户、无权限体系、无云依赖。

## 文档

- [docs/quickstart.md](docs/quickstart.md)：安装、demo、单条流水线、排错。
- [docs/ui.md](docs/ui.md)：页面清单、每个页面读哪些表、怎么加一个页面。
- [docs/adr/0001-mvp-architecture.md](docs/adr/0001-mvp-architecture.md)：架构与数据契约（唯一权威）。
- [docs/adr/0002-oss-and-compliance.md](docs/adr/0002-oss-and-compliance.md)：许可证与合规红线。

质量门：`bash scripts/check.sh`（`ruff check .` + `pytest -q`，全部测试禁止联网）。
