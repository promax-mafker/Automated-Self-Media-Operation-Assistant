#!/usr/bin/env bash
# 质量门：ruff 静态检查 + pytest（全部测试禁止联网）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/ruff" ]]; then
  RUFF="$ROOT/.venv/bin/ruff"
else
  RUFF="ruff"
fi
if [[ -x "$ROOT/.venv/bin/pytest" ]]; then
  PYTEST="$ROOT/.venv/bin/pytest"
else
  PYTEST="pytest"
fi

if ! command -v "$RUFF" >/dev/null 2>&1; then
  echo "[check] 找不到 ruff，请先运行 bash scripts/setup.sh" >&2
  exit 1
fi
if ! command -v "$PYTEST" >/dev/null 2>&1; then
  echo "[check] 找不到 pytest，请先运行 bash scripts/setup.sh" >&2
  exit 1
fi

echo "[check] ruff check ."
"$RUFF" check .

echo "[check] pytest -q"
"$PYTEST" -q
