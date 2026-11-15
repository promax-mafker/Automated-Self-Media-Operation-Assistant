"""发布包、发布记录与数据回流（T08）。

- :mod:`workbench.publish.build`：本地打包实现与 ``build_publisher()`` 契约入口；
- :mod:`workbench.publish.bundle`：把 ``copy_set`` / ``asset`` / T02 指标组装成发布包并落库；
- :mod:`workbench.publish.manifest`：manifest、SHA-256 校验和建议发布时间；
- :mod:`workbench.publish.backfill`：平台导出 CSV 按 URL 回灌 ``post`` / ``metric``。

本包只做本地打包与记录，不登录平台、不发布、不调平台 API。
错误统一继承 :class:`PublishError`，CLI 默认退出码 2。
"""

from __future__ import annotations

from workbench.errors import WorkbenchError


class PublishError(WorkbenchError):
    """T08 的可预期错误（copy/asset 不存在、URL 非法、CSV 缺列等）；退出码 2。"""

    exit_code = 2


__all__ = ["PublishError"]
