from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .agent import AgentResult
from .cases import BenchmarkCase


@dataclass
class TrajectoryGrade:
    score: float
    passed: bool
    details: dict[str, Any]


def _tool_events(result: AgentResult) -> list[dict[str, Any]]:
    return [event.data for event in result.events if event.stage == "tool"]


def grade_trajectory(case: BenchmarkCase, result: AgentResult) -> TrajectoryGrade:
    tools = _tool_events(result)
    names = [event.get("name", "") for event in tools]
    issues: list[str] = []

    failed_tools = 0
    for event in tools:
        try:
            payload = json.loads(event.get("output", "{}"))
            failed_tools += int(payload.get("ok") is False)
        except (json.JSONDecodeError, TypeError):
            failed_tools += 1
    signatures = [
        (event.get("name"), event.get("arguments"))
        for event in tools
    ]
    redundant_calls = len(signatures) - len(set(signatures))
    forced_final = any(event.stage == "forced_final" for event in result.events)

    if case.kind == "report":
        tool_discipline = max(0.0, 1.0 - 0.2 * len(tools))
        if tools:
            issues.append("report_unnecessary_database_tools")
        if result.llm_calls <= 2:
            efficiency = 1.0
        elif result.llm_calls <= 4:
            efficiency = 0.7
        else:
            efficiency = 0.3
            issues.append("excessive_llm_calls")
        score = 0.65 * tool_discipline + 0.35 * efficiency
    else:
        required = ["describe_table", "explain_query", "trial_index_plan"]
        coverage = sum(name in names for name in required) / len(required)
        if "describe_table" not in names:
            issues.append("missing_schema_inspection")
        if "explain_query" not in names:
            issues.append("missing_baseline_explain")
        if "trial_index_plan" not in names:
            issues.append("missing_transactional_index_trial")

        ordering = 1.0
        if "explain_query" in names and "describe_table" in names:
            ordering *= float(names.index("describe_table") < names.index("explain_query"))
        if "trial_index_plan" in names and "explain_query" in names:
            ordering *= float(names.index("explain_query") < names.index("trial_index_plan"))
        if ordering == 0:
            issues.append("evidence_steps_out_of_order")

        sample_calls = names.count("run_readonly_query")
        discipline = max(0.0, 1.0 - 0.15 * max(sample_calls - 2, 0))
        discipline = max(0.0, discipline - 0.2 * redundant_calls - 0.25 * failed_tools)
        if sample_calls > 2:
            issues.append("excessive_sample_queries")
        if redundant_calls:
            issues.append("redundant_tool_calls")
        if failed_tools:
            issues.append("tool_errors")

        if result.llm_calls <= 5:
            efficiency = 1.0
        elif result.llm_calls <= 8:
            efficiency = 0.7
        else:
            efficiency = 0.4
            issues.append("excessive_llm_calls")
        score = 0.45 * coverage + 0.15 * ordering + 0.25 * discipline + 0.15 * efficiency

    if forced_final:
        score -= 0.12
        issues.append("forced_final_after_step_budget")
    if result.error:
        score -= 0.3
        issues.append("agent_error")
    score = round(min(max(score, 0.0), 1.0), 4)
    return TrajectoryGrade(
        score=score,
        passed=score >= 0.7,
        details={
            "issues": sorted(set(issues)),
            "llm_calls": result.llm_calls,
            "tool_calls": len(tools),
            "tool_sequence": names,
            "failed_tools": failed_tools,
            "redundant_calls": redundant_calls,
            "forced_final": forced_final,
        },
    )
