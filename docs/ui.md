# Web 控制台（T09）

`workbench/web/` 是一个**服务端渲染**的本地控制台：FastAPI + Jinja2 + 一份手写 CSS。
没有前端构建、没有 Node、没有 Tailwind、没有 JS 也能完成全部读写。

## 1. 设计约束

- **只读业务数据**：页面只调 `store.repo`（读）与 `pipeline.runner`（写流水线），不复制业务逻辑。
- **写操作**：`POST` + 原生表单 + `303` 重定向（PRG），刷新不会重复提交。
- **单进程单用户**：不登录、不鉴权、不做多用户；`wb serve` 默认只绑 `127.0.0.1`。
- **无实时进度**：长任务在终端跑；页面只读结果。不做 WebSocket、不做后台队列。
- **文件访问**：`GET /files/{path}` 只接受 `WB_HOME` 下的相对路径；`..`、绝对路径、软链越界
  一律 404。
- **数据源**：控制台与 CLI 共用同一个 `cfg`（`WB_HOME` / `WB_TZ` / `WB_PLATFORM`）。

## 2. 页面清单

| 路由 | 模板 | 读哪些表 | 空态引导 | 写操作 |
| --- | --- | --- | --- | --- |
| `GET /` | `index.html` | `account`、`post`、`metric`、`topic`、`pack`、`job` | 先跑 `wb run demo` | — |
| `GET /topics` | `topics.html` | `topic`、`copy_set`（计数） | 先跑 `wb run demo` / `wb topic suggest` | — |
| `POST /topics/{id}/copy` | — | 写 `copy_set` / `job`（copy 流水线） | — | 生成 10 条文案 → `303 /copies` |
| `GET /copies` | `copies.html` | `copy_set`、`topic`（标题） | 先跑 `wb run demo` / `wb copy gen` | — |
| `GET /assets` | `assets.html` | `asset` | 先跑 `wb run demo` / `wb run visual` | — |
| `GET /packs` | `packs.html` | `pack` | 先跑 `wb run demo` / `wb pack build` | — |
| `GET /kb` | `kb.html` | `kb_doc`、`kb_chunk`、`kb_fts` | 先跑 `wb run demo` / `wb kb add` | — |
| `POST /kb/ask` | `kb.html` | 同上（FTS5 检索） | — | 只读检索，直接渲染结果 |
| `GET /files/{path}` | — | 文件系统（`WB_HOME` 内） | — | — |

补充：

- `/copies?copy_id=<id>` 展开正文、标签、引用与合规命中。
- `/packs?pack_id=<id>` 展开 manifest JSON、AI 标识与已发布 URL。
- 首页「本周」按 `WB_TZ` 的 ISO 周计算；只统计 `is_benchmark=0` 的账号。
- 「播放中位数」是本周有 `metric` 的作品 views 中位数；「待发布包数」= 状态非 `published`。

## 3. 数据流

```text
wb serve (cli_ext/serve.py)
  └─ web/app.py:create_app(cfg)      应用工厂、Jinja 过滤器、静态文件
       ├─ web/routes.py              路由；每个请求 open/close 一个 SQLite 连接
       │    └─ web/views.py          装配模板上下文（repo 查询 + 少量聚合）
       │         └─ store/repo.py    CRUD（JSON 列在此还原为 list/dict）
       └─ web/templates/*.html       base.html + 6 个页面
            web/static/app.css
```

- `create_app(cfg=None)` 可注入 `Config`；`wb serve` 与测试共用同一入口。
- 模板过滤器集中在 `app.py` 的 `install_filters`：`fmt_int`、`fmt_num`、`fmt_pct`、`fmt_bytes`。
- 所有页面共用 `base.html`：顶部导航、`WB_HOME` 提示、错误横幅（`error`）与页脚红线提示。

## 4. 怎么加一个页面

以「对标账号」页为例：

1. **视图**：在 `workbench/web/views.py` 加一个返回模板 dict 的函数，只查 `repo`：

   ```python
   def benchmarks_page(conn):
       rows = repo.list_accounts(conn, is_benchmark=True)
       return {"rows": rows}
   ```

2. **路由**：在 `workbench/web/routes.py` 注册固定路径，复用 `render()`：

   ```python
   @router.get("/benchmarks", name="benchmarks")
   def benchmarks(request: Request) -> Response:
       cfg = _cfg(request)
       conn = _conn(request)
       try:
           context = views.benchmarks_page(conn)
       finally:
           conn.close()
       return render(request, cfg, "benchmarks.html", "benchmarks", "对标账号", context)
   ```

3. **模板**：新增 `workbench/web/templates/benchmarks.html`，`{% extends "base.html" %}`；
   空列表时给出「先跑 `wb topic seed` / `wb run demo`」引导。
4. **导航**：在 `base.html` 的 `<nav>` 加一个链接，并在测试里补 200 断言。
5. **测试**：在 `tests/test_web.py` 增加空态与有数据两种断言；写操作另测 `303`。

注意：本 Ticket 的可写路径只有已列出的 `web/`、`cli_ext/serve.py`、`scripts/`、`README.md`、
`docs/`、`tests/test_web.py`；加页面时不要改 `store/`、`pipeline/` 等共享文件。

## 5. 测试

`tests/test_web.py` 覆盖：

- 6 个页面 200 与空态文案（`wb run demo`）。
- `/kb/ask` 无命中与空查询提示。
- `/files`：正常读取、缺失、明文 `..`、编码穿越、绝对路径、软链越界。
- 首页关键数字（本周发布数 / 播放中位数 / Top3 / 待发布包 / 最近 job）。
- 选题「生成文案」`POST → 303 /copies` 且确实写入 10 条 `copy_set`。
- 文案详情与发布包 manifest 展开。
- `scripts/demo.sh --list` 的产物清单函数：齐全返回 0，缺 `report.md` 返回非 0。
- `wb serve --help` 暴露 `--host` / `--port`。

> 注：Starlette 1.7 的 `TestClient` 需要事件循环自管道的 socket，测试通过仓库
> `tests/conftest.py` 提供的 `allow_network` fixture 显式放行；测试本身不访问外网。

## 6. 已知限制

- 没有分页与搜索（数据量按单机万级设计）；列表直接渲染近期全部行。
- 没有登录/权限/CSRF token；只在 `127.0.0.1` 使用，不要暴露公网。
- 不做 WebSocket 实时进度；`POST /topics/{id}/copy` 会同步跑流水线，期间页面等待。
- 没有引入 HTMX 本地文件；当前全用原生表单，功能优先。
- 环境无浏览器/截图工具，CI 只做 HTML 与路由断言，不做视觉回归。
