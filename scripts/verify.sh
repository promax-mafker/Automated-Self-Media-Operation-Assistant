#!/usr/bin/env bash
# T09 质量门：check.sh + demo.sh + wb doctor；任一失败退出码非 0。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "[verify] 1/3 bash scripts/check.sh"
bash scripts/check.sh

echo
echo "[verify] 2/3 bash scripts/demo.sh"
bash scripts/demo.sh

echo
echo "[verify] 3/3 wb doctor"
"$PYTHON" -m workbench.cli doctor

echo
echo "[verify] 全部通过：ruff + pytest + demo + doctor"
