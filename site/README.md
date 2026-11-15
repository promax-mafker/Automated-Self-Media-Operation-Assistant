# 站点（GitHub Pages）

本项目站点：<https://promax-mafker.github.io/Automated-Self-Media-Operation-Assistant/>

- 落地页、样例页的源在 `site/src/`（纯 HTML + CSS，无构建链）。
- 控制台快照与样例产物由 `site/build.py` 从一次真实 demo 渲染出来，产物在 `site/dist/`（不进 main）。
- 发布：把 `site/dist/` 推到 `gh-pages` 分支，GitHub Pages 从该分支根目录提供静态文件。

## 重新生成

```sh
bash scripts/demo.sh                 # 先有一份 demo 数据（out/demo-<时间戳>/）
python3 -m pip install --user -i https://pypi.tuna.tsinghua.edu.cn/simple markdown
python3 site/build.py                # 默认取最新的 out/demo-*
```

产物：

```
site/dist/
  index.html              落地页（src/ 原样复制）
  examples/report.html    demo 的 report.md 渲染而成
  console/*.html          控制台六页快照（链接、图片、表单已改写为静态）
  assets/                 site.css / console.css / poster.png / video.mp4 / pack.zip / files/**
  .nojekyll               跳过 Jekyll，避免下划线路径被吞
```

## 发布

```sh
# 用 dist 的内容替换 gh-pages 分支（保留历史用 --force 覆盖单提交亦可）
git worktree add --detach /tmp/ghp
cd /tmp/ghp && git checkout --orphan gh-pages
git rm -rf . >/dev/null 2>&1 || true
cp -r <仓库>/site/dist/. .
git add -A && git commit -m "chore(pages): 更新站点" && git push origin gh-pages --force
```

首次发布后，在仓库 Settings → Pages 里确认 Source 为 `gh-pages` 分支根目录
（或调用 `POST /repos/{owner}/{repo}/pages` 指定 `source.branch=gh-pages`、`path=/`）。

## 约定

- 站内不要出现真实账号名、真实数据；演示一律 `demo_` 前缀或带「（虚构）」。
- 不在站点里承诺「自动发布」「自动抓取」——产品边界见根目录 `README.md`。
- 控制台快照是只读的，表单已禁用；不要在快照里放可提交的写操作。
