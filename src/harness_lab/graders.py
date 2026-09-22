from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .cases import BenchmarkCase
from .db import ExplainResult, normalize_readonly_query


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("no valid JSON object found")


def norm(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value).lower().replace("ё", "е")
    text = re.sub(r"[_×/–—-]+", " ", text)
    # Small, explicit bilingual ontology for deterministic (non-LLM) grading.
    replacements = {
        r"понедель\w*": "monday",
        r"вторник\w*": "tuesday",
        r"пятниц\w*": "friday",
        r"налог\w*": "tax",
        r"тест\w*": "test",
        r"уникальн\w*": "distinct",
        r"заказ\w*": "order",
        r"предыдущ\w*": "previous",
        r"текущ\w*": "current",
        r"оплачен\w*": "paid",
        r"ежеднев\w*": "daily",
        r"недел\w*": "week",
        r"месяц\w*": "month",
        r"week\s+over\s+week": "wow",
        r"is\s+test": "test",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[^\w@>=<']+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def contains_semantic(actual: Any, expected: Any) -> bool:
    haystack = norm(actual)
    needle = norm(expected)
    if needle in haystack:
        return True
    tokens = [token for token in needle.split() if len(token) > 2]
    return bool(tokens) and all(token in haystack for token in tokens)


def metric_id_matches(actual: Any, expected: Any) -> bool:
    actual_norm = norm(actual)
    expected_norm = norm(expected)
    if actual_norm == expected_norm:
        return True
    generic = {"count", "rate", "metric", "total", "unique", "distinct"}
    actual_tokens = set(actual_norm.split()) - generic
    expected_tokens = set(expected_norm.split()) - generic
    return bool(actual_tokens & expected_tokens)


def get_path(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            raise KeyError(path)
        value = value[segment]
    return value


@dataclass
class GradeResult:
    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)


REPORT_FIELDS = {
    "report_name",
    "objective",
    "grain",
    "timezone",
    "population",
    "metrics",
    "dimensions",
    "filters",
    "comparisons",
    "delivery",
    "assumptions",
    "open_questions",
    "validation_checks",
}


def grade_report(case: BenchmarkCase, output: str) -> GradeResult:
    try:
        payload = extract_json_object(output)
    except ValueError as exc:
        return GradeResult(score=0.0, passed=False, details={"parse_error": str(exc)})

    present = len(REPORT_FIELDS.intersection(payload))
    schema_score = present / len(REPORT_FIELDS)
    check_results: list[dict[str, Any]] = []
    for check in case.checks:
        kind = check["kind"]
        ok = False
        actual: Any = None
        try:
            if kind == "equals":
                actual = get_path(payload, check["path"])
                if check["path"] == "report_name":
                    ok = contains_semantic(actual, check["value"])
                else:
                    ok = norm(actual) == norm(check["value"])
            elif kind == "contains":
                actual = get_path(payload, check["path"])
                ok = contains_semantic(actual, check["value"])
            elif kind == "list_contains":
                actual = get_path(payload, check["path"])
                if not isinstance(actual, list):
                    ok = False
                else:
                    ok = any(contains_semantic(item, check["value"]) for item in actual)
            elif kind == "metric_contains":
                metrics = payload.get("metrics", [])
                metric = next(
                    (
                        item
                        for item in metrics
                        if isinstance(item, dict)
                        and metric_id_matches(item.get("id", ""), check["metric_id"])
                    ),
                    None,
                )
                actual = metric
                haystack = norm(metric) if metric else ""
                ok = bool(metric) and all(
                    contains_semantic(haystack, term) for term in check["terms"]
                )
            else:
                actual = f"unknown check kind: {kind}"
        except (KeyError, TypeError):
            ok = False
        check_results.append(
            {
                "kind": kind,
                "target": check.get("path", check.get("metric_id")),
                "ok": ok,
                "actual": actual,
            }
        )

    requirement_score = (
        sum(1 for result in check_results if result["ok"]) / len(check_results)
        if check_results
        else 1.0
    )
    score = round(0.15 * schema_score + 0.85 * requirement_score, 4)
    return GradeResult(
        score=score,
        passed=score >= 0.8,
        details={
            "schema_score": round(schema_score, 4),
            "requirements_score": round(requirement_score, 4),
            "checks": check_results,
        },
    )


INDEX_RE = re.compile(
    r"^create\s+(?:unique\s+)?index\s+(?!concurrently\b)"
    r"(?:if\s+not\s+exists\s+)?(?P<name>[a-z_][a-z0-9_$]*)\s+"
    r"on\s+(?:public\.)?(?P<table>[a-z_][a-z0-9_$]*)\b",
    re.IGNORECASE | re.DOTALL,
)


def validate_index_statement(statement: str) -> tuple[str, str, str]:
    value = statement.strip()
    if value.endswith(";"):
        value = value[:-1].rstrip()
    if ";" in value or "--" in value or "/*" in value:
        raise ValueError("index must be one comment-free statement")
    match = INDEX_RE.match(value)
    if not match:
        raise ValueError("only non-CONCURRENT CREATE INDEX on a public table is allowed")
    if match.group("table") not in {
        "tenants",
        "customers",
        "products",
        "orders",
        "order_items",
        "events",
        "support_tickets",
    }:
        raise ValueError("index targets a table outside the benchmark allowlist")
    forbidden = re.search(r"\b(drop|alter|insert|update|delete|copy|do|call)\b", value, re.I)
    if forbidden:
        raise ValueError("forbidden token in index statement")
    return value, match.group("name"), match.group("table")


def _explain_cursor(cursor: psycopg.Cursor[Any], query: str) -> ExplainResult:
    row = cursor.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchone()
    return ExplainResult(plan=row[0][0]["Plan"])


def _fetch_all_bounded(
    cursor: psycopg.Cursor[Any], query: str, max_rows: int = 5000
) -> list[tuple[Any, ...]]:
    cursor.execute(query)
    rows = cursor.fetchmany(max_rows + 1)
    if len(rows) > max_rows:
        raise ValueError(f"equivalence result exceeds {max_rows} rows")
    return rows


def grade_sql(case: BenchmarkCase, output: str, dsn: str) -> GradeResult:
    try:
        payload = extract_json_object(output)
    except ValueError as exc:
        return GradeResult(score=0.0, passed=False, details={"parse_error": str(exc)})

    required = {"diagnosis", "optimized_sql", "index_statements", "tradeoffs", "evidence"}
    structure = len(required.intersection(payload)) / len(required)
    details: dict[str, Any] = {"structure_score": structure}

    try:
        optimized = normalize_readonly_query(str(payload.get("optimized_sql", "")))
        original = normalize_readonly_query(case.sql)
        raw_indexes = payload.get("index_statements", [])
        if not isinstance(raw_indexes, list) or len(raw_indexes) > 3:
            raise ValueError("index_statements must be a list with at most 3 entries")
        indexes = [validate_index_statement(str(item))[0] for item in raw_indexes]
        safe = True
    except Exception as exc:
        details["validation_error"] = f"{type(exc).__name__}: {exc}"
        return GradeResult(
            score=round(0.1 * structure, 4), passed=False, details=details
        )

    equivalence = False
    ratio = 0.0
    db_error: str | None = None
    baseline_cost = 0.0
    candidate_cost = 0.0
    conn: psycopg.Connection[Any] | None = None
    try:
        conn = psycopg.connect(dsn, autocommit=False, connect_timeout=8)
        cursor = conn.cursor()
        cursor.execute("SET LOCAL statement_timeout = '45000ms'")
        cursor.execute("SET LOCAL lock_timeout = '3000ms'")
        original_rows = _fetch_all_bounded(cursor, original)
        candidate_rows = _fetch_all_bounded(cursor, optimized)
        equivalence = original_rows == candidate_rows
        baseline_plan = _explain_cursor(cursor, original)
        baseline_cost = baseline_plan.total_cost
        for statement in indexes:
            cursor.execute(statement)
        candidate_plan = _explain_cursor(cursor, optimized)
        candidate_cost = candidate_plan.total_cost
        ratio = baseline_cost / candidate_cost if candidate_cost > 0 else 0.0
    except Exception as exc:
        db_error = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.rollback()
            conn.close()

    index_text = norm(payload.get("index_statements", []))
    expected_index_score = (
        sum(norm(term) in index_text for term in case.expected_index_terms)
        / len(case.expected_index_terms)
        if case.expected_index_terms
        else 1.0
    )
    optimized_text = norm(optimized)
    expected_sql_score = (
        sum(norm(term) in optimized_text for term in case.expected_sql_terms)
        / len(case.expected_sql_terms)
        if case.expected_sql_terms
        else 1.0
    )
    improvement_score = min(max((ratio - 1.0) / max(case.min_cost_ratio - 1.0, 0.01), 0.0), 1.0)
    tradeoffs = payload.get("tradeoffs")
    tradeoff_score = 1.0 if isinstance(tradeoffs, list) and any(norm(x) for x in tradeoffs) else 0.0
    evidence = payload.get("evidence")
    evidence_score = 0.0
    evidence_error: str | None = None
    try:
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be an object")
        reported_baseline = float(evidence["baseline_total_cost"])
        reported_candidate = float(evidence["candidate_total_cost"])
        reported_ratio = float(evidence["cost_ratio"])

        def close(reported: float, actual: float) -> bool:
            return abs(reported - actual) <= max(abs(actual) * 0.03, 0.02)

        if not (
            close(reported_baseline, baseline_cost)
            and close(reported_candidate, candidate_cost)
            and close(reported_ratio, ratio)
        ):
            raise ValueError("reported before/after evidence differs from grader trial")
        evidence_score = 1.0
    except (KeyError, TypeError, ValueError) as exc:
        evidence_error = str(exc)

    score = (
        0.05 * structure
        + 0.05 * float(safe)
        + 0.25 * float(equivalence)
        + 0.20 * improvement_score
        + 0.10 * expected_index_score
        + 0.05 * expected_sql_score
        + 0.05 * tradeoff_score
        + 0.25 * evidence_score
    )
    details.update(
        {
            "safe": safe,
            "equivalent": equivalence,
            "baseline_cost": round(baseline_cost, 3),
            "candidate_cost": round(candidate_cost, 3),
            "cost_ratio": round(ratio, 3),
            "target_cost_ratio": case.min_cost_ratio,
            "improvement_score": round(improvement_score, 4),
            "expected_index_score": round(expected_index_score, 4),
            "expected_sql_score": round(expected_sql_score, 4),
            "evidence_score": evidence_score,
            "evidence_error": evidence_error,
            "db_error": db_error,
        }
    )
    score = round(score, 4)
    return GradeResult(score=score, passed=score >= 0.8, details=details)


def grade_case(case: BenchmarkCase, output: str, dsn: str) -> GradeResult:
    if case.kind == "report":
        return grade_report(case, output)
    return grade_sql(case, output, dsn)
