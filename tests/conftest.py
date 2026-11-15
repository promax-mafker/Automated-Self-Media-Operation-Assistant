"""pytest 公共 fixture：强制离线 + 临时 WB_HOME。"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from workbench.config import Config
from workbench.store.db import init_db


@pytest.fixture(autouse=True)
def block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """禁止全部测试联网；需要网络时显式声明 ``allow_network`` fixture。"""
    if "allow_network" in request.fixturenames:
        yield
        return

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("测试禁止联网：socket 已被 conftest.py 禁用")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    yield


@pytest.fixture
def allow_network() -> Iterator[None]:
    """显式放行网络的占位 fixture；当前没有任何测试使用。"""
    yield


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config.from_env(
        {
            "WB_HOME": str(tmp_path / "wb"),
            "WB_LLM": "stub",
        }
    )


@pytest.fixture
def conn(cfg: Config) -> Iterator[sqlite3.Connection]:
    connection = init_db(cfg.db_path)
    yield connection
    connection.close()
