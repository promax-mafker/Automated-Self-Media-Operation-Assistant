"""控制台路由（固定路径，测试按此断言）。

| 路径 | 内容 |
| --- | --- |
| ``GET /`` | 概览 |
| ``GET /topics`` | 选题池；``POST /topics/{id}/copy`` 生成文案 |
| ``GET /copies`` | 文案列表；``?copy_id=`` 看全文与引用 |
| ``GET /assets`` | 素材网格（缩略图走 ``/files/...``） |
| ``GET /packs`` | 发布包列表；``?pack_id=`` 看 manifest |
| ``GET /kb`` + ``POST /kb/ask`` | 知识库问答 |
| ``GET /files/{path:path}`` | 只允许 ``WB_HOME`` 下的相对路径 |

所有写操作走 ``POST`` + 原生表单 + ``303`` 重定向，不依赖 JS。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.responses import Response

from workbench.config import Config
from workbench.errors import WorkbenchError
from workbench.pipeline.runner import run_pipeline
from workbench.store import repo
from workbench.store.db import init_db
from workbench.web import views

router = APIRouter()


def _cfg(request: Request) -> Config:
    return request.app.state.cfg


def _templates(request: Request) -> Any:
    return request.app.state.templates


def _conn(request: Request) -> sqlite3.Connection:
    cfg = _cfg(request)
    cfg.ensure_dirs()
    return init_db(cfg.db_path)


def render(
    request: Request,
    cfg: Config,
    template: str,
    page: str,
    title: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> Response:
    payload: dict[str, Any] = {
        "home": str(cfg.home),
        "page": page,
        "title": title,
    }
    payload.update(context or {})
    return _templates(request).TemplateResponse(
        request, template, payload, status_code=status_code
    )


# --------------------------------------------------------------------------- 概览
@router.get("/", name="index")
def index(request: Request) -> Response:
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        context = views.overview(cfg, conn)
    finally:
        conn.close()
    return render(request, cfg, "index.html", "index", "概览", context)


# --------------------------------------------------------------------------- 选题池
@router.get("/topics", name="topics")
def topics(request: Request, error: str | None = None) -> Response:
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        context = views.topics_page(conn)
    finally:
        conn.close()
    context["error"] = error
    return render(request, cfg, "topics.html", "topics", "选题池", context)


@router.post("/topics/{topic_id}/copy", name="topic_copy")
def topic_copy(request: Request, topic_id: str) -> Response:
    """生成 10 条文案后 303 回 /copies；不新增页面，不重复业务逻辑。"""
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        if repo.get_topic(conn, topic_id) is None:
            raise HTTPException(status_code=404, detail=f"找不到选题：{topic_id}")
    finally:
        conn.close()

    try:
        run_pipeline(cfg, "copy", params={"topic_id": topic_id})
    except WorkbenchError as exc:
        conn = _conn(request)
        try:
            context = views.topics_page(conn)
        finally:
            conn.close()
        context["error"] = f"生成文案失败：{exc}"
        return render(
            request, cfg, "topics.html", "topics", "选题池", context, status_code=400
        )
    return RedirectResponse(url="/copies", status_code=303)


# --------------------------------------------------------------------------- 文案
@router.get("/copies", name="copies")
def copies(request: Request, copy_id: str | None = None) -> Response:
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        context = views.copies_page(conn, copy_id=copy_id)
    finally:
        conn.close()
    return render(request, cfg, "copies.html", "copies", "文案", context)


# --------------------------------------------------------------------------- 素材
@router.get("/assets", name="assets")
def assets(request: Request) -> Response:
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        context = views.assets_page(cfg, conn)
    finally:
        conn.close()
    return render(request, cfg, "assets.html", "assets", "素材", context)


# --------------------------------------------------------------------------- 发布包
@router.get("/packs", name="packs")
def packs(request: Request, pack_id: str | None = None) -> Response:
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        context = views.packs_page(conn, pack_id=pack_id)
    finally:
        conn.close()
    return render(request, cfg, "packs.html", "packs", "发布包", context)


# --------------------------------------------------------------------------- 知识库
@router.get("/kb", name="kb")
def kb(request: Request) -> Response:
    cfg = _cfg(request)
    conn = _conn(request)
    try:
        context = views.kb_page(cfg, conn)
    finally:
        conn.close()
    return render(request, cfg, "kb.html", "kb", "知识库问答", context)


@router.post("/kb/ask", name="kb_ask")
async def kb_ask(request: Request) -> Response:
    """表单检索：POST 后直接渲染结果（只读，不写库，因此不重定向）。

    只解析原生表单的 ``application/x-www-form-urlencoded``，不依赖
    ``python-multipart``（pyproject 未声明该依赖）。
    """
    cfg = _cfg(request)
    body = await request.body()
    query = (parse_qs(body.decode("utf-8", "replace")).get("q") or [""])[0].strip()
    conn = _conn(request)
    try:
        context = views.kb_page(cfg, conn, query=query)
    except WorkbenchError as exc:
        context = {
            "docs": [],
            "chunk_count": 0,
            "query": query,
            "hits": [],
            "hint": None,
            "empty_hint": views.EMPTY_KB_HINT,
            "asked": True,
            "error": f"知识库检索失败：{exc}",
        }
    finally:
        conn.close()
    return render(request, cfg, "kb.html", "kb", "知识库问答", context)


# --------------------------------------------------------------------------- 文件
@router.get("/files/{file_path:path}", name="files")
def files(request: Request, file_path: str) -> Response:
    """只读 WB_HOME 下的文件；绝对路径、``..`` 与软链越界一律 404。"""
    cfg = _cfg(request)
    target = views.resolve_file(cfg, file_path)
    if target is None:
        raise HTTPException(status_code=404, detail="文件不存在或路径越界")
    return FileResponse(Path(target))


__all__ = ["router"]
