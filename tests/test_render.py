"""T04 渲染层测试：模板列表、8 套模板、九图、截断、越界、字体、块类型与 stage。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from PIL import Image
from typer.testing import CliRunner

from workbench.cli import app
from workbench.contracts import RenderSpec
from workbench.errors import Unavailable
from workbench.render.build import PillowRenderer
from workbench.render.fonts import FontBook
from workbench.render.layout import LayoutError, load_tokens, parse_canvas, plan_page

FIXTURES = Path(__file__).parent / "fixtures"
POSTER_DATA: dict[str, Any] = json.loads(
    (FIXTURES / "poster_data.json").read_text(encoding="utf-8")
)
CAROUSEL_DATA: dict[str, Any] = json.loads(
    (FIXTURES / "carousel_data.json").read_text(encoding="utf-8")
)


@pytest.fixture
def renderer() -> PillowRenderer:
    return PillowRenderer()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    monkeypatch.delenv("WB_FONT_DIR", raising=False)
    monkeypatch.delenv("WB_TEMPLATE_DIR", raising=False)
    return home


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    if stderr:
        text += stderr
    return text


def _write_template(root: Path, template: dict[str, Any]) -> Path:
    directory = root / "poster"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{template['id']}.json"
    path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
    return path


def _single_block_template(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "unit-block",
        "label": "块单测",
        "canvas": {"width": 1080, "height": 1440, "background": "#FFFFFF", "pad": 72},
        "blocks": [block],
    }


def test_render_list_shows_five_posters_and_three_carousels(cli_env: Path) -> None:
    result = _invoke(["render", "list"])
    output = _combined(result)
    assert result.exit_code == 0, output
    for template_id in (
        "poster-hero",
        "poster-quote",
        "poster-checklist",
        "poster-story",
        "poster-minimal",
        "xhs-nine",
        "xhs-square",
        "vertical-cover",
    ):
        assert template_id in output
    assert "1080×1440" in output
    assert "1080×1080" in output
    assert "1080×1920" in output
    assert "poster 5 / carousel 3" in output
    assert re.search(r"xhs-nine\s*│\s*carousel\s*│\s*1080×1440\s*│\s*9", output)
    assert re.search(r"xhs-square\s*│\s*carousel\s*│\s*1080×1080\s*│\s*4", output)
    assert re.search(r"vertical-cover\s*│\s*carousel\s*│\s*1080×1920\s*│\s*1", output)


def test_each_poster_template_renders_declared_size(
    renderer: PillowRenderer, tmp_path: Path
) -> None:
    posters = [info for info in renderer.list_templates() if info.kind == "poster"]
    assert len(posters) == 5
    generated = 0
    for info in posters:
        out = tmp_path / f"{info.id}.png"
        result = renderer.render(
            RenderSpec(kind="poster", out=out, template=info.id, data=POSTER_DATA)
        )
        assert result.path == out
        assert (result.width, result.height) == (info.width, info.height)
        assert result.bytes == out.stat().st_size
        with Image.open(out) as image:
            assert image.format == "PNG"
            assert image.size == (info.width, info.height)
        generated += 1
    assert generated == 5


def test_each_carousel_template_renders_all_pages(
    renderer: PillowRenderer, tmp_path: Path
) -> None:
    carousels = [info for info in renderer.list_templates() if info.kind == "carousel"]
    assert len(carousels) == 3
    for info in carousels:
        out = tmp_path / info.id
        result = renderer.render(
            RenderSpec(kind="carousel", out=out, template=info.id, data=CAROUSEL_DATA)
        )
        pages = sorted(path.name for path in out.glob("*.png"))
        assert len(pages) == info.pages
        assert pages == [f"{index:02d}.png" for index in range(1, info.pages + 1)]
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["template"] == info.id
        assert manifest["page_count"] == info.pages
        assert [page["page"] for page in manifest["pages"]] == list(
            range(1, info.pages + 1)
        )
        assert all(page["slots"] for page in manifest["pages"])
        with Image.open(out / "01.png") as image:
            assert image.size == (info.width, info.height)
        assert result.meta["manifest"] == "manifest.json"


def test_xhs_nine_produces_nine_files_and_manifest(
    renderer: PillowRenderer, tmp_path: Path
) -> None:
    out = tmp_path / "nine"
    renderer.render(
        RenderSpec(kind="carousel", out=out, template="xhs-nine", data=CAROUSEL_DATA)
    )
    for index in range(1, 10):
        page = out / f"{index:02d}.png"
        assert page.is_file()
        with Image.open(page) as image:
            assert image.size == (1080, 1440)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["page_count"] == 9
    assert [page["file"] for page in manifest["pages"]] == [
        f"{index:02d}.png" for index in range(1, 10)
    ]
    assert manifest["pages"][0]["slots"]["title"] == CAROUSEL_DATA["pages"][0]["title"]


def test_same_input_renders_identical_structure(
    renderer: PillowRenderer, tmp_path: Path
) -> None:
    first = renderer.render(
        RenderSpec(
            kind="poster", out=tmp_path / "a.png", template="poster-hero", data=POSTER_DATA
        )
    )
    second = renderer.render(
        RenderSpec(
            kind="poster", out=tmp_path / "b.png", template="poster-hero", data=POSTER_DATA
        )
    )
    assert (first.width, first.height) == (second.width, second.height)

    def structure(result: Any) -> dict[str, Any]:
        meta = json.loads(json.dumps(result.meta, ensure_ascii=False))
        for page in meta["pages"]:
            page.pop("file", None)
        return meta

    assert structure(first) == structure(second)


def test_long_title_truncates_with_ellipsis_and_warning(
    renderer: PillowRenderer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = {"slots": {"title": "超长标题" * 50, "tags": ["测试"], "footer": "结尾"}}
    out = tmp_path / "long.png"
    with caplog.at_level(logging.WARNING, logger="workbench.render"):
        result = renderer.render(
            RenderSpec(kind="poster", out=out, template="poster-hero", data=data)
        )
    title = next(
        block
        for block in result.meta["pages"][0]["blocks"]
        if block["type"] == "text" and block["bbox"][1] == 216
    )
    assert title["lines"][-1].endswith("…")
    assert any("截断" in warning for warning in result.meta["warnings"])
    assert "截断" in caplog.text
    with Image.open(out) as image:
        assert image.size == (1080, 1440)


def test_overflow_template_raises_layout_error(tmp_path: Path) -> None:
    template = _single_block_template(
        {
            "type": "text",
            "slot": "title",
            "x": 1080 + 100,
            "y": 200,
            "size": 64,
            "weight": "bold",
            "color": "#000000",
            "max_width": 400,
            "max_lines": 2,
            "line_height": 90,
            "align": "left",
        }
    )
    template["id"] = "bad-overflow"
    _write_template(tmp_path, template)
    renderer = PillowRenderer(template_dir=tmp_path)
    with pytest.raises(LayoutError) as excinfo:
        renderer.render(
            RenderSpec(
                kind="poster",
                out=tmp_path / "bad.png",
                template="bad-overflow",
                data={"slots": {"title": "任何文案"}},
            )
        )
    message = str(excinfo.value)
    assert "越界" in message
    assert "bad-overflow" in message
    assert "1080×1440" in message


def test_overlapping_text_blocks_raise_layout_error(tmp_path: Path) -> None:
    template = _single_block_template(
        {
            "type": "text",
            "slot": "title",
            "x": 72,
            "y": 200,
            "size": 64,
            "weight": "bold",
            "color": "#000000",
            "max_width": 600,
            "max_lines": 2,
            "line_height": 90,
            "align": "left",
        }
    )
    template["id"] = "bad-overlap"
    template["blocks"].append(
        {
            "type": "text",
            "slot": "subtitle",
            "x": 72,
            "y": 260,
            "size": 32,
            "weight": "regular",
            "color": "#333333",
            "max_width": 600,
            "max_lines": 2,
            "line_height": 50,
            "align": "left",
        }
    )
    _write_template(tmp_path, template)
    renderer = PillowRenderer(template_dir=tmp_path)
    with pytest.raises(LayoutError) as excinfo:
        renderer.render(
            RenderSpec(
                kind="poster",
                out=tmp_path / "overlap.png",
                template="bad-overlap",
                data={"slots": {"title": "标题", "subtitle": "副标题"}},
            )
        )
    assert "重叠" in str(excinfo.value)


def test_missing_font_raises_unavailable_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WB_FONT_DIR", str(tmp_path / "empty"))
    renderer = PillowRenderer()
    with pytest.raises(Unavailable) as excinfo:
        renderer.render(
            RenderSpec(
                kind="poster", out=tmp_path / "font.png", template="poster-hero", data=POSTER_DATA
            )
        )
    assert "Unavailable" in str(excinfo.value)
    assert "assets/fonts" in str(excinfo.value)


def test_cli_missing_font_exits_nonzero_with_hint(
    cli_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WB_FONT_DIR", str(tmp_path / "empty"))
    result = _invoke(
        [
            "render",
            "poster",
            "--template",
            "poster-hero",
            "--data",
            str(FIXTURES / "poster_data.json"),
            "--out",
            str(tmp_path / "out.png"),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 1, output
    assert "CJK 字体" in output
    assert "assets/fonts" in output


def test_cli_rejects_wrong_template_kind(cli_env: Path, tmp_path: Path) -> None:
    result = _invoke(
        [
            "render",
            "poster",
            "--template",
            "xhs-nine",
            "--data",
            str(FIXTURES / "poster_data.json"),
            "--out",
            str(tmp_path / "wrong.png"),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 1, output
    assert "carousel" in output


def test_cli_help_lists_render_commands(cli_env: Path) -> None:
    root = _combined(_invoke(["--help"]))
    assert "render" in root
    render_help = _combined(_invoke(["render", "--help"]))
    assert "list" in render_help
    assert "poster" in render_help
    assert "carousel" in render_help
    poster_help = _combined(_invoke(["render", "poster", "--help"]))
    assert "--template" in poster_help
    assert "--data" in poster_help
    assert "--out" in poster_help


BLOCK_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "text",
        {
            "type": "text",
            "slot": "title",
            "x": 72,
            "y": 72,
            "size": 64,
            "weight": "bold",
            "color": "#111111",
            "max_width": 936,
            "max_lines": 2,
            "line_height": 90,
            "align": "left",
        },
        {"title": "块类型单测"},
    ),
    (
        "list",
        {
            "type": "list",
            "slot": "points",
            "x": 72,
            "y": 72,
            "size": 36,
            "weight": "regular",
            "color": "#111111",
            "max_width": 936,
            "max_lines": 3,
            "line_height": 50,
            "item_gap": 10,
            "bullet": "•",
            "align": "left",
        },
        {"points": ["第一条", "第二条"]},
    ),
    (
        "panel",
        {
            "type": "panel",
            "x": 72,
            "y": 72,
            "width": 936,
            "height": 300,
            "fill": "#EEEEEE",
            "radius": 24,
            "stroke": "#CCCCCC",
            "stroke_width": 2,
            "slot": "title",
            "size": 40,
            "color": "#111111",
            "padding": 32,
            "max_lines": 2,
            "line_height": 56,
            "align": "left",
        },
        {"title": "面板里的文字"},
    ),
    (
        "chips",
        {
            "type": "chips",
            "slot": "tags",
            "x": 72,
            "y": 72,
            "size": 24,
            "weight": "regular",
            "color": "#FFFFFF",
            "background": "#333333",
            "max_width": 936,
            "max_rows": 2,
            "gap": 12,
            "padding": 10,
            "radius": 999,
            "align": "left",
        },
        {"tags": ["标签一", "标签二"]},
    ),
    (
        "divider",
        {
            "type": "divider",
            "x": 72,
            "y": 72,
            "width": 936,
            "thickness": 4,
            "color": "#FF5A36",
        },
        {},
    ),
    (
        "image",
        {
            "type": "image",
            "slot": "image_path",
            "x": 72,
            "y": 72,
            "width": 936,
            "height": 400,
            "fill": "#EEEEEE",
            "stroke": "#CCCCCC",
            "radius": 24,
            "label": "占位图",
            "label_size": 28,
            "label_color": "#666666",
        },
        {},
    ),
]


@pytest.mark.parametrize(("block_type", "block", "slots"), BLOCK_CASES)
def test_each_block_type_plans_and_draws(
    block_type: str,
    block: dict[str, Any],
    slots: dict[str, Any],
    tmp_path: Path,
) -> None:
    template = _single_block_template(block)
    template["id"] = f"unit-{block_type}"
    _write_template(tmp_path, template)
    canvas = parse_canvas(template)
    fonts = FontBook()
    plan = plan_page(
        template["blocks"],
        slots,
        load_tokens(tmp_path),
        fonts,
        canvas,
        template_id=template["id"],
    )
    assert [op["type"] for op in plan.ops] == [block_type]
    assert plan.ops[0]["bbox"][0] >= canvas.pad
    assert plan.ops[0]["bbox"][2] <= canvas.width - canvas.pad
    renderer = PillowRenderer(template_dir=tmp_path)
    out = tmp_path / f"{block_type}.png"
    result = renderer.render(
        RenderSpec(kind="poster", out=out, template=template["id"], data={"slots": slots})
    )
    assert out.is_file()
    assert (result.width, result.height) == (canvas.width, canvas.height)
    with Image.open(out) as image:
        assert image.size == (1080, 1440)


def test_image_block_loads_local_file(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (320, 200), (10, 120, 200)).save(source)
    block = next(block for name, block, _slots in BLOCK_CASES if name == "image")
    template = _single_block_template(block)
    template["id"] = "unit-image-file"
    _write_template(tmp_path, template)
    renderer = PillowRenderer(template_dir=tmp_path)
    result = renderer.render(
        RenderSpec(
            kind="poster",
            out=tmp_path / "image-file.png",
            template=template["id"],
            data={"slots": {"image_path": str(source)}},
        )
    )
    image_block = result.meta["pages"][0]["blocks"][0]
    assert image_block["placeholder"] is False


def test_stage_render_assets_poster_writes_asset_row(cli_env: Path, tmp_path: Path) -> None:
    out = tmp_path / "run"
    result = _invoke(
        [
            "run",
            "visual",
            "--input",
            "template=poster-hero",
            "--input",
            f"data={FIXTURES / 'poster_data.json'}",
            "--out",
            str(out),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 0, output
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert [stage["name"] for stage in manifest["stages"]] == ["render_assets"]
    assert manifest["stages"][0]["output"]["kind"] == "poster"
    image_path = out / "assets" / "poster-hero.png"
    assert image_path.is_file()
    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        rows = connection.execute("SELECT kind, template, width, height FROM asset").fetchall()
    finally:
        connection.close()
    assert rows == [("poster", "poster-hero", 1080, 1440)]


def test_stage_render_assets_carousel_writes_one_row_per_page(
    cli_env: Path, tmp_path: Path
) -> None:
    out = tmp_path / "run-nine"
    result = _invoke(
        [
            "run",
            "visual",
            "--input",
            "template=xhs-nine",
            "--input",
            f"data={FIXTURES / 'carousel_data.json'}",
            "--out",
            str(out),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 0, output
    assert (out / "assets" / "xhs-nine" / "09.png").is_file()
    assert (out / "assets" / "xhs-nine" / "manifest.json").is_file()
    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        rows = connection.execute("SELECT kind, template, meta FROM asset").fetchall()
    finally:
        connection.close()
    assert len(rows) == 9
    assert {row[0] for row in rows} == {"carousel"}
    assert {row[1] for row in rows} == {"xhs-nine"}
    assert {json.loads(row[2])["page"] for row in rows} == set(range(1, 10))


def test_stage_render_assets_requires_data(cli_env: Path, tmp_path: Path) -> None:
    result = _invoke(
        [
            "run",
            "visual",
            "--input",
            "template=poster-hero",
            "--out",
            str(tmp_path / "run-missing"),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 1, output
    assert "data" in output
    assert "copy_set_id" in output
