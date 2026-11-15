"""环境变量读取与路径解析（ADR §6.5）。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workbench.errors import ConfigError

DEFAULT_HOME = "./.wb"
DEFAULT_LLM_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-chat"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_PLATFORM = "xhs"
DEFAULT_FONT_DIR = "assets/fonts"

LLM_MODES = ("stub", "openai")


@dataclass(slots=True)
class Config:
    """一次进程运行所需的全部配置。路径均已解析为绝对路径。"""

    home: Path
    db_path: Path
    artifacts_dir: Path
    runs_dir: Path
    packs_dir: Path
    llm_mode: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tz: str
    platform: str
    font_dir: Path
    llm_auto: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        """从环境变量构造配置；``WB_HOME`` 等相对路径按当前工作目录解析。"""
        env = dict(os.environ) if environ is None else dict(environ)

        home = Path(env.get("WB_HOME") or DEFAULT_HOME).expanduser().resolve()
        font_dir = Path(env.get("WB_FONT_DIR") or DEFAULT_FONT_DIR).expanduser().resolve()

        llm_mode_raw = (env.get("WB_LLM") or "").strip().lower()
        api_key = (env.get("WB_LLM_API_KEY") or "").strip()
        if llm_mode_raw:
            if llm_mode_raw not in LLM_MODES:
                raise ConfigError(
                    f"WB_LLM 只支持 {'/'.join(LLM_MODES)}，当前为 {llm_mode_raw!r}"
                )
            llm_mode = llm_mode_raw
            llm_auto = False
        else:
            # 未显式设置：有 key 走 openai，无 key 自动 stub。
            llm_mode = "openai" if api_key else "stub"
            llm_auto = True

        tz = (env.get("WB_TZ") or DEFAULT_TZ).strip() or DEFAULT_TZ
        platform = (env.get("WB_PLATFORM") or DEFAULT_PLATFORM).strip() or DEFAULT_PLATFORM

        return cls(
            home=home,
            db_path=home / "wb.sqlite3",
            artifacts_dir=home / "artifacts",
            runs_dir=home / "runs",
            packs_dir=home / "packs",
            llm_mode=llm_mode,
            llm_base_url=(env.get("WB_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL).strip()
            or DEFAULT_LLM_BASE_URL,
            llm_api_key=api_key,
            llm_model=(env.get("WB_LLM_MODEL") or DEFAULT_LLM_MODEL).strip() or DEFAULT_LLM_MODEL,
            tz=tz,
            platform=platform,
            font_dir=font_dir,
            llm_auto=llm_auto,
        )

    def ensure_dirs(self) -> None:
        """创建 WB_HOME 下的数据目录；重复调用幂等。"""
        for path in (self.home, self.artifacts_dir, self.runs_dir, self.packs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def timezone(self) -> tzinfo:
        try:
            return ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc

    def relpath(self, path: Path | str) -> str:
        """返回相对 WB_HOME 的路径字符串；在 WB_HOME 之外时退回绝对路径。"""
        resolved = Path(path).expanduser().resolve()
        try:
            return str(resolved.relative_to(self.home))
        except ValueError:
            return str(resolved)


def now_iso(tz_name: str = DEFAULT_TZ) -> str:
    """当前时间，ISO 8601 带时区（默认 Asia/Shanghai）。"""
    try:
        zone: tzinfo = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    return datetime.now(zone).isoformat(timespec="seconds")
