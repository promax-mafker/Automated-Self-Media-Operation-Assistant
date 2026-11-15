#!/usr/bin/env bash
# T09 一键 demo：热点 → 选题 → 文案 → 海报 → 短视频 → 发布包 → 周报。
#
# - 完全离线：WB_LLM=stub（未显式设置时），不读任何平台凭据。
# - 数据隔离：WB_HOME=<out/demo-时间戳>/.wb，不碰默认 ./.wb。
# - 全部内容为虚构演示数据；不抓取、不登录、不发布。
# - 支持 `bash scripts/demo.sh --list <demo 目录>` 只检查产物清单。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi
# 始终使用当前仓库的 workbench，避免 PATH 上指向其他 checkout 的 wb。
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
WB=("$PYTHON" -m workbench.cli)
export WB_LLM="${WB_LLM:-stub}"
export WB_TZ="${WB_TZ:-Asia/Shanghai}"

usage() {
  cat <<'USAGE'
用法：
  bash scripts/demo.sh                 离线跑通完整链路，产物落 out/demo-<时间戳>/
  bash scripts/demo.sh --list <目录>   只检查某个 demo 目录的产物清单（不跑流水线）
  bash scripts/demo.sh --help          显示本帮助

环境变量：
  WB_LLM   默认 stub；显式传入其他值会原样传递（非法值会让 demo 失败）
  WB_TZ    默认 Asia/Shanghai
  PYTHON   默认 python3；存在 .venv/bin/python 时优先使用
USAGE
}

list_demo_artifacts() {
  local dir="${1:-}"
  if [[ -z "$dir" ]]; then
    echo "用法：bash scripts/demo.sh --list <demo 目录>" >&2
    return 2
  fi
  if [[ ! -d "$dir" ]]; then
    echo "目录不存在：$dir" >&2
    return 2
  fi

  local missing=0
  local name size count
  echo
  echo "== demo 产物清单 =="
  echo "目录：$dir"
  printf '%-34s %12s  %s\n' "产物" "大小" "说明"
  printf '%-34s %12s  %s\n' "----------------------------------" "------------" "--------------------------------"
  for name in wb.sqlite3 topics.json copies.json report.md; do
    if [[ -f "$dir/$name" ]]; then
      size="$(wc -c <"$dir/$name" | tr -d ' ')"
      printf '%-34s %12s  %s\n' "$name" "${size} B" "必有"
    else
      printf '%-34s %12s  %s\n' "$name" "-" "缺失"
      echo "缺少产物：$name" >&2
      missing=1
    fi
  done
  for name in packs artifacts; do
    if [[ -d "$dir/$name" ]]; then
      count="$(find "$dir/$name" -type f | wc -l | tr -d ' ')"
      printf '%-34s %12s  %s\n' "$name/" "${count} 个文件" "必有"
    else
      printf '%-34s %12s  %s\n' "$name/" "-" "缺失"
      echo "缺少目录：$name/" >&2
      missing=1
    fi
  done

  local png_count=0 mp4_count=0
  if [[ -d "$dir/artifacts" ]]; then
    png_count="$(find "$dir/artifacts" -type f -iname '*.png' | wc -l | tr -d ' ')"
    mp4_count="$(find "$dir/artifacts" -type f -iname '*.mp4' | wc -l | tr -d ' ')"
  fi
  printf '%-34s %12s  %s\n' "artifacts/**/*.png" "${png_count} 张" "至少 1 张"
  printf '%-34s %12s  %s\n' "artifacts/**/*.mp4" "${mp4_count} 个" "至少 1 个"
  if (( png_count < 1 )); then
    echo "缺少产物：artifacts 下没有 PNG" >&2
    missing=1
  fi
  if (( mp4_count < 1 )); then
    echo "缺少产物：artifacts 下没有 MP4" >&2
    missing=1
  fi

  echo
  if (( missing )); then
    echo "检查未通过：demo 产物不完整。" >&2
    return 1
  fi
  echo "检查通过：wb.sqlite3 / packs/ / artifacts/ / report.md / topics.json / copies.json 齐全，"
  echo "且至少包含 ${png_count} 张 PNG 与 ${mp4_count} 个 MP4。"
  return 0
}

run_demo() {
  local ts demo home week shift_days
  ts="$(date +%Y%m%d-%H%M%S)"
  demo="$ROOT/out/demo-$ts"
  home="$demo/.wb"
  mkdir -p "$demo"
  export WB_HOME="$home"

  echo "[demo] 仓库：$ROOT"
  echo "[demo] WB_HOME：$WB_HOME"
  echo "[demo] WB_LLM：$WB_LLM（stub 为确定性占位文案，不联网）"
  echo "[demo] 输出目录：$demo"
  echo

  echo "[1/8] 初始化数据库与知识库"
  "${WB[@]}" db init
  "${WB[@]}" kb add data/kb/seed

  echo
  echo "[2/8] 热点 → 选题（本地 fixture，10 条带证据的选题卡）"
  "${WB[@]}" topic suggest --count 10 --out "$WB_HOME/runs/topic"

  echo
  echo "[3/8] 选题 → 文案（5 类 × 2 长度 = 10 条）"
  local topic_id copy_id
  topic_id="$("$PYTHON" - "$WB_HOME/runs/topic/topics.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
topics = payload.get("topics") or []
if not topics:
    raise SystemExit("topics.json 里没有选题")
print(topics[0]["id"])
PY
)"
  "${WB[@]}" copy gen --topic "$topic_id" --out "$WB_HOME/runs/copy"
  copy_id="$("$PYTHON" - "$WB_HOME/runs/copy/copies.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
copies = payload.get("copies") or []
preferred = [item for item in copies if item.get("kind") == "note" and item.get("length") == "long"]
chosen = (preferred or copies or [None])[0]
if chosen is None:
    raise SystemExit("copies.json 里没有文案")
print(chosen["id"])
PY
)"

  echo
  echo "[4/8] 文案 → 海报（Pillow 声明式模板）"
  "$PYTHON" - "$WB_HOME/runs/topic/topics.json" "$demo/poster-data.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    topics = json.load(handle).get("topics") or []
if not topics:
    raise SystemExit("topics.json 里没有选题")
topic = topics[0]
points = [
    str(item.get("note") or "")
    for item in (topic.get("evidence") or [])
    if str(item.get("note") or "").strip()
][:4]
if not points:
    points = [str(topic.get("hook") or "演示海报（虚构）")]
payload = {
    "slots": {
        "kicker": "demo",
        "title": topic.get("title") or "演示选题（虚构）",
        "subtitle": topic.get("hook") or "",
        "body": topic.get("hook") or "",
        "points": points,
        "tags": ["演示", "虚构"],
        "author": "演示账号（虚构）",
        "footer": "全部为虚构演示数据，不是平台真实数据",
    },
    "meta": {"source": "scripts/demo.sh", "note": "demo 演示数据，非平台真实数据"},
}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
  "${WB[@]}" run visual \
    --input template=poster-checklist \
    --input copy_set_id="$copy_id" \
    --input data="$demo/poster-data.json" \
    --out "$WB_HOME/artifacts/visual"

  echo
  echo "[5/8] 文案 → 短视频（卡片分镜，无 BGM、无数字人）"
  "$PYTHON" - "$WB_HOME/runs/topic/topics.json" "$demo/storyboard.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    topics = json.load(handle).get("topics") or []
if not topics:
    raise SystemExit("topics.json 里没有选题")
topic = topics[0]
points = [
    str(item.get("note") or "")
    for item in (topic.get("evidence") or [])
    if str(item.get("note") or "").strip()
][:5]
if not points:
    points = [str(topic.get("hook") or "先跑通骨架，再填业务")]
payload = {
    "title": topic.get("title") or "演示选题（虚构）",
    "tts": False,
    "scenes": [
        {"type": "title", "text": topic.get("title") or "演示选题（虚构）", "seconds": 4},
        {
            "type": "points",
            "text": topic.get("angle") or "这条流水线的四个动作",
            "points": points,
            "seconds": 9,
        },
        {"type": "cta", "text": "先跑通一条流水线，再谈放大", "seconds": 4},
    ],
}
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
  "${WB[@]}" run video \
    --input storyboard="$demo/storyboard.json" \
    --input copy_set_id="$copy_id" \
    --out "$WB_HOME/artifacts/video"

  echo
  echo "[6/8] 文案 + 素材 → 发布包（只导出，不发布）"
  "${WB[@]}" pack build --copy "$copy_id"

  echo
  echo "[7/8] 平台导出 → 周报（数据为 data/seed 的虚构 CSV，日期平移到本周）"
  shift_days="$("$PYTHON" - "$ROOT/data/seed/metrics_sample.csv" "$demo/metrics-demo.csv" "${WB_TZ}" demo_xhs <<'PY'
import csv
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

source, target, tz, account = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
zone = ZoneInfo(tz)
today = datetime.now(zone).date()
monday = today - timedelta(days=today.weekday())
anchor = date(2026, 11, 2)  # data/seed 里「本周」的周一（2026-W45）
delta = monday - anchor

with open(source, encoding="utf-8", newline="") as handle:
    all_rows = list(csv.DictReader(handle))
# 只取自己账号的行：对标账号已由 topic 流水线用另一套 handle 导入，
# 同 post_id 换账号会触发 T02 的导入保护。
rows = [row for row in all_rows if (row.get("account") or "").strip() == account]
if not rows:
    raise SystemExit(f"seed CSV 里没有账号 {account} 的数据")
for row in rows:
    for key in ("published_at", "captured_at"):
        value = (row.get(key) or "").strip()
        if value:
            row[key] = (datetime.fromisoformat(value) + delta).isoformat()
with open(target, "w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(delta.days)
PY
)"
  week="$("$PYTHON" - "${WB_TZ}" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo(sys.argv[1]))
iso = now.isocalendar()
print(f"{iso.year}-W{iso.week:02d}")
PY
)"
  echo "[demo] 日期平移 ${shift_days} 天；本周 ${week}"
  "${WB[@]}" review import --csv "$demo/metrics-demo.csv" --account demo_xhs --platform xhs
  "${WB[@]}" review week --account demo_xhs --week "$week" --platform xhs \
    --out "$WB_HOME/artifacts/report"

  echo
  echo "[8/8] 汇总产物到 $demo"
  mkdir -p "$demo/artifacts" "$demo/packs"
  cp -R "$WB_HOME/artifacts/." "$demo/artifacts/"
  cp -R "$WB_HOME/packs/." "$demo/packs/"
  cp "$WB_HOME/runs/topic/topics.json" "$demo/topics.json"
  cp "$WB_HOME/runs/copy/copies.json" "$demo/copies.json"
  cp "$WB_HOME/artifacts/report/report.md" "$demo/report.md"
  "$PYTHON" - "$WB_HOME/wb.sqlite3" "$demo/wb.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    with target:
        source.backup(target)
finally:
    target.close()
    source.close()
PY

  list_demo_artifacts "$demo"

  echo
  echo "接下来看控制台（只读本地 store）："
  echo "  WB_HOME=\"$WB_HOME\" wb serve --port 8077"
  echo "  浏览器打开 http://127.0.0.1:8077/"
  echo
  echo "说明：demo 内容全部是 data/seed 与 stub 模型生成的虚构演示数据，不代表任何真实账号表现。"
}

case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --list)
    if [[ $# -lt 2 || -z "${2:-}" ]]; then
      echo "用法：bash scripts/demo.sh --list <demo 目录>" >&2
      exit 2
    fi
    list_demo_artifacts "$2"
    exit $?
    ;;
  "")
    run_demo
    ;;
  *)
    echo "未知参数：$1" >&2
    usage >&2
    exit 2
    ;;
esac
