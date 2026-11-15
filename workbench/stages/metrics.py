"""T02 stage：``metrics_import`` 与 ``review_report``（review 流水线）。"""

from __future__ import annotations

from typing import Any

from workbench.metrics.import_csv import import_metrics
from workbench.metrics.report import write_weekly_report
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext


@register("metrics_import")
class MetricsImport:
    name = "metrics_import"
    needs: tuple[str, ...] = ("csv", "account")

    def run(self, ctx: RunContext) -> dict[str, Any]:
        result = import_metrics(
            ctx.db,
            ctx.params["csv"],
            account=ctx.params["account"],
            platform=ctx.params.get("platform") or ctx.cfg.platform,
            tz=ctx.cfg.tz,
        )
        ctx.log(
            "metrics_import: "
            f"imported={result.imported} accounts={result.accounts} skipped={result.skipped}"
        )
        return {
            "imported": result.imported,
            "skipped": result.skipped,
            "accounts": result.accounts,
            "posts_created": result.posts_created,
            "posts_existing": result.posts_existing,
            "handles": list(result.handles),
        }


@register("review_report")
class ReviewReport:
    name = "review_report"
    needs: tuple[str, ...] = ("account", "week")

    def run(self, ctx: RunContext) -> dict[str, Any]:
        result = write_weekly_report(
            ctx.db,
            ctx.params["account"],
            ctx.params["week"],
            ctx.out,
            platform=ctx.params.get("platform") or ctx.cfg.platform,
            tz=ctx.cfg.tz,
        )
        ctx.log(f"review_report: 写入 {result.path.name}")
        return {
            "report": str(result.path),
            "bytes": result.path.stat().st_size,
            "posts": result.posts,
            "actions": result.actions,
            "anomalies": result.anomalies,
        }


__all__ = ["MetricsImport", "ReviewReport"]
