"""流水线 runner：逐 stage 执行、写 job、落 manifest（ADR §6.1）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbench.config import Config, now_iso
from workbench.errors import MissingParam, PipelineFailed, WorkbenchError
from workbench.ids import new_id, new_run_id
from workbench.llm.base import LLMClient
from workbench.llm.factory import build_llm
from workbench.pipeline.definitions import missing_stage, stages_for
from workbench.pipeline.registry import get_stage
from workbench.pipeline.stage import RunContext
from workbench.stages import load_stages
from workbench.store import repo
from workbench.store.db import init_db
from workbench.store.models import Job


@dataclass(slots=True)
class RunResult:
    run_id: str
    pipeline: str
    out: Path
    manifest: dict[str, Any]
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)


def _write_manifest(out: Path, manifest: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__


def run_pipeline(
    cfg: Config,
    pipeline: str,
    *,
    params: dict[str, str] | None = None,
    out: Path | str | None = None,
    stop_after: str | None = None,
    llm: LLMClient | None = None,
    log: Callable[[str], None] | None = None,
) -> RunResult:
    """执行一条流水线。

    - 每个 stage 写一行 ``job``：running → done/failed。
    - stage 未实现 → 该行 failed，抛 ``MissingStage``（退出码 2）。
    - stage 抛异常 → 该行 failed，抛 ``PipelineFailed``。
    - 成功时写 ``<out>/manifest.json``；失败也会写一份带 error 的 manifest。
    """
    load_stages()
    stage_list = stages_for(pipeline)
    if stop_after is not None and stop_after not in stage_list:
        raise WorkbenchError(
            f"--stop-after {stop_after!r} 不在流水线 {pipeline} 中；可选：{', '.join(stage_list)}"
        )

    run_params = {str(key): str(value) for key, value in (params or {}).items()}
    cfg.ensure_dirs()
    client = llm if llm is not None else build_llm(cfg)
    run_id = new_run_id()
    out_path = (
        Path(out).expanduser().resolve()
        if out is not None
        else (cfg.runs_dir / run_id).resolve()
    )
    out_path.mkdir(parents=True, exist_ok=True)

    log_fn: Callable[[str], None] = log if log is not None else (lambda _message: None)
    conn = init_db(cfg.db_path)
    ctx = RunContext(
        run_id=run_id,
        pipeline=pipeline,
        home=cfg.home,
        out=out_path,
        db=conn,
        llm=client,
        cfg=cfg,
        params=run_params,
        log=log_fn,
    )
    started_at = now_iso(cfg.tz)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "pipeline": pipeline,
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "out": str(out_path),
        "home": str(cfg.home),
        "params": run_params,
        "stages": [],
    }
    input_ref = (
        json.dumps(run_params, ensure_ascii=False, separators=(",", ":")) if run_params else None
    )

    try:
        for stage_name in stage_list:
            job_id = new_id("job")
            repo.insert_job(
                conn,
                Job(
                    id=job_id,
                    pipeline=pipeline,
                    stage=stage_name,
                    input_ref=input_ref,
                    status="running",
                    started_at=now_iso(cfg.tz),
                ),
            )

            try:
                stage = get_stage(stage_name)
            except KeyError:
                error = missing_stage(pipeline, stage_name)
                repo.update_job(
                    conn,
                    job_id,
                    status="failed",
                    ended_at=now_iso(cfg.tz),
                    error=str(error),
                )
                raise error from None
            except Exception as exc:  # noqa: BLE001 - 注册表/实例化失败同样要落 job
                repo.update_job(
                    conn,
                    job_id,
                    status="failed",
                    ended_at=now_iso(cfg.tz),
                    error=_error_text(exc),
                )
                raise PipelineFailed(pipeline, stage_name, exc) from exc

            needs = tuple(getattr(stage, "needs", ()))
            missing_keys = [key for key in needs if key not in run_params]
            if missing_keys:
                error = MissingParam(pipeline, stage_name, missing_keys[0])
                repo.update_job(
                    conn,
                    job_id,
                    status="failed",
                    ended_at=now_iso(cfg.tz),
                    error=str(error),
                )
                raise error

            log_fn(f"[{pipeline}] {stage_name} 开始")
            try:
                output = stage.run(ctx)
                if not isinstance(output, dict):
                    raise TypeError(
                        f"stage 必须返回 dict，实际为 {type(output).__name__}"
                    )
                # 先验证 JSON 可序列化，避免 job 已标 done 但 output_ref 写不进去。
                json.dumps(output, ensure_ascii=False)
            except Exception as exc:
                repo.update_job(
                    conn,
                    job_id,
                    status="failed",
                    ended_at=now_iso(cfg.tz),
                    error=_error_text(exc),
                )
                if isinstance(exc, WorkbenchError):
                    raise
                raise PipelineFailed(pipeline, stage_name, exc) from exc

            repo.update_job(
                conn,
                job_id,
                status="done",
                ended_at=now_iso(cfg.tz),
                output_ref=output,
            )
            manifest["stages"].append(
                {"name": stage_name, "status": "done", "output": output}
            )
            log_fn(f"[{pipeline}] {stage_name} 完成")

            if stop_after == stage_name:
                break

        manifest["status"] = "done"
        manifest["ended_at"] = now_iso(cfg.tz)
        _write_manifest(out_path, manifest)
        log_fn(f"[{pipeline}] 完成，run_id={run_id}，输出：{out_path}")
        return RunResult(
            run_id=run_id,
            pipeline=pipeline,
            out=out_path,
            manifest=manifest,
            outputs={
                item["name"]: item["output"]
                for item in manifest["stages"]
                if item["status"] == "done"
            },
        )
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["ended_at"] = now_iso(cfg.tz)
        manifest["error"] = _error_text(exc)
        try:
            _write_manifest(out_path, manifest)
        except OSError:
            pass
        raise
    finally:
        conn.close()


__all__ = ["RunResult", "run_pipeline"]
