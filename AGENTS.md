# AGENTS.md · 所有 Ticket 共享的规则

> 本文件在每个 worker 开始工作前被读取。各 Ticket 正文说「遵守 `AGENTS.md` 与 `docs/adr/0001-mvp-architecture.md`」，指的就是这里和那里。
> 架构与数据契约以 ADR 为准；本文件只放规则。两者冲突时，以 ADR 的契约条款为准。

## R1 项目定位

本仓库是「超级个体获客工作台」（Automated Self-Media Operation Assistant）的最小可运行产品（MVP）。

- 使用者是一个人（或一个 1-3 人小团队），在自己的机器上跑，管 1-3 个账号。
- 产品形态：一个 Python 服务 + 一个薄 Web 控制台 + 一组可被 CLI 单独调用的流水线。
- 核心价值是把「选题 → 文案 → 图文/视频 → 发布包 → 数据回灌 → 复盘」串成可重复的流水线，人只在审核与发布两处动手。

## R2 硬约束（违反任何一条，PR 一律不合格）

1. **只写本 Ticket 声明的可写路径。** 越界内容写进 PR 的「后续 Ticket 建议」。
2. **不改共享文件。** `pyproject.toml`、`workbench/store/db.py`、`workbench/pipeline/*`、`workbench/cli.py`、`workbench/stages/__init__.py` 属于内核，依赖与契约已预置；需要新依赖或新列时，写进「后续 Ticket 建议」。新增 stage 模块、新增 `workbench/cli_ext/*.py` 会被自动发现，不需要改共享文件。
3. **离线可跑。** 所有测试不得访问外网、不得依赖 LLM key、不得依赖浏览器或系统字体。需要外部能力时走适配器 + fixture，真实实现用环境变量开关。
4. **stub 优先。** 任何生成类能力都必须能在 `WB_LLM=stub` 下产出结构合法、可被下游消费的产物；真实模型只是把 stub 换成实现。
5. **不编造。** 平台数据、指标、案例、价格只能是：fixture 里的虚构数据、用户导入的真实导出文件、或明确标注「假设」的测算。seed 数据里的账号名一律带「（虚构）」或使用 `demo_` 前缀。
6. **不做外部动作。** 不登录任何平台、不爬取、不发布、不发消息、不注册账号、不调用需要凭据的第三方服务。发布只做「导出发布包 + 记录」，交给人。
7. **依赖已预置。** 只允许用 `pyproject.toml` 里已经声明的库；不得引入 Node、Chromium、Docker、向量数据库、RAG 平台。
8. **不提问，按默认执行。** 未覆盖的选择按「最小范围、最可验证、最贴合 ADR」自行决定，写进 PR 的「决策记录」。只有 R6 的 E 条件才升级。
9. **语言。** 交付物与文档用简体中文；代码标识符、命令、字段名用英文；中文与英文/数字之间加空格。

## R3 目录与归属

```
AGENTS.md                    只读（人类维护）
README.md                    T09
pyproject.toml               只读（内核）
docs/adr/                    只读（人类维护）
docs/*.md                    各 Ticket 自己的文档
tickets/                     只读：Ticket 原文存档
assets/fonts/                只读：内置字体（Noto Sans SC，SIL OFL）
scripts/setup.sh|check.sh    只读（内核）
scripts/demo.sh|verify.sh    T09

workbench/config.py          T01
workbench/ids.py             T01
workbench/errors.py          T01
workbench/store/*.py         T01
workbench/llm/*.py           T01
workbench/pipeline/*.py      T01
workbench/stages/__init__.py T01（自动发现 stage 模块）
workbench/stages/demo.py     T01
workbench/cli.py             T01
workbench/cli_ext/__init__.py T01（自动发现子命令）
workbench/{metrics,knowledge,render,topics,copy,video,publish,web}/  对应 Ticket
workbench/cli_ext/*.py       对应 Ticket
workbench/stages/*.py        对应 Ticket
templates/                   T04（渲染模板）、T07（分镜模板）
prompts/                     T06
data/seed/                   T02、T05
data/kb/                     T03
data/compliance/             T06
```

## R4 数据契约

- 表结构与字段以 `docs/adr/0001-mvp-architecture.md` §5 为准，由 `workbench/store/db.py` 的迁移实现。
- 任何 Ticket 都不得修改已有列或语义。需要新表/新列 → 写进「后续 Ticket 建议」，由人类决定。
- 时间统一 ISO 8601 带时区（默认 `Asia/Shanghai`）；金额与比率不存格式化字符串。
- JSON 列一律存紧凑 JSON 字符串，读出来是 `list`/`dict`，不是二次编码的字符串。

## R5 质量门（每个 PR 必须通过）

```sh
bash scripts/check.sh     # ruff check + pytest -q
```

- 新增行为至少 1 个正向测试 + 1 个失败/边界测试。
- 命令行能力必须在 `wb <子命令> --help` 里能看到，并被至少一个测试覆盖。
- 生成类产物的测试断言「结构 + 硬指标」（尺寸、时长、字段、命中数），不断言像素哈希或模型原文。

## R6 升级条件（只有这三种可以请求人类）

- E1 契约冲突：ADR 的数据模型或 stage 契约无法表达本 Ticket 的需求，且改法会影响其他 Ticket。
- E2 依赖缺失：必须新增 `pyproject.toml` 之外的依赖才能完成验收。
- E3 凭据缺失：验收必须有真实平台凭据或真实模型 key，而 stub 无法覆盖。

其他情况一律不升级：缺数据就用 fixture，缺能力就写 NotImplemented 并让上层优雅降级，缺参数就用默认值。

## R7 报告与 PR 约定

1. 用 `orchestrator_report` 汇报：`state` 取 `progress` / `waiting` / `ready` / `blocked` / `failed` / `dropped`；问题不要写在 `summary` 里当提问。
2. 一个 Ticket 一个 PR、一个分支；不碰合并、不关 issue、不改他人分支。
3. PR 标题用中文动宾短语，例如 `feat(T04): 图文与海报渲染`。
4. PR 描述模板：

```
## 交付物
- 路径列表

## 验收自查
- [x]/[ ] 逐条对应 Ticket 验收标准，附命令与输出摘要

## 决策记录
- 未覆盖的选择 → 决定 → 理由

## 待核实项
- 【待核实】条目与建议的核实途径

## 后续 Ticket 建议
- 超出范围但需要做的事
```

5. `ready` 的定义是「已自审、`scripts/check.sh` 通过、PR 已开」。不要等合并再报 ready。
