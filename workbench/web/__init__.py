"""本地 Web 控制台：FastAPI + Jinja2，只读本地 store，无前端构建。

页面只调用 ``workbench.store.repo`` 与 ``workbench.pipeline.runner``，
不重写业务逻辑；写操作统一走 ``POST`` + 表单 + ``303`` 重定向，无 JS 也能用。

应用工厂在 :mod:`workbench.web.app`；CLI 入口见 ``workbench/cli_ext/serve.py``。
"""
