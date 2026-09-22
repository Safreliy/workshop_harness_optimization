from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .agent import HarnessAgent
from .cases import BenchmarkCase
from .config import HarnessConfig
from .graders import grade_case
from .model import ModelClient
from .settings import Settings
from .tools import ToolExecutor
from .db import PostgresSandbox
from .trajectory import grade_trajectory


@dataclass
class BenchmarkRecord:
    case_id: str
    kind: str
    title: str
    score: float
    passed: bool
    output_score: float
    output_passed: bool
    trajectory_score: float
    trajectory_passed: bool
    trajectory_details: dict[str, Any]
    output: str
    grade_details: dict[str, Any]
    llm_calls: int
    input_tokens: int
    output_tokens: int
    error: str | None
    events: list[dict[str, Any]]


@dataclass
class RunnerResult:
    run_dir: Path
    records: list[BenchmarkRecord]
    summary: dict[str, Any]


def config_fingerprint(config: HarnessConfig) -> str:
    payload = config.model_dump(mode="json", exclude={"name"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:10]


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:80]


def summarize(records: list[BenchmarkRecord], config: HarnessConfig) -> dict[str, Any]:
    kinds: dict[str, Any] = {}
    for kind in sorted({record.kind for record in records}):
        selected = [record for record in records if record.kind == kind]
        kinds[kind] = {
            "cases": len(selected),
            "score": round(mean(record.score for record in selected), 4),
            "pass_rate": round(mean(float(record.passed) for record in selected), 4),
            "output_score": round(mean(record.output_score for record in selected), 4),
            "trajectory_score": round(
                mean(record.trajectory_score for record in selected), 4
            ),
        }
    return {
        "config": config.name,
        "config_fingerprint": config_fingerprint(config),
        "cases": len(records),
        "score": round(mean(record.score for record in records), 4) if records else 0.0,
        "pass_rate": round(mean(float(record.passed) for record in records), 4)
        if records
        else 0.0,
        "output_score": round(mean(record.output_score for record in records), 4)
        if records
        else 0.0,
        "trajectory_score": round(
            mean(record.trajectory_score for record in records), 4
        )
        if records
        else 0.0,
        "avg_llm_calls": round(mean(record.llm_calls for record in records), 3)
        if records
        else 0.0,
        "input_tokens": sum(record.input_tokens for record in records),
        "output_tokens": sum(record.output_tokens for record in records),
        "by_kind": kinds,
    }


def run_benchmark(
    *,
    settings: Settings,
    config: HarnessConfig,
    cases: list[BenchmarkCase],
    artifact_root: str | Path = "artifacts/runs",
    run_label: str = "benchmark",
    model: ModelClient | None = None,
) -> RunnerResult:
    model_client = model or ModelClient(settings)
    database = PostgresSandbox(settings.pg_dsn)
    agent = HarnessAgent(model_client, ToolExecutor(database))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_name = (
        f"{timestamp}-{_safe_name(run_label)}-{_safe_name(config.name)}-"
        f"{config_fingerprint(config)}"
    )
    run_dir = Path(artifact_root) / run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    config.to_yaml(run_dir / "config.yaml")

    records: list[BenchmarkRecord] = []
    records_path = run_dir / "records.jsonl"
    with records_path.open("w", encoding="utf-8") as output_file:
        for case in cases:
            agent_result = agent.run(case, config)
            grade = grade_case(case, agent_result.output, settings.pg_dsn)
            trajectory = grade_trajectory(case, agent_result)
            combined_score = round(0.75 * grade.score + 0.25 * trajectory.score, 4)
            record = BenchmarkRecord(
                case_id=case.id,
                kind=case.kind,
                title=case.title,
                score=combined_score,
                passed=grade.passed and trajectory.passed,
                output_score=grade.score,
                output_passed=grade.passed,
                trajectory_score=trajectory.score,
                trajectory_passed=trajectory.passed,
                trajectory_details=trajectory.details,
                output=agent_result.output,
                grade_details=grade.details,
                llm_calls=agent_result.llm_calls,
                input_tokens=agent_result.input_tokens,
                output_tokens=agent_result.output_tokens,
                error=agent_result.error,
                events=[asdict(event) for event in agent_result.events],
            )
            records.append(record)
            output_file.write(
                json.dumps(asdict(record), ensure_ascii=False, default=str) + "\n"
            )
            output_file.flush()

    summary = summarize(records, config)
    summary.update(
        {
            "model": settings.model_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_label": run_label,
        }
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return RunnerResult(run_dir=run_dir, records=records, summary=summary)
