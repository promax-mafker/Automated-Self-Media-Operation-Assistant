"""把 ``copy_set``、``asset`` 与 T02 指标组装成发布包，并维护 ``pack`` 行（T08）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from workbench.config import Config, now_iso
from workbench.contracts import get_publisher
from workbench.ids import new_id
from workbench.metrics import MetricsError
from workbench.metrics.compute import parse_iso
from workbench.publish import PublishError
from workbench.publish.build import pack_zip_path, zip_pack
from workbench.publish.manifest import (
    MANIFEST_FILENAME,
    PLATFORMS,
    load_manifest,
    suggest_publish_time,
    write_manifest,
)
from workbench.store import repo
from workbench.store.models import Pack


@dataclass(frozen=True, slots=True)
class PackBuildResult:
    """一次 ``wb pack build`` 的结果。"""

    pack: Pack
    dir: Path
    manifest_path: Path
    zip_path: Path
    files: tuple[str, ...]


def pack_path(cfg: Config, pack: Pack) -> Path:
    """``pack.dir`` 是相对 ``WB_HOME`` 的路径；在 WB_HOME 之外时为绝对路径。"""
    path = Path(pack.dir).expanduser()
    return path.resolve() if path.is_absolute() else (cfg.home / path).resolve()


def _asset_source(cfg: Config, asset_path: str) -> Path:
    path = Path(asset_path).expanduser()
    return path.resolve() if path.is_absolute() else (cfg.home / path).resolve()


def _asset_ids(assets: Any) -> list[str]:
    if assets is None:
        return []
    if isinstance(assets, str):
        raw_items: list[str] = [assets]
    elif isinstance(assets, (list, tuple)):
        raw_items = [str(item) for item in assets]
    else:
        raise PublishError("--assets 需要逗号分隔的 asset ID")
    ids: list[str] = []
    for raw in raw_items:
        for part in raw.split(","):
            value = part.strip()
            if value and value not in ids:
                ids.append(value)
    return ids


def _resolve_assets(
    conn: sqlite3.Connection, copy_set_id: str, assets: Any
) -> list[Any]:
    requested = _asset_ids(assets)
    if not requested:
        return list(repo.list_assets(conn, copy_set_id=copy_set_id))
    selected = []
    for asset_id in requested:
        asset = repo.get_asset(conn, asset_id)
        if asset is None:
            raise PublishError(f"找不到 asset：{asset_id}")
        selected.append(asset)
    return selected


def create_pack(
    conn: sqlite3.Connection,
    cfg: Config,
    copy_set_id: str,
    *,
    platform: str | None = None,
    assets: Any = None,
    out: Path | str | None = None,
    now: datetime | None = None,
) -> PackBuildResult:
    """构建发布包：写目录 + manifest + zip，并插入一行 ``pack``。"""
    copy_id = (copy_set_id or "").strip()
    if not copy_id:
        raise PublishError("--copy 不能为空")
    copy_set = repo.get_copy_set(conn, copy_id)
    if copy_set is None:
        raise PublishError(f"找不到 copy_set：{copy_id}；请先运行 wb copy generate")

    target_platform = (platform or cfg.platform or "").strip().lower()
    if target_platform not in PLATFORMS:
        raise PublishError(
            f"平台无法识别：{target_platform!r}；支持 {' / '.join(PLATFORMS)}"
        )

    selected = _resolve_assets(conn, copy_set.id, assets)
    asset_payload = [
        {
            "id": asset.id,
            "source": str(_asset_source(cfg, asset.path)),
            "source_path": asset.path,
            "kind": asset.kind,
            "width": asset.width,
            "height": asset.height,
            "duration_sec": asset.duration_sec,
        }
        for asset in selected
    ]

    cfg.ensure_dirs()
    pack_id = new_id("pack")
    if out is None:
        pack_dir = (cfg.packs_dir / pack_id).resolve()
    else:
        pack_dir = Path(out).expanduser().resolve()
    created_at = now_iso(cfg.tz)
    suggestion = suggest_publish_time(
        conn, target_platform, tz=cfg.tz, now=now
    )
    compliance = dict(copy_set.compliance or {})
    if copy_set.model:
        compliance.setdefault("model", copy_set.model)

    pack_payload: dict[str, Any] = {
        "pack_id": pack_id,
        "platform": target_platform,
        "copy_set_id": copy_set.id,
        "title": copy_set.title or copy_set.hook or "",
        "body": copy_set.body,
        "tags": list(copy_set.tags),
        "compliance": compliance,
        "copy_kind": copy_set.kind,
        "copy_length": copy_set.length,
        "assets": asset_payload,
        "suggestion": suggestion,
        "created_at": created_at,
    }

    publisher = get_publisher()
    returned = Path(publisher.build(pack=pack_payload, out=pack_dir)).expanduser().resolve()
    manifest_path = returned / MANIFEST_FILENAME
    manifest = load_manifest(manifest_path)
    zip_path = pack_zip_path(returned)
    if not zip_path.is_file():
        raise PublishError(f"打包器没有生成 zip：{zip_path}")

    pack = Pack(
        id=pack_id,
        copy_set_id=copy_set.id,
        platform=target_platform,
        dir=cfg.relpath(returned),
        manifest=manifest,
        status="exported",
        created_at=created_at,
    )
    repo.insert_pack(conn, pack)
    files = tuple(
        path.relative_to(returned).as_posix()
        for path in sorted(returned.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    )
    return PackBuildResult(
        pack=pack,
        dir=returned,
        manifest_path=manifest_path,
        zip_path=zip_path,
        files=files,
    )


def _validate_url(url: str) -> str:
    text = (url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PublishError(
            f"--url 必须是 http(s) 链接，收到 {url!r}；例如 https://example.com/p/1"
        )
    return text


def _normalize_at(raw: str, tz: str) -> str:
    try:
        parsed = parse_iso(raw, tz)
    except MetricsError as exc:
        raise PublishError(
            f"--at 不是合法 ISO 8601 时间：{raw!r}；"
            "例如 2026-11-15T20:00:00+08:00"
        ) from exc
    return parsed.isoformat(timespec="seconds")


def update_pack_log(
    conn: sqlite3.Connection,
    cfg: Config,
    pack_id: str,
    url: str,
    *,
    at: str | None = None,
) -> Pack:
    """记录人工发布结果：``status=published``，URL 写进 manifest，重复记录以最后一次为准。"""
    target = (pack_id or "").strip()
    if not target:
        raise PublishError("--pack 不能为空")
    pack = repo.get_pack(conn, target)
    if pack is None:
        raise PublishError(f"找不到发布包：{target}")
    clean_url = _validate_url(url)
    published_at = _normalize_at(at, cfg.tz) if at else now_iso(cfg.tz)

    directory = pack_path(cfg, pack)
    manifest_path = directory / MANIFEST_FILENAME
    manifest = load_manifest(manifest_path)
    manifest["published_url"] = clean_url
    manifest["published_at"] = published_at
    write_manifest(manifest_path, manifest)
    package_zip = pack_zip_path(directory)
    if package_zip.is_file():
        zip_pack(directory)

    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    try:
        cursor = conn.execute(
            "UPDATE pack SET status = ?, manifest = ? WHERE id = ?",
            ("published", payload, pack.id),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise PublishError(f"更新 pack 状态失败：{exc}") from exc
    if cursor.rowcount == 0:
        raise PublishError(f"找不到发布包：{pack.id}")

    updated = repo.get_pack(conn, pack.id)
    if updated is None:
        raise PublishError(f"找不到发布包：{pack.id}")
    return updated


__all__ = [
    "PackBuildResult",
    "create_pack",
    "pack_path",
    "update_pack_log",
]
