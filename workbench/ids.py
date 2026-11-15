"""短 ID：``<prefix>_<12hex>``（ADR §4）。"""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    """生成 ``<prefix>_<12 位十六进制>`` 的短 ID。"""
    normalized = prefix.strip()
    if not normalized:
        raise ValueError("ID 前缀不能为空")
    return f"{normalized}_{secrets.token_hex(6)}"


def new_run_id() -> str:
    return new_id("run")
