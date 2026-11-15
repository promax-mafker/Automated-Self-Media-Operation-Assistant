"""Markdown 周报渲染与「下周三条动作」生成（T02）。

周报固定五节：概览 / 指标表 / 分组对比 / Top5 与 Bottom5 / 下周三条动作。
所有动作都必须引用具体数字，并指向一个作品 id 或一个分组名；样本不足的分组不参与推荐。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from workbench.metrics.compute import (
    GroupSummary,
    PostRecord,
    WeeklyData,
    build_weekly_data,
)


@dataclass(frozen=True, slots=True)
class ReportResult:
    path: Path
    text: str
    posts: int
    actions: int
    anomalies: int


def _int(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}"


def _num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


def _hours(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f} 小时"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _usable(groups: list[GroupSummary]) -> list[GroupSummary]:
    return [group for group in groups if not group.insufficient]


def build_actions(data: WeeklyData) -> list[str]:
    """生成三条「做什么 + 依据哪个数字」的动作；样本不足的分组不参与。"""
    actions: list[str] = []
    summary = data.summary

    slots = [
        group
        for group in _usable(data.groups["time_slot"])
        if group.mean_interaction_rate is not None
    ]
    if len(slots) >= 2:
        best = max(slots, key=lambda group: group.mean_interaction_rate or 0.0)
        worst = min(slots, key=lambda group: group.mean_interaction_rate or 0.0)
        diff = (best.mean_interaction_rate or 0.0) - (worst.mean_interaction_rate or 0.0)
        actions.append(
            f"把下周 3 篇内容的发布时段从「{worst.key}」移到「{best.key}」："
            f"{best.key}时段 {best.n} 篇平均 interaction_rate "
            f"{_pct(best.mean_interaction_rate)}，{worst.key}时段 {worst.n} 篇仅 "
            f"{_pct(worst.mean_interaction_rate)}，相差 {diff * 100:.2f} 个百分点。"
        )
    elif len(slots) == 1:
        best = slots[0]
        actions.append(
            f"把下周 3 篇内容集中到「{best.key}」发布：该时段 {best.n} 篇平均 "
            f"interaction_rate {_pct(best.mean_interaction_rate)}，"
            f"本周均值为 {_pct(summary.mean_interaction_rate)}。"
        )
    else:
        top = data.top[0]
        actions.append(
            f"下周先复盘 {top.post.id} 的发布节奏：本周 {summary.publish_count} 篇作品的"
            f"发布间隔中位数为 {_hours(summary.publish_gap_hours)}，"
            "先把间隔压到 48 小时以内。"
        )

    durations = [
        group
        for group in _usable(data.groups["duration"])
        if group.key != "无时长" and group.mean_completion_rate is not None
    ]
    if durations:
        best = max(durations, key=lambda group: group.mean_completion_rate or 0.0)
        week_mean = summary.mean_completion_rate
        if week_mean is not None:
            diff = (best.mean_completion_rate or 0.0) - week_mean
            relation = "高" if diff >= 0 else "低"
            actions.append(
                f"把下周 3 条视频统一到「{best.key}」：该组 {best.n} 条平均 completion_rate "
                f"{_pct(best.mean_completion_rate)}，比本周均值 {_pct(week_mean)} "
                f"{relation} {abs(diff) * 100:.2f} 个百分点。"
            )
        else:
            actions.append(
                f"把下周 3 条视频统一到「{best.key}」：该组 {best.n} 条平均 completion_rate "
                f"{_pct(best.mean_completion_rate)}，是本周最好的时长分组。"
            )
    else:
        contents = [
            group
            for group in _usable(data.groups["content_type"])
            if group.mean_save_rate is not None
        ]
        if len(contents) >= 2:
            best = max(contents, key=lambda group: group.mean_save_rate or 0.0)
            worst = min(contents, key=lambda group: group.mean_save_rate or 0.0)
            diff = (best.mean_save_rate or 0.0) - (worst.mean_save_rate or 0.0)
            actions.append(
                f"下周把内容重心放在「{best.key}」：该组 {best.n} 篇平均 save_rate "
                f"{_pct(best.mean_save_rate)}，{worst.key}组 {worst.n} 篇为 "
                f"{_pct(worst.mean_save_rate)}，相差 {diff * 100:.2f} 个百分点。"
            )
        elif len(contents) == 1:
            best = contents[0]
            actions.append(
                f"下周把内容重心放在「{best.key}」：该组 {best.n} 篇平均 save_rate "
                f"{_pct(best.mean_save_rate)}，本周均值为 {_pct(summary.mean_save_rate)}。"
            )
        else:
            top = data.top[0]
            actions.append(
                f"下周按 {top.post.id} 的结构产出 1 篇：该作品 views {_int(top.views)}，"
                f"本周中位数为 {_int(summary.median_views)}。"
            )

    if data.anomalies:
        record, score = max(data.anomalies, key=lambda item: item[1])
        if record.burst_ratio is not None:
            detail = (
                f"爆款率 {record.burst_ratio:.2f}"
                f"（为账号历史 P90 的 {record.burst_ratio:.2f} 倍）"
            )
        else:
            detail = f"z-score {score:.2f}"
        actions.append(
            f"复刻 {record.post.id} 的选题结构：views {_int(record.views)}，{detail}，"
            "下周产出 1 篇同结构内容。"
        )
    else:
        record = data.top[0]
        ratio = record.views / summary.median_views if summary.median_views else None
        actions.append(
            f"复盘 {record.post.id}：views {_int(record.views)} 为本周中位数 "
            f"{_int(summary.median_views)} 的 {_num(ratio)} 倍，下周产出 1 篇同结构内容。"
        )

    while len(actions) < 3:
        record = data.top[0]
        actions.append(
            f"继续复盘 {record.post.id}：本周 views {_int(record.views)}，"
            "下周产出 1 篇同结构内容。"
        )
    return actions[:3]


def _metrics_table(data: WeeklyData) -> list[str]:
    summary = data.summary
    rows = [
        ("发布量", f"{summary.publish_count} 篇", "本周发布的作品数"),
        ("views 中位数", _int(summary.median_views), "本周作品 views 的中位数"),
        ("views P90（本周）", _int(summary.p90_views), "本周作品 views 的 P90（线性插值）"),
        (
            "interaction_rate 均值",
            _pct(summary.mean_interaction_rate),
            "(likes+comments+saves+shares)/views，先按作品算再平均",
        ),
        ("save_rate 均值", _pct(summary.mean_save_rate), "saves/views，先按作品算再平均"),
        ("follow_rate 均值", _pct(summary.mean_follow_rate), "follows/views，先按作品算再平均"),
        (
            "completion_rate 均值",
            _pct(summary.mean_completion_rate),
            "完播率均值；缺失时用 avg_watch_sec/duration_sec 估算",
        ),
        (
            "burst_ratio 均值",
            _num(summary.mean_burst_ratio),
            "views/P90(同账号历史)，先按作品算再平均",
        ),
        (
            "publish_gap 中位数",
            _hours(summary.publish_gap_hours),
            "本周相邻作品发布时间间隔的中位数",
        ),
        (
            "异常作品数",
            f"{summary.anomaly_count} 篇",
            "views 的 z-score ≥ 2 或 ≤ -2（总体标准差）",
        ),
    ]
    lines = ["| 指标 | 本周数值 | 口径 |", "| --- | --- | --- |"]
    lines.extend(f"| {name} | {value} | {rule} |" for name, value, rule in rows)
    return lines


def _group_table(groups: list[GroupSummary]) -> list[str]:
    lines = [
        "| 分组 | 样本量 | views 中位数 | interaction_rate 均值 | "
        "save_rate 均值 | completion_rate 均值 | burst_ratio 均值 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group in groups:
        lines.append(
            f"| {group.key} | {group.n} | {_int(group.median_views)} | "
            f"{_pct(group.mean_interaction_rate)} | {_pct(group.mean_save_rate)} | "
            f"{_pct(group.mean_completion_rate)} | {_num(group.mean_burst_ratio)} | "
            f"{'样本不足' if group.insufficient else ''} |"
        )
    return lines


def _post_table(records: list[PostRecord]) -> list[str]:
    lines = [
        "| # | 作品 ID | 标题 | 发布时段 | views | interaction_rate |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, record in enumerate(records, start=1):
        lines.append(
            f"| {index} | {record.post.id} | {_cell(record.post.title)} | "
            f"{record.time_slot} | {_int(record.views)} | {_pct(record.interaction_rate)} |"
        )
    return lines


def render_report(data: WeeklyData, actions: list[str] | None = None) -> str:
    """渲染完整周报；``actions`` 为空时按当前数据重新生成。"""
    if actions is None:
        actions = build_actions(data)
    summary = data.summary
    account = data.account
    lines: list[str] = [
        f"# 账号周报 · {account.handle} · {data.week.key()}",
        "",
        f"> 数据来源：本地 `metric` 表（平台导出 CSV 导入）。时区：`{data.tz}`。",
        "",
        "## 概览",
        "",
        f"- 账号：{account.handle}（{account.display_name or '未填写名称'}）",
        f"- 平台：{data.platform}",
        f"- 周：{data.week.label()}",
        f"- 发布量：{summary.publish_count} 篇",
        f"- 总 views：{_int(summary.total_views)}",
        f"- 异常作品：{summary.anomaly_count} 篇（|z-score| ≥ 2）",
        f"- 历史样本：{summary.history_count} 篇（用于计算 P90 与爆款率）",
        f"- 缺指标作品：{data.missing_metric_posts} 篇（未计入）",
        "",
        "## 指标表",
        "",
        *_metrics_table(data),
        "",
        "## 分组对比",
        "",
        "### 按内容类型",
        "",
        *_group_table(data.groups["content_type"]),
        "",
        "### 按发布时段",
        "",
        "早 = 06:00–11:59，午 = 12:00–17:59，晚 = 18:00–23:59，深夜 = 00:00–05:59。",
        "",
        *_group_table(data.groups["time_slot"]),
        "",
        "### 按时长",
        "",
        "≤15s = 0–15 秒，15–30s = 15–30 秒（不含 15），30–60s = 30–60 秒（不含 30），"
        ">60s = 超过 60 秒；图文与文章计入「无时长」。",
        "",
        *_group_table(data.groups["duration"]),
        "",
        "## Top5 与 Bottom5",
        "",
        "### Top5（按 views 降序）",
        "",
        *_post_table(data.top),
        "",
        "### Bottom5（按 views 升序）",
        "",
        *_post_table(data.bottom),
        "",
        "## 下周三条动作",
        "",
    ]
    lines.extend(f"{index}. {action}" for index, action in enumerate(actions, start=1))
    lines.append("")
    return "\n".join(lines)


def write_weekly_report(
    conn: sqlite3.Connection,
    account_handle: str,
    week_value: str,
    out_dir: Path | str,
    *,
    platform: str,
    tz: str = "Asia/Shanghai",
) -> ReportResult:
    """计算并写出 ``<out_dir>/report.md``。"""
    data = build_weekly_data(
        conn,
        account_handle,
        week_value,
        platform=platform,
        tz=tz,
    )
    actions = build_actions(data)
    text = render_report(data, actions)
    target = Path(out_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    path = target / "report.md"
    path.write_text(text, encoding="utf-8")
    return ReportResult(
        path=path,
        text=text,
        posts=len(data.rows),
        actions=len(actions),
        anomalies=len(data.anomalies),
    )


__all__ = ["ReportResult", "build_actions", "render_report", "write_weekly_report"]
