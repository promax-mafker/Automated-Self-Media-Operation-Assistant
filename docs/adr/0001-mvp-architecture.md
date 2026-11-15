# ADR 0001 · MVP 架构与数据契约

- 状态：已接受（2026-11-15）
- 决策者：人类（本仓库 owner）
- 影响范围：全部 Ticket

## 0. 一句话

**用一条薄的 Python 内核（stage 注册表 + SQLite + 适配器协议）把「选题 → 文案 → 图文/视频 → 发布包 → 数据回灌 → 复盘」串成六条可单独调用的流水线；外部能力（模型、信源、知识库、渲染、视频、发布）全部藏在协议后面，`WB_LLM=stub` 时整条链路离线可跑。**

## 1. 目标与非目标

### 目标（MVP 必须做到）

1. 一个人在自己机器上 10 分钟内跑通全链路，不需要 Docker、不需要 Node、不需要浏览器。
2. 六条流水线各自可独立调用，也可串起来；每步产物落盘、落库、可复算。
3. 中文内容的生成质量靠「模板 + 知识库取证 + 合规闸门」，而不是靠堆 prompt。
4. 换模型、换信源、换渲染方式只改适配器，不改流水线。

### 非目标（本版坚决不做）

1. 不做多租户、不做计费、不做权限体系。
2. 不做自动发布、不做自动登录、不做任何平台的爬取。
3. 不做爆款数据的自动抓取（数据来源是平台导出文件 + 手动录入）。
4. 不做数字人、不做 AI 配音克隆、不做自动剪辑成片。
5. 不做向量数据库、不做 RAG 平台（用 SQLite FTS5 + jieba 分词就够）。

## 2. 七项能力 → 六条流水线

| 能力（用户语言） | 流水线 | 主要产出（落库/落盘） |
| --- | --- | --- |
| 数据复盘与账号洞察 | `review` | `metric` + `post` 汇总、周报 Markdown |
| 选题策划与爆款对标 | `topic` | `topic` 选题卡（含对标证据） |
| 多类型文案生成 | `copy` | `copy_set`（5 类 × 2 长度 = 10 条） |
| 视频与图文自动化生成 | `visual` / `video` | `asset`（PNG 图文、MP4 短视频） |
| 营销海报生成 | `visual` | `asset`（海报，1080×1440 等） |
| 知识库 | 被 `copy` / `topic` 调用 | `kb_doc` + `kb_chunk` + 引用 |
| 工作台（把上面串起来） | `pack` + Web 控制台 | `pack` 发布包、单页控制台 |

```
信源/热榜 ─┐
对标库  ───┼→ topic ──→ copy ──┬→ visual ─┐
知识库  ───┘                   └→ video  ─┼→ pack ──→ 人工发布 ──→ 平台导出 CSV ──→ review
                                          └──────────────────────────────────────────────┘
```

闭环的关键：`review` 的产出（哪些类型/时段/时长表现好）反向喂给 `topic` 打分，所以指标口径必须在同一个 schema 里。

## 3. 技术选型

| 层 | 选择 | 理由 / 被否方案 |
| --- | --- | --- |
| 语言 | Python 3.10+ | 目标机器现成；LLM/图像/视频生态最全 |
| 存储 | SQLite（WAL）+ JSON 列 | 单机零运维；数据量级（万级行）远不到需要 PG |
| 检索 | SQLite FTS5 + jieba 分词 | 中文不需要向量也能用；FTS5 的 trigram 分词器对 2 字词失效，故先 jieba 分词再入 unicode61 索引 |
| Web | FastAPI + Jinja2 + HTMX（`vendor/htmx.min.js` 本地文件） | 无构建步骤、无 node_modules；HTMX 够用 |
| 命令行 | Typer + Rich | 一条 `wb` 命令覆盖所有能力，便于 worker 验收 |
| 模型 | OpenAI 兼容 `/chat/completions`（默认 DeepSeek） | 换模型只改 base_url/model；不绑定任何厂商 SDK |
| 图文/海报 | Pillow + 声明式模板（JSON 版式）+ 内置 Noto Sans SC | 不依赖浏览器/系统字体，确定性可测；HTML→PNG（Playwright）留作后续 Renderer 实现 |
| 视频 | ffmpeg（`imageio-ffmpeg` 自带二进制）+ Pillow 生成帧 | 不装系统包；60 秒竖版卡片视频足够 MVP |
| 配音 | `edge-tts`（可选，需网络） | 无网络时降级为静音 + 字幕条 |
| 发布 | 只生成发布包 + 记录；接口留 `social-auto-upload` 适配位 | 自动发布有封号与合规风险，MVP 不做 |

### 为什么不用调研里的开源组合直接拼

调研（`开源方案-AIHOT与开源项目命中清单.md`）给的拼法是 AiToEarn + AIHOT + RAGFlow + poster-design + Superset，能跑，但：

- **AiToEarn** 的 OAuth 默认走官方 Relay，且官方明确不接受外部 PR，改造与数据流都不受控；
- **Coze Studio / Dify(RAGFlow)** 是平台，部署与加固成本高于本项目全部业务代码，MVP 阶段是负资产；
- **poster-design 是 AGPL**，做成对外服务有传染风险；
- **MediaCrawler 明确非商用**，而「爆款对标」恰恰最想用它。

所以 MVP 自建薄内核，把上述项目当作**将来某个适配器的实现**（发布接 social-auto-upload，渲染接 Playwright/HTML，数据接平台官方导出）。这样许可证与运维风险都留在边界外。

## 4. 目录结构与运行方式

```
pyproject.toml            setuptools 后端，console script: wb
scripts/setup.sh          pip install -e .（走清华镜像，见 §10）
scripts/check.sh          ruff check . && pytest -q
assets/fonts/             NotoSansSC-Regular.otf / -Bold.otf（SIL OFL，随仓库分发）
workbench/
  config.py               Env 读取 + 路径解析（WB_HOME / artifacts / db）
  ids.py                  短 ID（`<prefix>_<12hex>`）
  errors.py               WorkbenchError / Unavailable / MissingStage / PipelineFailed
  contracts.py            适配器协议 + 数据类 + Null 实现 + 懒加载工厂
  store/db.py             连接、PRAGMA、迁移（§5 的 DDL）、事务
  store/models.py         pydantic 模型（与表一一对应）
  store/repo.py           每张表的 insert/get/list/update
  llm/                    base.py / stub.py / openai.py / factory.py
  pipeline/               stage.py / registry.py / runner.py / definitions.py
  stages/__init__.py      pkgutil 自动 import 本包下所有模块
  stages/<feature>.py     各 Ticket 的 stage
  <feature>/              业务实现（metrics/knowledge/render/topics/copy/video/publish/web）
  web/                    控制台（Jinja2 模板 + HTMX）
  cli.py                  根命令 + 自动挂载 cli_ext/*
  cli_ext/<feature>.py    各 Ticket 的子命令
templates/                海报/图文/分镜模板
prompts/                  文案 prompt（带版本号）
data/seed/                fixture（虚构数据）
tests/                    pytest（conftest 里禁止联网）
```

进程模型：单进程 CLI 或 `wb serve` 起的单进程 Web；同一进程内共享 SQLite 连接池（每请求一个连接，WAL 保证读写不互斥）。没有队列、没有 worker 进程——流水线在请求内同步跑，长任务在 CLI 里跑。

## 5. 数据模型（唯一权威，T01 按此实现迁移）

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS account (
  id           TEXT PRIMARY KEY,
  platform     TEXT NOT NULL,                       -- xhs | douyin | shipinhao | wechat
  handle       TEXT NOT NULL,
  display_name TEXT,
  is_benchmark INTEGER NOT NULL DEFAULT 0,
  niche        TEXT,
  note         TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (platform, handle)
);

CREATE TABLE IF NOT EXISTS post (
  id           TEXT PRIMARY KEY,
  account_id   TEXT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  platform     TEXT NOT NULL,
  title        TEXT NOT NULL,
  url          TEXT,
  published_at TEXT NOT NULL,                       -- ISO 8601 +08:00
  content_type TEXT NOT NULL,                       -- image_text | video | article
  topic_tags   TEXT NOT NULL DEFAULT '[]',          -- JSON string[]
  duration_sec REAL,
  caption      TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS post_account_time ON post (account_id, published_at);

CREATE TABLE IF NOT EXISTS metric (
  id              TEXT PRIMARY KEY,
  post_id         TEXT NOT NULL REFERENCES post(id) ON DELETE CASCADE,
  captured_at     TEXT NOT NULL,
  views           INTEGER,
  likes           INTEGER,
  comments        INTEGER,
  shares          INTEGER,
  saves           INTEGER,
  follows         INTEGER,
  completion_rate REAL,                             -- 0..1
  avg_watch_sec   REAL,
  raw             TEXT,
  UNIQUE (post_id, captured_at)
);

CREATE TABLE IF NOT EXISTS topic (
  id          TEXT PRIMARY KEY,
  platform    TEXT NOT NULL,
  title       TEXT NOT NULL,
  angle       TEXT,
  hook        TEXT,
  score       REAL NOT NULL DEFAULT 0,
  score_parts TEXT NOT NULL DEFAULT '{}',           -- JSON {heat,fit,benchmark,timing}
  source      TEXT NOT NULL,                        -- hotlist | rss | benchmark | manual
  source_ref  TEXT,
  evidence    TEXT NOT NULL DEFAULT '[]',           -- JSON [{kind,ref,note}]
  status      TEXT NOT NULL DEFAULT 'new',          -- new | picked | used | dropped
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_score ON topic (platform, score DESC);

CREATE TABLE IF NOT EXISTS copy_set (
  id             TEXT PRIMARY KEY,
  topic_id       TEXT REFERENCES topic(id) ON DELETE SET NULL,
  kind           TEXT NOT NULL,                     -- spoken | note | seed | explain | hook
  length         TEXT NOT NULL,                     -- short | long
  title          TEXT,
  hook           TEXT,
  body           TEXT NOT NULL,
  tags           TEXT NOT NULL DEFAULT '[]',        -- JSON string[]
  citations      TEXT NOT NULL DEFAULT '[]',        -- JSON [{doc_id,chunk_id,quote}]
  model          TEXT,
  prompt_version TEXT,
  compliance     TEXT NOT NULL DEFAULT '{}',        -- JSON {passed,hits:[{word,kind,index}]}
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS copy_set_topic ON copy_set (topic_id, kind, length);

CREATE TABLE IF NOT EXISTS asset (
  id           TEXT PRIMARY KEY,
  copy_set_id  TEXT REFERENCES copy_set(id) ON DELETE SET NULL,
  kind         TEXT NOT NULL,                       -- poster | carousel | cover | video
  template     TEXT,
  path         TEXT NOT NULL,                       -- 相对 WB_HOME
  width        INTEGER,
  height       INTEGER,
  duration_sec REAL,
  bytes        INTEGER,
  meta         TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pack (
  id          TEXT PRIMARY KEY,
  copy_set_id TEXT NOT NULL REFERENCES copy_set(id) ON DELETE CASCADE,
  platform    TEXT NOT NULL,
  dir         TEXT NOT NULL,                        -- 相对 WB_HOME
  manifest    TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'draft',        -- draft | exported | published
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job (
  id         TEXT PRIMARY KEY,
  pipeline   TEXT NOT NULL,
  stage      TEXT NOT NULL,
  input_ref  TEXT,
  status     TEXT NOT NULL,                         -- pending | running | done | failed | skipped
  started_at TEXT,
  ended_at   TEXT,
  error      TEXT,
  output_ref TEXT
);
CREATE INDEX IF NOT EXISTS job_pipeline ON job (pipeline, started_at);

CREATE TABLE IF NOT EXISTS kb_doc (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  kind        TEXT NOT NULL,                        -- note | product | case | faq | other
  source_path TEXT,
  sha256      TEXT,
  updated_at  TEXT NOT NULL,
  meta        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kb_chunk (
  id      TEXT PRIMARY KEY,
  doc_id  TEXT NOT NULL REFERENCES kb_doc(id) ON DELETE CASCADE,
  ord     INTEGER NOT NULL,
  heading TEXT,
  body    TEXT NOT NULL
);

-- 检索表：seg 是 jieba 分词后用空格连接的中文词串
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5 (chunk_id UNINDEXED, seg);
```

补充约定：

- 所有布尔用 `INTEGER 0/1`；比率用 `REAL`；JSON 列由 `store/models.py` 负责 `json.loads`。
- 迁移用 `store/db.py` 里的有序迁移列表（`MIGRATIONS: list[tuple[str, str]]`），按 `user_version` 推进，幂等。
- 不允许在别处 `CREATE TABLE`。

## 6. 契约

### 6.1 Stage 与 RunContext

```python
# workbench/pipeline/stage.py
@dataclass(slots=True)
class RunContext:
    run_id: str
    pipeline: str
    home: Path                 # WB_HOME
    out: Path                  # WB_HOME/runs/<run_id>
    db: sqlite3.Connection
    llm: LLMClient
    cfg: Config
    params: dict[str, str]     # wb run <pipeline> --input k=v
    log: Callable[[str], None]

class Stage(Protocol):
    name: str
    needs: tuple[str, ...]     # 必需 params 键；缺失时 runner 抛 MissingParam
    def run(self, ctx: RunContext) -> dict[str, Any]: ...   # 返回值写进 job.output_ref（JSON）
```

- 注册：`@register("copy_generate")`（`workbench/pipeline/registry.py`）。
- 自动发现：`workbench/stages/__init__.py` 用 `pkgutil.iter_modules` import 全部模块，新增 stage 文件不需要改任何共享文件。
- runner：逐 stage 执行，每个 stage 写一行 `job`；异常 → `job.status='failed'` + 抛 `PipelineFailed`。支持 `--stop-after <stage>`。

### 6.2 流水线定义（`workbench/pipeline/definitions.py`，T01 固定）

```python
PIPELINES: dict[str, list[str]] = {
  "review": ["metrics_import", "review_report"],       # T02
  "topic":  ["topic_source", "topic_score"],           # T05
  "copy":   ["copy_generate", "copy_lint"],            # T06
  "visual": ["render_assets"],                         # T04
  "video":  ["video_build"],                           # T07
  "pack":   ["pack_build"],                            # T08
  "demo":   ["demo_source", "demo_topic", "demo_copy", "demo_asset", "demo_report"],  # T01
}
```

`wb doctor` 必须能列出每条流水线的 stage 覆盖情况；未实现的标 `missing` 而不是报错。

### 6.3 LLM

```python
class LLMClient(Protocol):
    model: str
    def text(self, messages: Sequence[Message], *, temperature: float = 0.3, max_tokens: int = 1200) -> str: ...
    def json(self, messages: Sequence[Message], schema: type[T], *, temperature: float = 0.2, max_tokens: int = 2000) -> T: ...
```

- `StubLLM`：`json()` 按 pydantic schema 递归填确定性占位值（字符串 `"【stub】<字段名>"`，列表给 1 个元素，数字给 0/1），`text()` 回显最后一条 user 消息的前 80 字。**必须对任意 schema 成立**，否则下游 ticket 无法离线测试。
- `OpenAICompatLLM`：`httpx` 调 `POST {base_url}/chat/completions`，`response_format={"type":"json_object"}`；解析失败重试 1 次，仍失败抛 `LLMError`（带原始响应前 500 字）。
- 选择：`WB_LLM=stub|openai`；未设置时有 key 走 openai，无 key 走 stub 并在 `wb doctor` 里提示。

### 6.4 其余适配器（`workbench/contracts.py`，T01 定义，各 Ticket 实现）

```python
class SourceAdapter(Protocol):        # T05：热榜 / RSS / 对标库
    name: str
    def fetch(self, *, since: str | None = None) -> list[RawItem]: ...

class KB(Protocol):                    # T03
    def search(self, query: str, *, k: int = 5) -> list[KBHit]: ...
    def add(self, paths: Sequence[Path]) -> dict[str, int]: ...

class Renderer(Protocol):              # T04
    def render(self, spec: RenderSpec) -> RenderResult: ...

class VideoComposer(Protocol):         # T07
    def compose(self, *, storyboard: dict[str, Any], out: Path) -> dict[str, Any]: ...

class Publisher(Protocol):             # T08（只打包，不登录）
    def build(self, *, pack: dict[str, Any], out: Path) -> Path: ...
```

懒加载约定：调用方用 `get_kb(cfg)` / `get_renderer()` / `get_composer()`；实现模块缺失时返回 Null 实现（`search→[]`、`add→{}`、其余抛 `Unavailable`），日志一条 warning，**不允许崩**。实现模块固定路径：`workbench.knowledge.build`、`workbench.render.build`、`workbench.video.build`、`workbench.publish.build`、`workbench.topics.sources.build_adapters`。

工厂函数名固定为 `build_kb(cfg)` / `build_renderer()` / `build_composer()` / `build_publisher()` / `build_adapters(cfg)`，下游 Ticket 不得改名；要改契约由人类改这一节。

数据类字段（以 `workbench/contracts.py` 为准）：

| 数据类 | 字段 |
| --- | --- |
| `RawItem` | `source, title, url, published_at, summary, platform, heat, tags` |
| `KBHit` | `doc_id, chunk_id, title, body, heading, score` |
| `RenderSpec` | `kind, out, template, width, height, data` |
| `RenderResult` | `path, width, height, bytes, meta` |

### 6.5 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WB_HOME` | `./.wb` | 数据根：`wb.sqlite3`、`artifacts/`、`runs/`、`packs/` |
| `WB_LLM` | 自动 | `stub` / `openai` |
| `WB_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 任意 OpenAI 兼容端点 |
| `WB_LLM_API_KEY` | 空 | 无 key 时自动 stub |
| `WB_LLM_MODEL` | `deepseek-chat` | |
| `WB_TZ` | `Asia/Shanghai` | |
| `WB_PLATFORM` | `xhs` | 默认平台 |
| `WB_FONT_DIR` | `assets/fonts` | 渲染字体 |

## 7. 质量门

- `bash scripts/check.sh`：`ruff check .` + `pytest -q`，必须全绿。
- 测试禁止联网：`tests/conftest.py` 里用 autouse fixture 把 `socket.socket` 换成抛异常的实现（局部放行需显式 `allow_network` fixture，当前无人使用）。
- 每个 PR 至少 1 个正向 + 1 个边界/失败测试。
- 产物测试断结构（尺寸、时长、字段、命中数、行数），不断哈希、不断模型原文。

## 8. 已预置依赖（不得新增）

运行：`fastapi`、`uvicorn`、`jinja2`、`typer`、`rich`、`httpx`、`pydantic>=2`、`pillow`、`jieba`、`feedparser`、`beautifulsoup4`、`imageio-ffmpeg`、`edge-tts`
开发：`pytest`、`ruff`

`pyproject.toml` 里已声明全部；缺依赖意味着 Ticket 设计有问题，走 `AGENTS.md` R6-E2 升级。

## 9. 已知限制（写进 README，不藏）

1. 平台数据靠导出/手填，不做抓取；「爆款对标」的证据强度取决于导入数据的质量。
2. 海报是声明式版式 + Pillow 绘制，复杂设计（图层混合、任意排版）需要后续 Renderer。
3. 视频是卡片式分镜，不做数字人与自动剪辑。
4. 发布是半自动：导出包 + 人工发布 + 手动/CSV 回填。
5. 单机单用户，无并发写保护之外的隔离。

## 10. 环境注意（WSL 实测）

- PyPI 直连在本机代理下会 `SSL: UNEXPECTED_EOF`，**必须**用镜像：`pip install -i https://pypi.tuna.tsinghua.edu.cn/simple`。已写进 `scripts/setup.sh`。
- `python3.10-venv` 可能缺失；`scripts/setup.sh` 优先用 venv，失败则退回 system pip（并打印提示）。
- 无 `fc-list`/CJK 字体：**不要**依赖系统字体，只用 `assets/fonts/`。
- 无系统 `ffmpeg`：用 `imageio-ffmpeg` 自带的二进制（`imageio_ffmpeg.get_ffmpeg_exe()`）。
