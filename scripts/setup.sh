#!/usr/bin/env bash
# 安装开发环境：优先使用仓库内 .venv，venv 不可用时退回 pip --user。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"

echo "[setup] 仓库目录: $ROOT"
echo "[setup] Python: $($PYTHON --version 2>&1)"
echo "[setup] PyPI 镜像: $INDEX_URL"

if "$PYTHON" -m venv "$ROOT/.venv" >/dev/null 2>&1; then
  echo "[setup] 已创建/复用虚拟环境 .venv"
  PIP="$ROOT/.venv/bin/python -m pip"
  "$PIP" install -i "$INDEX_URL" --upgrade pip
  "$PIP" install -i "$INDEX_URL" -e ".[dev]"
  echo
  echo "[setup] 安装完成。后续命令："
  echo "  source .venv/bin/activate"
  echo "  bash scripts/check.sh"
  echo "  wb doctor"
else
  echo "[setup] 警告：python3 -m venv 不可用，退回用户级安装（pip --user）。" >&2
  echo "[setup] 若后续命令找不到，请确认 ~/.local/bin 在 PATH 中。" >&2
  # venv 失败可能留下没有 pip 的半成品，清掉以免误用。
  rm -rf "$ROOT/.venv"
  # 系统 pip 过旧时无法识别 PEP 660（editable）钩子，先升级构建工具。
  "$PYTHON" -m pip install -i "$INDEX_URL" --user --upgrade pip setuptools wheel
  "$PYTHON" -m pip install -i "$INDEX_URL" --user -e ".[dev]"
  echo
  echo "[setup] 安装完成。后续命令："
  echo "  bash scripts/check.sh"
  echo "  wb doctor"
fi
