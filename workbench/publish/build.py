"""本地发布包构建器（T08）。

``LocalPublisher`` 只做本地目录 + zip；不登录、不发布、不调平台 API。
``SocialAutoUploadPublisher`` 是 ADR 0002 §3 预留的对接位：只保留函数签名，
调用即抛 ``NotImplementedError``，避免误把自动发布接进 MVP。
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from workbench.contracts import Publisher
from workbench.publish import PublishError
from workbench.publish.manifest import (
    AI_DISCLOSURE,
    MANIFEST_FILENAME,
    PLATFORMS,
    PublishSuggestion,
    build_manifest,
    compute_checksums,
    write_manifest,
)

PUBLISH_NOT_IMPLEMENTED = "本项目不自动发布，请人工发布后用 wb pack log 记录"

PLATFORM_LABELS: dict[str, str] = {
    "xhs": "小红书",
    "douyin": "抖音",
    "shipinhao": "视频号",
    "wechat": "公众号",
}

PLATFORM_HINTS: dict[str, tuple[str, ...]] = {
    "xhs": (
        "首行是笔记标题，正文保留空行，末尾是 `#话题` 标签行。",
        "图片按 3:4（1080×1440）准备，建议 1–9 张；本包的图片在 `图片/` 下按 01、02 排序。",
        "发布时在平台声明处勾选「AI 生成」标识；本包不会自动发布。",
    ),
    "douyin": (
        "`文案.md` 的正文按口播稿录制，标题用于发布信息，末尾是 `#话题` 标签行。",
        "视频在 `视频/video.mp4`（若有）；竖版 1080×1920 更容易过审。",
        "发布时在平台声明处勾选「AI 生成」标识；本包不会自动发布。",
    ),
    "shipinhao": (
        "标题与正文按视频号发布信息填写，话题标签放在文案末尾。",
        "图片/视频素材在 `图片/`、`视频/` 下；发布前确认尺寸与时长。",
        "发布时在平台声明处勾选「AI 生成」标识；本包不会自动发布。",
    ),
    "wechat": (
        "标题用于公众号标题，正文可直接作为图文内容；话题标签按公众号规则使用。",
        "图片在 `图片/` 下；发布前确认封面与正文字体。",
        "发布时按平台要求声明 AI 辅助生成；本包不会自动发布。",
    ),
}

_PLATFORM_GENERIC_HINTS: tuple[str, ...] = (
    "按平台要求填写标题、正文与话题标签。",
    "素材在 `图片/`、`视频/` 下；发布前确认尺寸与时长。",
    "发布时按平台要求声明 AI 辅助生成；本包不会自动发布。",
)


@dataclass(frozen=True, slots=True)
class PackArtifacts:
    """一次打包的实际产物。"""

    dir: Path
    zip_path: Path
    files: tuple[str, ...]


def pack_zip_path(pack_dir: Path) -> Path:
    """zip 与包目录同级：``packs/pack_x/`` → ``packs/pack_x.zip``。"""
    directory = Path(pack_dir)
    return directory.parent / f"{directory.name}.zip"


def _prepare_dir(pack_dir: Path) -> None:
    if pack_dir.exists():
        if not pack_dir.is_dir():
            raise PublishError(f"输出路径已存在且不是目录：{pack_dir}")
        if any(pack_dir.iterdir()):
            raise PublishError(f"输出目录已存在且非空：{pack_dir}")
        return
    pack_dir.mkdir(parents=True, exist_ok=True)


def _normalize_tags(tags: Any) -> list[str]:
    if not isinstance(tags, (list, tuple)):
        return []
    normalized: list[str] = []
    for raw in tags:
        text = str(raw).strip().lstrip("#").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def format_copy_text(*, platform: str, title: str, body: str, tags: Any) -> str:
    """按平台生成 ``文案.md``：标题、正文、话题标签三部分。"""
    if platform not in PLATFORMS:
        raise PublishError(
            f"平台无法识别：{platform!r}；支持 {' / '.join(PLATFORMS)}"
        )
    clean_title = (title or "").strip()
    clean_body = (body or "").strip()
    if not clean_body:
        raise PublishError("文案正文为空，无法生成发布包")
    tag_list = _normalize_tags(tags)
    lines: list[str] = []
    if clean_title:
        lines.append(clean_title)
        lines.append("")
    lines.append(clean_body)
    if tag_list:
        lines.append("")
        lines.append(" ".join(f"#{tag}" for tag in tag_list))
    return "\n".join(lines).rstrip() + "\n"


def _copy_assets(
    pack_dir: Path, assets: Any
) -> list[dict[str, Any]]:
    if not isinstance(assets, list):
        raise PublishError("pack.assets 必须是数组")
    copied: list[dict[str, Any]] = []
    image_index = 0
    video_index = 0
    for index, item in enumerate(assets, start=1):
        if not isinstance(item, dict):
            raise PublishError(f"pack.assets[{index}] 必须是对象")
        asset_id = str(item.get("id") or "").strip()
        raw_source = item.get("source")
        if not asset_id:
            raise PublishError(f"pack.assets[{index}] 缺少 id")
        if not raw_source:
            raise PublishError(f"asset {asset_id} 缺少源文件路径")
        source = Path(str(raw_source)).expanduser()
        if not source.is_file():
            raise PublishError(f"asset 文件不存在：{asset_id} → {source}")

        kind = str(item.get("kind") or "poster")
        suffix = source.suffix.lower()
        if kind == "video":
            video_index += 1
            suffix = suffix or ".mp4"
            name = "video" + ("" if video_index == 1 else f"-{video_index}") + suffix
            relative = Path("视频") / name
        else:
            image_index += 1
            suffix = suffix or ".png"
            name = f"{image_index:02d}{suffix}"
            relative = Path("图片") / name

        destination = pack_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(
            {
                "id": asset_id,
                "path": relative.as_posix(),
                "width": item.get("width"),
                "height": item.get("height"),
                "duration_sec": item.get("duration_sec"),
                "source_path": str(item.get("source_path") or raw_source),
                "kind": kind,
            }
        )
    return copied


def _suggestion_from_pack(pack: dict[str, Any]) -> PublishSuggestion:
    suggestion = pack.get("suggestion")
    if isinstance(suggestion, PublishSuggestion):
        return suggestion
    if isinstance(suggestion, dict):
        return PublishSuggestion(
            at=str(suggestion.get("at") or ""),
            reason=str(suggestion.get("reason") or ""),
            default_used=bool(suggestion.get("default_used", False)),
            sample_posts=int(suggestion.get("sample_posts") or 0),
            weekday=suggestion.get("weekday"),
            slot=suggestion.get("slot"),
            mean_interaction_rate=suggestion.get("mean_interaction_rate"),
        )
    raise PublishError("pack.suggestion 缺失；请用 wb pack build 或 bundle.create_pack 生成")


def _compliance_lines(pack: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    source = pack.get("compliance") if isinstance(pack.get("compliance"), dict) else {}
    compliance = manifest["compliance"]
    passed = compliance["passed"]
    hits = compliance["hits"]
    value = "true" if passed else "false"
    lines = [
        f"- `compliance.passed`: **{value}**"
        + ("（通过）" if passed else "（未通过，请人工复核后再发布）"),
        f"- 命中项：{len(hits)} 条",
    ]
    if source.get("needs_human"):
        lines.append("- `needs_human`: true（存在需要人工判断的表述）")
    if source.get("checked_at"):
        lines.append(f"- 检查时间：{source['checked_at']}")
    if source.get("model"):
        lines.append(f"- 生成模型：{source['model']}")
    if hits:
        lines.extend(
            [
                "",
                "| 词 | 类型 | 位置 | 建议 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            word = str(hit.get("word") or "")
            kind = str(hit.get("kind") or "")
            position = str(hit.get("index") if hit.get("index") is not None else "—")
            suggestion = str(hit.get("suggestion") or "—")
            lines.append(f"| {word} | {kind} | {position} | {suggestion} |")
    return lines


def render_publish_notes(pack: dict[str, Any], manifest: dict[str, Any]) -> str:
    """生成给人看的 ``发布说明.md``。"""
    platform = manifest["platform"]
    label = PLATFORM_LABELS.get(platform, platform)
    hints = PLATFORM_HINTS.get(platform, _PLATFORM_GENERIC_HINTS)
    title = manifest["title"] or "（无标题）"
    kind = str(pack.get("copy_kind") or "")
    length = str(pack.get("copy_length") or "")

    lines: list[str] = [
        f"# 发布说明 · {title}",
        "",
        f"- pack_id：`{manifest['pack_id']}`",
        f"- 平台：{label}（`{platform}`）",
        f"- copy_set：`{manifest['copy_set_id']}`"
        + (f"（{kind} / {length}）" if kind or length else ""),
        f"- 生成时间：{manifest['created_at']}",
        f"- 文案标题：{manifest['title'] or '（无标题）'}",
        f"- 话题标签：{'、'.join(manifest['tags']) if manifest['tags'] else '（无）'}",
        "",
        "## 1. AI 生成声明（可复制到平台声明处）",
        "",
        "> " + AI_DISCLOSURE,
        "",
        "`manifest.ai_generated`：**true**（固定值）。",
        "",
        "## 2. 合规检查结果",
        "",
        *_compliance_lines(pack, manifest),
        "",
        "## 3. 建议发布时间",
        "",
        f"- 建议时间：`{manifest['suggested_publish_at']}`",
        f"- 依据：{manifest.get('suggested_publish_reason') or '—'}",
    ]
    if manifest.get("suggested_publish_default"):
        lines.append(
            "- 说明：当前没有足够的同平台指标，按默认时间给出；"
            "导入平台 CSV 后重跑可得到数据驱动的建议。"
        )
    lines.extend(
        [
            "",
            "## 4. 平台注意事项",
            "",
            *[f"- {hint}" for hint in hints],
            "",
            "## 5. 文件与校验和",
            "",
            "- `manifest.json` 内含 `checksums`，覆盖除 `manifest.json` 自身外的全部文件。",
            "- 发布前可用 `wb pack show --pack "
            + manifest["pack_id"]
            + " --json` 查看或自行核对 SHA-256。",
            "",
            "## 6. 发布后记录",
            "",
            "人工发布完成后，把平台作品 URL 记回本地（同一包重复记录以最后一次为准）：",
            "",
            "```sh",
            f"wb pack log --pack {manifest['pack_id']} --url https://example.com/p/1",
            "```",
            "",
            "本包不包含任何登录、发布或调平台 API 的动作；请不要把账号凭据写进包内。",
            "",
        ]
    )
    return "\n".join(lines)


def zip_pack(pack_dir: Path) -> Path:
    """把包目录压成 zip；zip 内保留包目录名作为顶层目录。"""
    directory = Path(pack_dir)
    zip_path = pack_zip_path(directory)
    files = [path for path in directory.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.relative_to(directory).as_posix())
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(directory).as_posix()
            archive.write(path, arcname=f"{directory.name}/{relative}")
    return zip_path


def write_pack(pack: dict[str, Any], out: Path) -> PackArtifacts:
    """把一份 pack 描述写成目录 + manifest + zip。"""
    pack_id = str(pack.get("pack_id") or "").strip()
    platform = str(pack.get("platform") or "").strip().lower()
    copy_set_id = str(pack.get("copy_set_id") or "").strip()
    created_at = str(pack.get("created_at") or "").strip()
    if not pack_id or not copy_set_id or not created_at:
        raise PublishError("pack 缺少 pack_id / copy_set_id / created_at")
    if platform not in PLATFORMS:
        raise PublishError(
            f"平台无法识别：{platform!r}；支持 {' / '.join(PLATFORMS)}"
        )

    pack_dir = Path(out).expanduser().resolve()
    _prepare_dir(pack_dir)

    title = str(pack.get("title") or "")
    body = str(pack.get("body") or "")
    tags = _normalize_tags(pack.get("tags"))

    (pack_dir / "文案.md").write_text(
        format_copy_text(platform=platform, title=title, body=body, tags=tags),
        encoding="utf-8",
    )
    copied_assets = _copy_assets(pack_dir, pack.get("assets", []))
    suggestion = _suggestion_from_pack(pack)
    manifest = build_manifest(
        pack_id=pack_id,
        platform=platform,
        copy_set_id=copy_set_id,
        title=title,
        body=body,
        tags=tags,
        assets=copied_assets,
        compliance=pack.get("compliance"),
        suggestion=suggestion,
        created_at=created_at,
    )
    (pack_dir / "发布说明.md").write_text(
        render_publish_notes(pack, manifest),
        encoding="utf-8",
    )
    manifest["checksums"] = compute_checksums(pack_dir)
    write_manifest(pack_dir / MANIFEST_FILENAME, manifest)
    zip_path = zip_pack(pack_dir)

    files = tuple(
        path.relative_to(pack_dir).as_posix()
        for path in sorted(pack_dir.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    )
    return PackArtifacts(dir=pack_dir, zip_path=zip_path, files=files)


class LocalPublisher:
    """只做本地打包的 ``Publisher`` 实现。"""

    def build(self, *, pack: dict[str, Any], out: Path) -> Path:
        return write_pack(pack, out).dir

    def publish(self, **_kwargs: Any) -> NoReturn:
        """自动发布入口：MVP 明确不支持。"""
        raise NotImplementedError(PUBLISH_NOT_IMPLEMENTED)


class SocialAutoUploadPublisher:
    """``social-auto-upload`` 适配位：只保留签名，默认关闭（ADR 0002 §3）。"""

    def build(self, *, pack: dict[str, Any], out: Path) -> Path:
        return LocalPublisher().build(pack=pack, out=out)

    def publish(
        self,
        *,
        pack_id: str,
        platform: str,
        bundle_dir: Path,
        url: str | None = None,
    ) -> NoReturn:
        raise NotImplementedError(PUBLISH_NOT_IMPLEMENTED)


def build_publisher() -> Publisher:
    """契约入口（``workbench/contracts.py`` 懒加载调用）。"""
    return LocalPublisher()


__all__ = [
    "PUBLISH_NOT_IMPLEMENTED",
    "LocalPublisher",
    "PackArtifacts",
    "SocialAutoUploadPublisher",
    "build_publisher",
    "format_copy_text",
    "pack_zip_path",
    "render_publish_notes",
    "write_pack",
    "zip_pack",
]
