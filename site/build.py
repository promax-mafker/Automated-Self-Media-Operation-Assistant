"""把本地 demo 的数据渲染成静态站点（供 GitHub Pages 托管）。

用法（在仓库根目录）：

    python3 site/build.py [demo 目录]

不传 ``demo 目录`` 时取 ``out/demo-*`` 里最新的一份。产物落在 ``site/dist/``：

    index.html                 落地页（来自 site/src/，examples.html 是模板不进 dist）
    console/*.html             控制台页面快照（列表页 + 详情页，链接/图片/表单已改写）
    examples/report.html       demo 跑出的账号周报（Markdown → HTML）
    assets/*.css|png|mp4|zip   样式与样例产物
    .nojekyll                  让 GitHub Pages 跳过 Jekyll

快照是只读渲染：表单禁用，``?copy_id=`` / ``?pack_id=`` 链接指向对应静态页，
本机路径被替换成「demo 数据根目录」，避免把开发机路径发到公网。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = ROOT / "site" / "src"
DIST = ROOT / "site" / "dist"

# 脚本目录是 site/，把仓库根放回 sys.path，保证导入本仓库的 workbench。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NAV = {
    "/": "index.html",
    "/topics": "topics.html",
    "/copies": "copies.html",
    "/assets": "assets.html",
    "/packs": "packs.html",
    "/kb": "kb.html",
}

LIST_ROUTES = list(NAV.items())

BANNER = (
    '<div class="snapshot-note">'
    "<strong>静态快照</strong> · 这些页面由本地 demo 数据预渲染，只读；"
    "「生成文案」「知识库问答」等写操作需要在本地运行 <code>wb</code> 命令。"
    "</div>"
)

# 页面前缀 → 额外说明（stub 模型产出的页面要讲清楚，别让人以为是真实文案）
PAGE_NOTES: list[tuple[str, str]] = [
    (
        "copies",
        "本页文案由 <code>WB_LLM=stub</code> 的占位模型生成，"
        "标题与正文是占位文本；这里要看的是「五类 × 两长度」的结构、引用与合规徽标。",
    ),
    (
        "kb",
        "快照里的检索是预渲染结果；问答输入框在本地才会真正调用 FTS5 索引。",
    ),
    (
        "assets",
        "素材是 demo 里真实渲染出的 PNG / MP4，路径指向站点内的静态副本。",
    ),
]

MEDIA_TAGS = ("img", "video", "audio", "source")


def newest_demo() -> Path:
    candidates = sorted((ROOT / "out").glob("demo-*"), key=lambda p: p.name)
    if not candidates:
        raise SystemExit("找不到 out/demo-*，请先跑 bash scripts/demo.sh")
    return candidates[-1]


def detail_slug(href: str) -> str:
    """``/copies?copy_id=copy_abc`` → ``copies-copy_abc.html``。"""
    path, _, query = href.partition("?")
    value = query.split("=", 1)[1] if "=" in query else query
    value = re.sub(r"[^A-Za-z0-9_]", "", value)
    return f"{path.strip('/')}-{value}.html"


def rewrite(html: str, detail_map: dict[str, str], home: str) -> str:
    from bs4 import BeautifulSoup

    # 先抹掉开发机路径：右上角的 WB_HOME 会原样出现在快照里
    html = html.replace(home, "demo 数据根目录")

    soup = BeautifulSoup(html, "html.parser")

    def localize(value: str) -> str | None:
        if not value or value.startswith(("http://", "https://", "#", "mailto:", "data:")):
            return None
        if value in detail_map:
            return detail_map[value]
        base = value.split("?")[0].split("#")[0]
        if value == "/static/app.css":
            return "../assets/console.css"
        if value.startswith("/static/"):
            return "../assets/" + value[len("/static/") :]
        if value.startswith("/files/"):
            return "../assets/files/" + value[len("/files/") :]
        if base in NAV:
            return NAV[base]
        return None

    for tag, attr in (("a", "href"), ("link", "href")):
        for node in soup.find_all(tag):
            value = node.get(attr)
            if value is None:
                continue
            new = localize(value)
            if new:
                node[attr] = new
            elif value.startswith("/"):
                node[attr] = "#"

    for tag in MEDIA_TAGS:
        for node in soup.find_all(tag):
            value = node.get("src")
            if value is None:
                continue
            new = localize(value)
            if new:
                node["src"] = new
            elif value.startswith("/"):
                del node["src"]

    for form in soup.find_all("form"):
        form["action"] = "#"
        form["onsubmit"] = "return false"
        classes = form.get("class") or []
        form["class"] = " ".join([*classes, "disabled-form"])
        note = soup.new_tag("p")
        note["class"] = "form-note"
        note.string = "静态快照：表单已禁用，请本地运行命令。"
        form.insert(0, note)

    body = soup.body
    if body is not None:
        body.insert(0, BeautifulSoup(BANNER, "html.parser"))

    head = soup.head
    if head is not None:
        head.append(soup.new_tag("link", rel="stylesheet", href="../assets/site.css"))

    return str(soup)


def add_page_note(html: str, name: str) -> str:
    from bs4 import BeautifulSoup

    for prefix, text in PAGE_NOTES:
        if not name.startswith(prefix):
            continue
        soup = BeautifulSoup(html, "html.parser")
        note = BeautifulSoup(f'<p class="page-note">{text}</p>', "html.parser")
        body = soup.body
        if body is not None:
            body.insert(1, note)
        return str(soup)
    return html


def collect_details(pages: dict[str, str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for html in pages.values():
        for href in re.findall(r'href="(/(?:copies|packs|topics)\?[^"]+)"', html):
            found.setdefault(href, detail_slug(href))
    return found


def copy_files(client, pages: dict[str, str], dist: Path) -> int:
    pattern = re.compile(r'"(?:\.\./assets/files/)([^"]+)"')
    wanted: set[str] = set()
    for html in pages.values():
        wanted.update(pattern.findall(html))

    count = 0
    for rel in sorted(wanted):
        response = client.get("/files/" + rel)
        if response.status_code != 200:
            print(f"  ! 资源缺失 /files/{rel} -> {response.status_code}")
            continue
        target = dist / "assets" / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        count += 1
    return count


def build_report(demo: Path, dist: Path) -> bool:
    report = demo / "report.md"
    if not report.exists():
        print("  ! demo 里没有 report.md")
        return False
    try:
        import markdown
    except ImportError:
        print(
            "  ! 缺少 markdown：python3 -m pip install --user -i "
            "https://pypi.tuna.tsinghua.edu.cn/simple markdown"
        )
        return False

    body = markdown.markdown(
        report.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    # 分组对比表列多，包一层可横向滚动的容器，窄屏不挤成竖排
    body = body.replace("<table>", '<div class="table-scroll"><table>')
    body = body.replace("</table>", "</table></div>")

    template = (SITE_SRC / "examples.html").read_text(encoding="utf-8")
    target = dist / "examples" / "report.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template.replace("<!--REPORT-->", body), encoding="utf-8")
    return True


def main() -> int:
    demo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else newest_demo()
    home = demo / ".wb"
    if not home.exists():
        raise SystemExit(f"{demo} 里没有 .wb，先确认这是 scripts/demo.sh 的产物")

    print(f"demo: {demo}")
    os.environ["WB_HOME"] = str(home)

    from fastapi.testclient import TestClient

    from workbench.web.app import create_app

    client = TestClient(create_app())

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    raw: dict[str, str] = {}
    for route, name in LIST_ROUTES:
        response = client.get(route)
        if response.status_code != 200:
            raise SystemExit(f"{route} 返回 {response.status_code}，快照失败")
        raw[name] = response.text

    details = collect_details(raw)
    print(f"  详情页 {len(details)} 个")

    pages: dict[str, str] = {}
    for href, name in details.items():
        response = client.get(href)
        if response.status_code != 200:
            print(f"  ! {href} -> {response.status_code}")
            continue
        pages[name] = add_page_note(rewrite(response.text, details, str(home)), name)

    for name, html in raw.items():
        pages[name] = add_page_note(rewrite(html, details, str(home)), name)

    for name, html in sorted(pages.items()):
        target = DIST / "console" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        print(f"  console/{name}  {len(html)} B")

    print(f"  assets/files  {copy_files(client, pages, DIST)} 个")

    shutil.copy(ROOT / "workbench" / "web" / "static" / "app.css", DIST / "assets" / "console.css")

    for item in SITE_SRC.rglob("*"):
        if not item.is_file() or item.name == "examples.html":
            continue  # examples.html 是模板，只经 build_report 落地
        rel = item.relative_to(SITE_SRC)
        target = DIST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(item, target)
    print("  src/ 已复制")

    for pattern, dest in (
        ("artifacts/**/*.png", "assets/poster.png"),
        ("artifacts/**/*.mp4", "assets/video.mp4"),
        ("packs/*.zip", "assets/pack.zip"),
    ):
        matches = sorted(demo.glob(pattern))
        if matches:
            shutil.copy(matches[0], DIST / dest)
            print(f"  {dest}  {matches[0].name}")

    build_report(demo, DIST)

    (DIST / ".nojekyll").write_text("", encoding="utf-8")
    total = sum(1 for path in DIST.rglob("*") if path.is_file())
    print(f"完成：{DIST}（{total} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
