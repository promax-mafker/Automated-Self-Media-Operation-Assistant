"""StubLLM 任意 schema、OpenAI 兼容解析与重试、工厂选择。"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

import workbench.llm.openai as openai_module
from workbench.config import Config
from workbench.errors import ConfigError, LLMError
from workbench.llm.factory import build_llm
from workbench.llm.openai import OpenAICompatLLM
from workbench.llm.stub import StubLLM


class Inner(BaseModel):
    label: str
    count: int
    active: bool


class Item(BaseModel):
    title: str
    tags: list[str]
    inner: Inner


class Payload(BaseModel):
    name: str
    total: int
    enabled: bool
    items: list[Item]
    primary: Inner
    mapping: dict[str, int]
    note: str | None = None


def test_stub_json_builds_valid_nested_schema() -> None:
    result = StubLLM().json([{"role": "user", "content": "生成"}], Payload)

    assert isinstance(result, Payload)
    assert result.name.startswith("【stub】")
    assert isinstance(result.total, int)
    assert isinstance(result.enabled, bool)
    assert len(result.items) == 1
    assert len(result.items[0].tags) == 1
    assert result.items[0].inner.count == 1
    assert result.mapping == {"stub_mapping": 1}
    assert result.note is None or isinstance(result.note, str)
    assert Payload.model_validate(result.model_dump()) == result


def test_stub_text_echoes_last_user_message_first_80_chars() -> None:
    content = "一" * 100
    result = StubLLM().text(
        [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": content},
        ]
    )
    assert result == content[:80]


def test_stub_rejects_non_pydantic_schema() -> None:
    with pytest.raises(LLMError):
        StubLLM().json([{"role": "user", "content": "x"}], dict)  # type: ignore[arg-type]


def test_openai_json_retries_once_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    contents = iter(["不是 JSON", '{"name":"测试","count":2,"active":true}'])
    calls: list[str] = []

    def fake_post(url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
        calls.append(url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(contents)}}]},
        )

    monkeypatch.setattr(openai_module.httpx, "post", fake_post)
    llm = OpenAICompatLLM(base_url="https://example.invalid/v1", api_key="k", model="m")

    class Simple(BaseModel):
        name: str
        count: int
        active: bool

    result = llm.json([{"role": "user", "content": "x"}], Simple)
    assert result.name == "测试"
    assert len(calls) == 2


def test_openai_json_raises_after_second_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "仍然不是 JSON"}}]},
        )

    monkeypatch.setattr(openai_module.httpx, "post", fake_post)
    llm = OpenAICompatLLM(base_url="https://example.invalid/v1", api_key="k", model="m")

    class Simple(BaseModel):
        name: str

    with pytest.raises(LLMError) as excinfo:
        llm.json([{"role": "user", "content": "x"}], Simple)
    assert "原始响应" in str(excinfo.value)


def test_factory_defaults_to_stub_without_key(tmp_path) -> None:
    cfg = Config.from_env({"WB_HOME": str(tmp_path / "wb")})
    assert cfg.llm_mode == "stub"
    assert cfg.llm_auto is True
    assert isinstance(build_llm(cfg), StubLLM)


def test_factory_rejects_openai_without_key(tmp_path) -> None:
    cfg = Config.from_env({"WB_HOME": str(tmp_path / "wb"), "WB_LLM": "openai"})
    with pytest.raises(ConfigError):
        build_llm(cfg)


def test_config_rejects_unknown_llm_mode(tmp_path) -> None:
    with pytest.raises(ConfigError):
        Config.from_env({"WB_HOME": str(tmp_path / "wb"), "WB_LLM": "other"})
