"""模型层：stub 与 OpenAI 兼容实现。"""

from workbench.llm.base import LLMClient, Message
from workbench.llm.factory import build_llm
from workbench.llm.openai import OpenAICompatLLM
from workbench.llm.stub import StubLLM

__all__ = ["LLMClient", "Message", "OpenAICompatLLM", "StubLLM", "build_llm"]
