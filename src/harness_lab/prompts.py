from __future__ import annotations

from .cases import BenchmarkCase
from .config import HarnessConfig


REPORT_CONTRACT = """Return exactly one JSON object (no prose outside JSON) with this shape:
{
  "report_name": "stable_snake_case_id",
  "objective": "...",
  "grain": "...",
  "timezone": "IANA timezone or UTC",
  "population": {"include": ["..."], "exclude": ["..."]},
  "metrics": [
    {"id": "stable_metric_id", "definition": "...", "formula_sql": "...", "owner": "..."}
  ],
  "dimensions": ["..."],
  "filters": ["..."],
  "comparisons": ["..."],
  "delivery": {"format": "...", "cadence": "..."},
  "assumptions": ["..."],
  "open_questions": ["..."],
  "validation_checks": ["..."]
}
Use explicit canonical identifiers and formulas. Later corrections override earlier requests. Do not invent unavailable fields."""


SQL_CONTRACT = """Return exactly one JSON object (no prose outside JSON) with this shape:
{
  "diagnosis": "plan-based diagnosis",
  "optimized_sql": "one read-only SELECT/WITH statement, semantically equivalent to the input",
  "index_statements": ["zero or more single CREATE INDEX statements"],
  "tradeoffs": ["write/space/selectivity caveats"],
  "evidence": {
    "baseline_total_cost": 123.45,
    "candidate_total_cost": 45.67,
    "cost_ratio": 2.703,
    "plan_change": "important before/after nodes"
  }
}
Inspect schema, current indexes, and EXPLAIN before recommending changes. The three numeric evidence values must compare the original input SQL before changes against the exact optimized_sql with the proposed indexes, using an actual transactional candidate-index trial; do not estimate them. CREATE INDEX statements must target public benchmark tables and contain no comments or multiple statements. PostgreSQL has no portable query hints: prefer a justified rewrite/index and never invent extensions."""


BASELINE_SYSTEM = """You are a helpful data assistant with access to a PostgreSQL database. Solve the user's task. Use tools when useful. Follow the requested output format."""


POSTGRES_REPORT_SYSTEM = """You are a senior analytics requirements editor. Reconcile a long, noisy stakeholder dialogue into an executable report contract. Treat later explicit corrections as authoritative, distinguish unavailable data from assumptions, preserve owners/timezones/grain, and make every metric mechanically testable. Never silently repair missing source data by inventing it."""


POSTGRES_SQL_SYSTEM = """You are a PostgreSQL performance engineer. Work evidence-first: inspect schema and indexes, obtain EXPLAIN (FORMAT JSON), identify the dominant scan/join/sort, propose the smallest semantics-preserving change, and transactionally trial the exact final index/query before answering. Copy the measured baseline_total_cost, candidate_total_cost, and cost_ratio into evidence. Check sargability, column order, partial/expression/covering index choices, selectivity, tie-breakers, and write amplification. Do not claim improvement from intuition alone."""


def system_prompt(case: BenchmarkCase, config: HarnessConfig) -> str:
    if config.prompt_profile == "baseline":
        base = BASELINE_SYSTEM
    elif case.kind == "report":
        base = POSTGRES_REPORT_SYSTEM
    else:
        base = POSTGRES_SQL_SYSTEM

    if config.routing == "task_kind":
        route = (
            "Route: requirements synthesis. Database tools are unnecessary unless the task "
            "explicitly asks for data inspection."
            if case.kind == "report"
            else "Route: PostgreSQL diagnosis. Ground recommendations in database tool evidence."
        )
        base = f"{base}\n\n{route}"
    if config.prompt_patch:
        base = f"{base}\n\nHarness patch:\n{config.prompt_patch}"
    return base


def render_task(case: BenchmarkCase, context_override: str | None = None) -> str:
    if case.kind == "report":
        if context_override is None:
            dialogue = "\n".join(
                f"[{i:02d}] {turn.role}: {turn.content}"
                for i, turn in enumerate(case.dialogue, start=1)
            )
        else:
            dialogue = context_override
        return (
            f"Task: synthesize the final specification for «{case.title}».\n\n"
            f"Dialogue:\n{dialogue}\n\n{REPORT_CONTRACT}"
        )
    return (
        f"Task: {case.title}\n{case.question}\n\nInput SQL:\n{case.sql}\n\n"
        f"{SQL_CONTRACT}"
    )


def ledger_prompt(case: BenchmarkCase) -> str:
    dialogue = "\n".join(
        f"[{i:02d}] {turn.role}: {turn.content}"
        for i, turn in enumerate(case.dialogue, start=1)
    )
    return f"""Compile a requirements ledger from the dialogue below. This is a context-compaction stage, not the final answer.
Return JSON with keys: active_requirements, superseded_requirements, unavailable_data, open_questions, provenance.
Every active requirement must cite the last turn number that established it. Later explicit corrections win. Preserve exact metric IDs, thresholds, timezones, grain, exclusions, delivery, and owners.

{dialogue}"""


def planning_prompt(case: BenchmarkCase, task_text: str) -> str:
    focus = (
        "Plan how to reconcile corrections, map every requirement into the output contract, and audit unsupported fields."
        if case.kind == "report"
        else "Plan which schema/index/EXPLAIN evidence to collect and how to verify semantic equivalence."
    )
    return f"{focus}\nReturn a concise numbered plan, not the final answer.\n\n{task_text}"


def reviewer_prompt(case: BenchmarkCase, task_text: str, candidate: str) -> str:
    rubric = (
        "Check every late correction, metric identifier/formula, grain, timezone, population, missing-data caveat, delivery, and validation check."
        if case.kind == "report"
        else "Check read-only safety, semantic equivalence, current-index awareness, predicate/index column order, plan evidence, tie-breakers, and write/space trade-offs."
    )
    return f"""You are an isolated domain reviewer and reviser. {rubric}
Repair omissions and contradictions. Return only the complete revised final JSON object required by the task, without commentary or markdown fences.

ORIGINAL TASK
{task_text}

CANDIDATE
{candidate}"""
