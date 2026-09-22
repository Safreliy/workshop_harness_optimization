from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class DialogueTurn(BaseModel):
    role: str
    content: str


class BenchmarkCase(BaseModel):
    id: str
    kind: Literal["report", "sql"]
    title: str
    tags: list[str] = Field(default_factory=list)
    dialogue: list[DialogueTurn] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    question: str = ""
    sql: str = ""
    expected_index_terms: list[str] = Field(default_factory=list)
    expected_sql_terms: list[str] = Field(default_factory=list)
    min_cost_ratio: float = 1.0


def load_cases(
    benchmark_dir: str | Path = "benchmarks",
    split: Literal["train", "eval", "all"] = "train",
    kinds: set[str] | None = None,
) -> list[BenchmarkCase]:
    root = Path(benchmark_dir)
    splits = ["train", "eval"] if split == "all" else [split]
    selected = kinds or {"report", "sql"}
    cases: list[BenchmarkCase] = []
    for split_name in splits:
        for kind in ("report", "sql"):
            if kind not in selected:
                continue
            path = root / f"{kind}_{split_name}.jsonl"
            if not path.exists():
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    cases.append(BenchmarkCase.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f"invalid case at {path}:{line_number}: {exc}") from exc
    return cases


def public_case_payload(case: BenchmarkCase) -> dict[str, Any]:
    """Return only information the agent is allowed to see (never grader checks)."""
    if case.kind == "report":
        return {
            "id": case.id,
            "kind": case.kind,
            "title": case.title,
            "dialogue": [turn.model_dump() for turn in case.dialogue],
        }
    return {
        "id": case.id,
        "kind": case.kind,
        "title": case.title,
        "question": case.question,
        "sql": case.sql,
    }


def dump_public_case(case: BenchmarkCase) -> str:
    return json.dumps(public_case_payload(case), ensure_ascii=False, indent=2)
