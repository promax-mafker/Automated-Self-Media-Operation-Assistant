"""数据复盘与账号洞察（T02）。

本包提供三块能力：

- :mod:`workbench.metrics.import_csv`：平台导出 CSV → ``account`` / ``post`` / ``metric``。
- :mod:`workbench.metrics.compute`：ISO 周计算、指标聚合、分组与异常检测。
- :mod:`workbench.metrics.report`：Markdown 周报与三条下一步动作。

错误统一继承 :class:`MetricsError`；它属于用户输入/数据问题，CLI 默认退出码 2。
"""

from __future__ import annotations

from workbench.errors import WorkbenchError


class MetricsError(WorkbenchError):
    """T02 的可预期错误（CSV 缺列、周格式错误、账号不存在等）；退出码 2。"""

    exit_code = 2


__all__ = ["MetricsError"]
