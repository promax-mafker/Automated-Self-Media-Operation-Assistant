"""按环境配置选择 LLM 实现（ADR §6.3）。"""

from __future__ import annotations

from workbench.config import Config
from workbench.errors import ConfigError
from workbench.llm.base import LLMClient
from workbench.llm.openai import OpenAICompatLLM
from workbench.llm.stub import StubLLM


def build_llm(cfg: Config) -> LLMClient:
    if cfg.llm_mode == "stub":
        return StubLLM()
    if cfg.llm_mode == "openai":
        if not cfg.llm_api_key:
            raise ConfigError("WB_LLM=openai 但 WB_LLM_API_KEY 为空")
        return OpenAICompatLLM(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            model=cfg.llm_model,
        )
    raise ConfigError(f"未知的 LLM 模式：{cfg.llm_mode!r}")


__all__ = ["build_llm"]
