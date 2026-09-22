import json

import pytest

from harness_lab.cases import load_cases
from harness_lab.db import normalize_readonly_query
from harness_lab.graders import (
    contains_semantic,
    extract_json_object,
    grade_report,
    validate_index_statement,
)


def test_extract_json_from_fence() -> None:
    assert extract_json_object('result:\n```json\n{"ok": true}\n```') == {"ok": True}


def test_bilingual_semantic_aliases_are_explicit_and_deterministic() -> None:
    assert contains_semantic("по понедельникам в 09:00", "monday")
    assert contains_semantic("налоговое поле отсутствует", "tax")
    assert contains_semantic("week-over-week", "wow")
    assert contains_semantic("test orders", "is_test")


def test_report_grader_is_deterministic() -> None:
    case = load_cases(split="train", kinds={"report"})[0]
    payload = {
        "report_name": "weekly_b2b_revenue",
        "objective": "Board revenue",
        "grain": "iso_week x plan x sales_channel",
        "timezone": "Europe/Moscow",
        "population": {
            "include": ["growth", "enterprise"],
            "exclude": ["starter", "is_test=true", "refunded", "cancelled"],
        },
        "metrics": [
            {
                "id": "recognized_net_revenue",
                "definition": "paid orders: total minus discount",
                "formula_sql": "sum(total - discount) filter (where status='paid')",
                "owner": "Finance",
            },
            {
                "id": "orders_count",
                "definition": "distinct order count",
                "formula_sql": "count(distinct order id)",
                "owner": "Finance",
            },
        ],
        "dimensions": ["plan", "sales_channel"],
        "filters": [],
        "comparisons": ["WoW"],
        "delivery": {"format": "csv", "cadence": "Monday 09:00 Moscow"},
        "assumptions": [],
        "open_questions": ["tax treatment"],
        "validation_checks": ["reconcile totals"],
    }
    first = grade_report(case, json.dumps(payload))
    second = grade_report(case, json.dumps(payload))
    assert first.score == second.score == 1.0
    assert first.passed


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM orders",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "SELECT pg_sleep(30)",
        "SELECT 1; SELECT 2",
    ],
)
def test_readonly_guard_rejects_unsafe_queries(query: str) -> None:
    with pytest.raises(ValueError):
        normalize_readonly_query(query)


def test_index_guard() -> None:
    value, name, table = validate_index_statement(
        "CREATE INDEX mh_orders_tenant ON public.orders (tenant_id, created_at DESC)"
    )
    assert name == "mh_orders_tenant"
    assert table == "orders"
    assert value.startswith("CREATE INDEX")


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE orders",
        "CREATE INDEX CONCURRENTLY bad ON orders (id)",
        "CREATE INDEX bad ON secrets (id)",
        "CREATE INDEX bad ON orders (id); DROP TABLE orders",
    ],
)
def test_index_guard_rejects_unsafe_ddl(statement: str) -> None:
    with pytest.raises(ValueError):
        validate_index_statement(statement)
