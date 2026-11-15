# Pipeline 内核说明（T01）

本文说明 `workbench/pipeline/` 与 `workbench/stages/` 的用法。数据模型与 stage 契约以
[`docs/adr/0001-mvp-architecture.md`](adr/0001-mvp-architecture.md) §5–§6 为准，本文只讲操作。

## 1. 一条流水线怎么跑

```sh
wb doctor                                  # 环境、user_version、stage 覆盖表
wb run demo --out /tmp/wbdemo              # 跑通演示流水线
wb run demo --out /tmp/wbdemo --input k=v  # 给 stage 传参数（可重复）
wb run demo --stop-after demo_copy         # 执行到指定 stage（含）后停止
wb db init                                 # 幂等迁移，可重复执行
```

`wb run <pipeline> --out <dir>` 的 `--out` 是最终输出目录本身，不是父目录；不传时默认
`WB_HOME/runs/<run_id>/`。runner 在该目录写：

- `manifest.json`：`run_id`、`pipeline`、`status`、`started_at` / `ended_at`、`params`、
  以及按执行顺序排列的 `stages`（每项含 `name`、`status`、`output`）。
- 各 stage 自己写出的文件，例如 demo 的 `report.md` 与 PNG。

`manifest.json` 在成功和失败时都会写；失败时额外带 `error`，`stages` 只包含已完成的 stage。

## 2. 新增一个 stage

1. 在 `workbench/stages/` 新建模块，例如 `workbench/stages/topic.py`。
2. 用 `@register("stage_name")` 注册一个类；`name` 必须与注册名一致，`needs` 声明必需的
   `--input` 参数键：

```python
from typing import Any

from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.store import repo


@register("topic_score")
class TopicScore:
    name = "topic_score"
    needs = ("since",)

    def run(self, ctx: RunContext) -> dict[str, Any]:
        since = ctx.params["since"]          # runner 已检查 needs，这里可直接取
        # ctx.db / ctx.llm / ctx.cfg / ctx.out / ctx.log 都可用
        ctx.log(f"topic_score: since={since}")
        return {"scored": 0}                  # 必须是 JSON 可序列化的 dict
```

3. 不需要修改 `workbench/stages/__init__.py` 或 `workbench/pipeline/*`。该包用
   `pkgutil.iter_modules(__path__)` 自动导入所有模块；导入失败会报出模块名与原始异常。

Stage 的返回值会写进当前 stage 的 `job.output_ref`（JSON），并进入 `manifest.json` 的
`stages[].output`。同一个 stage 不要依赖上一个 stage 的内存变量；共享数据请落库或落盘。

## 3. 新增一条流水线

流水线是 `workbench/pipeline/definitions.py` 里的 `PIPELINES`（ADR §6.2 在 T01 固定）。
新增一条流水线需要：

1. 在 `PIPELINES` 增加 `"<pipeline>": ["stage_a", "stage_b"]`。
2. 用上面的方式注册每个 stage。
3. 在 `STAGE_TICKETS` 补上 stage 负责的 Ticket，`wb doctor` 与 `MissingStage` 会用到。

如果只是实现一条已有流水线里标记为 `missing` 的 stage（例如 T02 的 `metrics_import`），
只需新增 stage 文件并注册，不要改 `PIPELINES`。`wb doctor` 会自动从 `missing` 变为 `ok`。

## 4. runner 的错误语义

runner 对每个 stage 先插一行 `job(status='running')`，再执行：

| 情况 | job 最终状态 | 抛出的异常 | CLI 退出码 |
| --- | --- | --- | --- |
| 正常返回 dict | `done`，`output_ref` 为返回 JSON | 无 | 0 |
| stage 未注册 | `failed`，`error` 为 MissingStage 文本 | `MissingStage` | 2 |
| `needs` 参数缺失 | `failed` | `MissingParam` | 2 |
| stage 抛异常 | `failed`，`error` 为异常文本 | `PipelineFailed`（保留原异常为 `__cause__`） | 1 |
| stage 返回非 dict | `failed` | `PipelineFailed` | 1 |

要点：

- 失败不会回滚已完成的 stage；每个 stage 各占一行 job，便于复盘到具体哪一步。
- runner 不做幂等合并。重复执行会得到新 `run_id`、新的 job 行；需要幂等的 stage 自己负责
  （例如先查后写或使用唯一键）。
- `MissingStage` 的文本固定为
  `MissingStage: <pipeline>/<stage> 尚未实现（<Ticket>）`，未分配的 stage 不带括号部分。
- 未实现的 stage 不应让 `wb doctor` 报错；doctor 只把状态标为 `missing`。

## 5. `--input` 怎么传

```sh
wb run copy --input topic_id=topic_ab12cd34ef56 --input tone=口语
```

- 每次 `--input` 传一个 `k=v`；值按字符串保存，不做类型转换（stage 自己解析数字/布尔）。
- 键名不能为空，缺少 `=` 会被 CLI 以用法错误（退出码 2）拒绝。
- `needs` 里声明的键缺失时，runner 在该 stage 落一行 `failed` 并抛 `MissingParam`，
  不会继续执行后面的 stage。
- 参数会写进 manifest 的 `params`，也会作为 job 的 `input_ref`（紧凑 JSON）。

## 6. 模型与适配器

- `WB_LLM=stub` 时 `StubLLM.json()` 按 pydantic schema 生成确定性合法值：字符串
  `"【stub】<字段名>"`、列表 1 个元素、数字 1、布尔 `true`、嵌套模型递归填充。
  `text()` 回显最后一条 user 消息前 80 字。所有离线测试都使用它。
- `WB_LLM=openai` 时使用 `OpenAICompatLLM`（`httpx` 调 `{WB_LLM_BASE_URL}/chat/completions`）。
  JSON 解析失败会重试 1 次，仍失败抛 `LLMError`（附原始响应前 500 字）。
- `workbench/contracts.py` 定义了信源 / 知识库 / 渲染 / 视频 / 发布五个协议。调用方只用
  `get_kb(cfg)` / `get_renderer()` / `get_composer()` / `get_publisher()` /
  `get_source_adapters(cfg)`；实现模块缺失时返回 Null 实现并记 warning，不崩。
  实现模块固定路径：
  - `workbench.knowledge.build:build_kb(cfg)`
  - `workbench.render.build:build_renderer()`
  - `workbench.video.build:build_composer()`
  - `workbench.publish.build:build_publisher()`
  - `workbench.topics.sources:build_adapters(cfg)`

## 7. CLI 扩展

`workbench/cli.py` 会遍历 `workbench/cli_ext/*.py`，把模块里的 `app: typer.Typer` 挂到根
命令。新增子命令只需新增文件：

```python
import typer

app = typer.Typer(name="topics", help="选题相关命令")


@app.command("list")
def list_topics() -> None:
    ...
```

未设置 `Typer(name=...)` 时使用模块文件名作为子命令名。不要在 `cli.py` 里手工 import
各 Ticket 的命令模块。

## 8. 测试约束

`tests/conftest.py` 用 autouse fixture 把 `socket.socket` / `socket.create_connection` /
`socket.getaddrinfo` 换成抛异常的实现，测试全程不允许外网。确需联网的测试必须显式声明
`allow_network` fixture（当前没有使用）。生成类测试只断结构、尺寸、字段与命中数，不断言
像素哈希或模型原文。
