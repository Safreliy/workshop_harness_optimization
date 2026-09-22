from __future__ import annotations

import json
from typing import Any

from .config import HarnessConfig
from .db import PostgresSandbox


TERSE_DESCRIPTIONS = {
    "list_tables": "List public PostgreSQL tables.",
    "describe_table": "Show columns and indexes for a table.",
    "inspect_indexes": "Show indexes for a table.",
    "explain_query": "Return a PostgreSQL JSON query plan without executing the query.",
    "run_readonly_query": "Run one read-only SELECT query and return up to 50 rows.",
    "database_context": "Show database tables and approximate sizes.",
    "trial_index_plan": "Temporarily create candidate indexes, compare planner costs, and roll them back.",
}

DIAGNOSTIC_DESCRIPTIONS = {
    "list_tables": "Start here when table names or scale are unknown. Returns public tables, row estimates, and total sizes; estimates may be stale.",
    "describe_table": "Inspect exact PostgreSQL columns, types, nullability, row estimate, and current index definitions before writing SQL or recommending a new index.",
    "inspect_indexes": "Check current index definitions for one allowlisted public table. Use this before proposing an index to avoid duplicates and to reason about column order, predicates, and INCLUDE columns.",
    "explain_query": "Run EXPLAIN (FORMAT JSON), never ANALYZE, for one read-only SELECT/WITH. Compare dominant node, filters, index conditions, sort, rows, and Total Cost. Cost is planner evidence, not wall-clock latency.",
    "run_readonly_query": "Execute a single SELECT/WITH in a read-only transaction with timeout and a 50-row display cap. Use only for semantics or small samples; do not use it as a latency benchmark.",
    "database_context": "Return the full benchmark table inventory, approximate sizes, and sandbox constraints. Useful for planning multi-table diagnostics; follow with targeted describe/index/explain calls.",
    "trial_index_plan": "Compare the original baseline query with the exact rewritten candidate query plus 1–3 CREATE INDEX hypotheses inside a rolled-back transaction. Returns measured end-to-end baseline/candidate Total Cost, cost ratio, and compact plans. Use before claiming improvement and copy the numeric evidence into final JSON.",
}

PARAMETERS: dict[str, dict[str, Any]] = {
    "list_tables": {"type": "object", "properties": {}, "additionalProperties": False},
    "describe_table": {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
        "additionalProperties": False,
    },
    "inspect_indexes": {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
        "additionalProperties": False,
    },
    "explain_query": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "run_readonly_query": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "database_context": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "trial_index_plan": {
        "type": "object",
        "properties": {
            "baseline_query": {"type": "string"},
            "candidate_query": {"type": "string"},
            "index_statements": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "required": ["baseline_query", "candidate_query", "index_statements"],
        "additionalProperties": False,
    },
}


def build_tool_schemas(config: HarnessConfig) -> list[dict[str, Any]]:
    descriptions = (
        TERSE_DESCRIPTIONS
        if config.tool_description_profile == "terse"
        else DIAGNOSTIC_DESCRIPTIONS
    )
    schemas = []
    for name in config.enabled_tools:
        description = config.tool_description_overrides.get(name, descriptions[name])
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": PARAMETERS[name],
                },
            }
        )
    return schemas


class ToolExecutor:
    def __init__(self, database: PostgresSandbox):
        self.database = database

    def execute(self, name: str, arguments_json: str) -> str:
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be a JSON object")
            if name == "list_tables":
                value = self.database.list_tables()
            elif name == "describe_table":
                value = self.database.describe_table(str(args["table"]))
            elif name == "inspect_indexes":
                value = self.database.inspect_indexes(str(args["table"]))
            elif name == "explain_query":
                result = self.database.explain(str(args["query"]))
                value = {"total_cost": result.total_cost, "plan": result.compact()}
            elif name == "run_readonly_query":
                value = self.database.run_query(str(args["query"]))
            elif name == "database_context":
                value = self.database.database_context()
            elif name == "trial_index_plan":
                statements = args["index_statements"]
                if not isinstance(statements, list):
                    raise ValueError("index_statements must be a list")
                value = self.database.trial_index_plan(
                    str(args["baseline_query"]),
                    str(args["candidate_query"]),
                    [str(item) for item in statements],
                )
            else:
                raise ValueError(f"unknown tool: {name}")
            return self.database.tool_json({"ok": True, "result": value})
        except Exception as exc:
            return self.database.tool_json(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            )
