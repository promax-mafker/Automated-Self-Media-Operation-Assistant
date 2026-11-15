"""知识库能力（T03）。

实现模块固定为 ``workbench.knowledge.build:build_kb(cfg)``；其余模块：

- ``chunk``：Markdown 切块
- ``index``：FTS5 + jieba 索引与检索
- ``answer``：引用回答与离线评测
"""

from __future__ import annotations

from workbench.knowledge.answer import AskResult, ask, evaluate, hit_to_dict, snippet
from workbench.knowledge.build import build_kb
from workbench.knowledge.chunk import Chunk, ParsedDoc, parse_file, parse_markdown
from workbench.knowledge.index import KnowledgeBase, build_seg, tokenize

__all__ = [
    "AskResult",
    "Chunk",
    "KnowledgeBase",
    "ParsedDoc",
    "ask",
    "build_kb",
    "build_seg",
    "evaluate",
    "hit_to_dict",
    "parse_file",
    "parse_markdown",
    "snippet",
    "tokenize",
]
