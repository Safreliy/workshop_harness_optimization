from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql


PUBLIC_TABLES = {
    "tenants",
    "customers",
    "products",
    "orders",
    "order_items",
    "events",
    "support_tickets",
    "benchmark_metadata",
}

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|truncate|grant|revoke|copy|vacuum|"
    r"analyze|refresh|create|call|do|pg_sleep|dblink|lo_import|lo_export)\b",
    re.IGNORECASE,
)


def json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def normalize_readonly_query(query: str) -> str:
    normalized = query.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if ";" in normalized:
        raise ValueError("only one SQL statement is allowed")
    if not re.match(r"^(select|with)\b", normalized, re.IGNORECASE):
        raise ValueError("only SELECT/WITH queries are allowed")
    if FORBIDDEN_SQL.search(normalized):
        raise ValueError("query contains a forbidden mutating or unsafe operation")
    return normalized


def validate_table(table: str) -> str:
    value = table.removeprefix("public.")
    if value not in PUBLIC_TABLES:
        raise ValueError(f"table is not in benchmark allowlist: {table}")
    return value


@dataclass
class ExplainResult:
    plan: dict[str, Any]

    @property
    def total_cost(self) -> float:
        return float(self.plan.get("Total Cost", 0.0))

    def compact(self) -> dict[str, Any]:
        def walk(node: dict[str, Any]) -> dict[str, Any]:
            compact_node = {
                key: node[key]
                for key in (
                    "Node Type",
                    "Relation Name",
                    "Index Name",
                    "Join Type",
                    "Startup Cost",
                    "Total Cost",
                    "Plan Rows",
                    "Filter",
                    "Index Cond",
                    "Sort Key",
                )
                if key in node
            }
            if "Plans" in node:
                compact_node["Plans"] = [walk(child) for child in node["Plans"]]
            return compact_node

        return walk(self.plan)


class PostgresSandbox:
    def __init__(self, dsn: str, statement_timeout_ms: int = 15_000):
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms

    def ping(self) -> tuple[bool, str]:
        try:
            with psycopg.connect(self.dsn, connect_timeout=5) as conn:
                value = conn.execute(
                    "SELECT value FROM benchmark_metadata WHERE key='dataset_version'"
                ).fetchone()
                return True, f"dataset_version={value[0] if value else 'unknown'}"
        except Exception as exc:
            return False, str(exc)

    def _readonly_connection(self) -> psycopg.Connection[Any]:
        conn = psycopg.connect(self.dsn, autocommit=False)
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{self.statement_timeout_ms}ms'")
        conn.execute("SET LOCAL lock_timeout = '2000ms'")
        return conn

    def list_tables(self) -> list[dict[str, Any]]:
        with self._readonly_connection() as conn:
            rows = conn.execute(
                """
                SELECT c.relname, c.reltuples::bigint AS estimated_rows,
                       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                ORDER BY c.relname
                """
            ).fetchall()
            return [
                {"table": row[0], "estimated_rows": row[1], "total_size": row[2]}
                for row in rows
            ]

    def describe_table(self, table: str) -> dict[str, Any]:
        table = validate_table(table)
        with self._readonly_connection() as conn:
            columns = conn.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
                ORDER BY ordinal_position
                """,
                (table,),
            ).fetchall()
            indexes = conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename=%s ORDER BY indexname",
                (table,),
            ).fetchall()
            estimate = conn.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE oid=%s::regclass",
                (f"public.{table}",),
            ).fetchone()
            return {
                "table": table,
                "estimated_rows": estimate[0] if estimate else None,
                "columns": [
                    {"name": row[0], "type": row[1], "nullable": row[2] == "YES"}
                    for row in columns
                ],
                "indexes": [{"name": row[0], "definition": row[1]} for row in indexes],
            }

    def inspect_indexes(self, table: str) -> list[dict[str, str]]:
        return self.describe_table(table)["indexes"]

    def database_context(self) -> dict[str, Any]:
        return {
            "tables": self.list_tables(),
            "notes": [
                "Synthetic deterministic PostgreSQL 16 dataset.",
                "Use EXPLAIN rather than EXPLAIN ANALYZE during diagnosis.",
                "DDL proposed in the final answer is evaluated inside a rolled-back transaction.",
            ],
        }

    def explain(self, query: str) -> ExplainResult:
        query = normalize_readonly_query(query)
        with self._readonly_connection() as conn:
            row = conn.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchone()
            return ExplainResult(plan=row[0][0]["Plan"])

    def run_query(self, query: str, max_rows: int = 50) -> dict[str, Any]:
        query = normalize_readonly_query(query)
        safe_limit = min(max(max_rows, 1), 200)
        with self._readonly_connection() as conn:
            cursor = conn.execute(query)
            columns = [desc.name for desc in cursor.description or []]
            rows = cursor.fetchmany(safe_limit + 1)
            truncated = len(rows) > safe_limit
            rows = rows[:safe_limit]
            return {
                "columns": columns,
                "rows": rows,
                "truncated": truncated,
            }

    def trial_index_plan(
        self,
        baseline_query: str,
        candidate_query: str,
        index_statements: list[str],
    ) -> dict[str, Any]:
        """Measure planner cost with real indexes, then roll the transaction back."""
        baseline_query = normalize_readonly_query(baseline_query)
        candidate_query = normalize_readonly_query(candidate_query)
        if not index_statements or len(index_statements) > 3:
            raise ValueError("provide between 1 and 3 CREATE INDEX statements")
        # Local import keeps the shared DDL validator as the single safety policy.
        from .graders import validate_index_statement

        indexes = [validate_index_statement(item)[0] for item in index_statements]
        conn = psycopg.connect(self.dsn, autocommit=False)
        try:
            conn.execute("SET LOCAL statement_timeout = '45000ms'")
            conn.execute("SET LOCAL lock_timeout = '3000ms'")
            row = conn.execute(f"EXPLAIN (FORMAT JSON) {baseline_query}").fetchone()
            baseline = ExplainResult(plan=row[0][0]["Plan"])
            for statement in indexes:
                conn.execute(statement)
            row = conn.execute(f"EXPLAIN (FORMAT JSON) {candidate_query}").fetchone()
            candidate = ExplainResult(plan=row[0][0]["Plan"])
            ratio = (
                baseline.total_cost / candidate.total_cost
                if candidate.total_cost > 0
                else 0.0
            )
            return {
                "baseline_total_cost": round(baseline.total_cost, 3),
                "candidate_total_cost": round(candidate.total_cost, 3),
                "cost_ratio": round(ratio, 3),
                "baseline_plan": baseline.compact(),
                "candidate_plan": candidate.compact(),
                "rolled_back": True,
            }
        finally:
            conn.rollback()
            conn.close()

    def tool_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=json_default)
