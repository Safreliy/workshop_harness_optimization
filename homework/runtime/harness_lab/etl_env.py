"""Bounded SELECT-only SQLite ETL environment and independent exact grader."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .etl_data import TARGET_COLUMNS, load_task


def safe_id(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def money_cents(value: Any, fmt: str) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    patterns = {"int_cents": r"[0-9]+", "decimal_dot": r"[0-9]+(?:\.[0-9]{1,2})?", "decimal_comma": r"[0-9]+(?:,[0-9]{1,2})?"}
    if fmt not in patterns or not re.fullmatch(patterns[fmt], text):
        return None
    try:
        number = Decimal(text.replace(",", ".")) * (1 if fmt == "int_cents" else 100)
        if not number.is_finite() or number != number.to_integral_value() or number > 10**14:
            return None
        return int(number)
    except (InvalidOperation, ValueError):
        return None


def utc_timestamp(value: Any) -> str | None:
    try:
        timestamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        return timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}


class ETLEnvironment:
    """Only public task data is loaded. Private gold is passed exclusively to grade().

    This is a SQLite query sandbox, not a sandbox for arbitrary Python programs.
    All source rows are loaded as TEXT so automatic CSV inference cannot destroy IDs.
    """
    MAX_ROWS = 10000
    MAX_SQL_LENGTH = 30000

    def __init__(self, task: dict[str, Any] | str | Path):
        self.task = load_task(task) if isinstance(task, (str, Path)) else task
        if "_task_dir" not in self.task:
            raise ValueError("Use load_task(path) to resolve source paths")
        self.connection = sqlite3.connect(":memory:")
        # The progress handler bounds instructions, but built-in functions can
        # otherwise allocate a large value inside a single instruction.
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, self.MAX_SQL_LENGTH)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 100)
        self.connection.row_factory = sqlite3.Row
        self.connection.enable_load_extension(False)
        self.connection.create_function("SAFE_ID", 1, safe_id, deterministic=True)
        self.connection.create_function("MONEY_CENTS", 2, money_cents, deterministic=True)
        self.connection.create_function("UTC_TIMESTAMP", 1, utc_timestamp, deterministic=True)
        self.source_rows: dict[str, list[dict[str, Any]]] = {}
        self.source_columns: dict[str, list[str]] = {}
        base = Path(self.task["_task_dir"]).resolve()
        for source in self.task["sources"]:
            table = source["table"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) or table in self.source_rows:
                raise ValueError("Invalid or duplicate source table")
            path = (base / source["path"]).resolve()
            if not path.is_relative_to(base):
                raise ValueError("Source path escapes public task directory")
            columns = list(source["columns"])
            if not columns or len(columns) != len(set(columns)):
                raise ValueError("Source columns must be distinct")
            if source["format"] == "csv":
                with path.open(encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if reader.fieldnames != columns:
                        raise ValueError("CSV schema differs from declared columns")
                    rows = list(reader)
            elif source["format"] == "json":
                rows = json.loads(path.read_text(encoding="utf-8"))
            elif source["format"] == "sqlite":
                uri = path.as_uri() + "?mode=ro&immutable=1"
                with closing(sqlite3.connect(uri, uri=True)) as source_connection:
                    source_connection.execute("PRAGMA query_only=ON")
                    source_connection.row_factory = sqlite3.Row
                    rows = [dict(row) for row in source_connection.execute(f"SELECT {','.join(map(_quote, columns))} FROM {_quote(table)}")]
            else:
                raise ValueError("Supported source formats: csv, json, sqlite")
            if not isinstance(rows, list) or len(rows) > self.MAX_ROWS or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows):
                raise ValueError("Source rows do not match declared schema or exceed row limit")
            normalized = [{column: None if row[column] is None else str(row[column]) for column in columns} for row in rows]
            self.source_rows[table] = normalized
            self.source_columns[table] = columns
            self._load_rows(table, columns, normalized)
        self.connection.commit()
        self.connection.set_authorizer(self._authorize)
        self.output_rows: list[dict[str, Any]] = []
        self.pipeline_sql: str | None = None
        self.tool_calls = 0
        self.execution_count = 0
        self.last_error: str | None = None

    @property
    def output(self) -> list[dict[str, Any]]:
        return self.output_rows

    @property
    def last_sql(self) -> str | None:
        return self.pipeline_sql

    @property
    def last_pipeline_sql(self) -> str | None:
        return self.pipeline_sql

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
            _schema("list_sources", "List loaded sources, their formats, semantic column descriptions and money formats. No private expected results are available.", {}),
            _schema("inspect_source", "Show source columns and up to eight raw rows. All values are TEXT; preserve identifiers.", {"table": {"type": "string"}}),
            _schema("profile_source", "Show exact row counts, missing values, distinct values and sample values for one source.", {"table": {"type": "string"}}),
            _schema("query", "Run one bounded read-only SQLite SELECT/WITH for investigation; shows at most 100 rows. Helpers SAFE_ID, MONEY_CENTS(value,format), UTC_TIMESTAMP are available.", {"sql": {"type": "string"}}),
            _schema("execute_pipeline", "Run one SELECT/WITH returning exactly the target columns. Replace the target snapshot only on success. No source writes, DDL, filesystem access or private gold.", {"sql": {"type": "string"}}),
            _schema("validate_output", "Check public output schema, types, duplicate keys, currencies and net arithmetic. Passing these checks does not establish business correctness.", {}),
        ]

    def _load_rows(self, table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
        self.connection.execute(f"CREATE TABLE {_quote(table)} ({','.join(_quote(column) + ' TEXT' for column in columns)})")
        self.connection.executemany(f"INSERT INTO {_quote(table)} VALUES ({','.join('?' for _ in columns)})", [[row[column] for column in columns] for row in rows])

    def _authorize(self, action: int, arg1: str | None, arg2: str | None, database: str | None, trigger: str | None) -> int:
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 in self.source_rows else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in {"load_extension", "readfile", "writefile"} else sqlite3.SQLITE_OK
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def _select(self, sql: str, row_limit: int | None = None) -> tuple[list[str], list[dict[str, Any]]]:
        if not isinstance(sql, str) or len(sql) > self.MAX_SQL_LENGTH or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            raise ValueError("Supply one SELECT or WITH query, at most 30000 characters")
        deadline = time.monotonic() + 3
        steps = 0
        def progress() -> int:
            nonlocal steps
            steps += 1
            return int(steps > 1500 or time.monotonic() > deadline)
        self.connection.set_progress_handler(progress, 1000)
        try:
            cursor = self.connection.execute(sql)
            columns = [entry[0] for entry in cursor.description or []]
            if len(columns) != len(set(columns)):
                raise ValueError("Query result has duplicate column names; use explicit aliases")
            limit = row_limit or self.MAX_ROWS
            rows = [dict(row) for row in cursor.fetchmany(limit + 1)]
            if len(rows) > limit:
                raise ValueError(f"Query result exceeds {limit} row limit")
            return columns, rows
        finally:
            self.connection.set_progress_handler(None, 0)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.tool_calls += 1
        try:
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            expected_args = {"list_sources": set(), "inspect_source": {"table"}, "profile_source": {"table"}, "query": {"sql"}, "execute_pipeline": {"sql"}, "validate_output": set()}
            if name not in expected_args or set(arguments) != expected_args[name]:
                raise ValueError("Unknown tool or unexpected arguments")
            if name == "list_sources":
                return {"ok": True, "sources": [{**source, "row_count": len(self.source_rows[source["table"]])} for source in self.task["sources"]], "sql_functions": self.task["sql_functions"]}
            if name in {"inspect_source", "profile_source"}:
                table = arguments["table"]
                if not isinstance(table, str) or table not in self.source_rows:
                    raise ValueError("Unknown source table")
                rows = self.source_rows[table]
                if name == "inspect_source":
                    return {"ok": True, "table": table, "columns": self.source_columns[table], "row_count": len(rows), "rows": rows[:8]}
                profile = {}
                for column in self.source_columns[table]:
                    values = [row[column] for row in rows]
                    profile[column] = {"missing": sum(value is None or not str(value).strip() for value in values), "distinct": len(set(values)), "values": list(dict.fromkeys(values))[:8]}
                return {"ok": True, "row_count": len(rows), "columns": profile}
            if name == "query":
                columns, rows = self._select(arguments["sql"])
                return {"ok": True, "columns": columns, "row_count": len(rows), "rows": rows[:100], "display_truncated": len(rows) > 100}
            if name == "execute_pipeline":
                self.execution_count += 1
                columns, rows = self._select(arguments["sql"])
                if set(columns) != set(self.task["target"]["columns"]):
                    raise ValueError("Output must contain exactly the declared target columns")
                # Assign only after successful complete execution and schema validation.
                self.output_rows = rows
                self.pipeline_sql = arguments["sql"]
                self.last_error = None
                return {"ok": True, "row_count": len(rows), "rows": rows[:20], "display_truncated": len(rows) > 20, "validation": self.validate_output()}
            return self.validate_output()
        except (ValueError, sqlite3.Error, TypeError) as error:
            self.last_error = str(error)
            return {"ok": False, "error": str(error)}

    def validate_output(self) -> dict[str, Any]:
        rows = self.output_rows
        key = self.task["target"]["key"]
        keys = [row.get(key) for row in rows]
        money_columns = [column for column in TARGET_COLUMNS if column.endswith("_cents")]
        checks = {
            "pipeline_executed": self.pipeline_sql is not None,
            "nonempty": bool(rows),
            "exact_columns": all(set(row) == set(self.task["target"]["columns"]) for row in rows),
            "unique_order_keys": len(keys) == len(set(keys)),
            "text_identifiers": all(isinstance(row.get("order_id"), str) and bool(row["order_id"]) and isinstance(row.get("customer_id"), str) and bool(row["customer_id"]) for row in rows),
            "integer_money": all(all(type(row.get(column)) is int for column in money_columns) for row in rows),
            "normalized_currency": all(isinstance(row.get("currency"), str) and bool(re.fullmatch(r"[A-Z]{3}", row["currency"])) for row in rows),
            "net_arithmetic": all(type(row.get("net_cents")) is int and type(row.get("paid_cents")) is int and type(row.get("refunded_cents")) is int and row["net_cents"] == row["paid_cents"] - row["refunded_cents"] for row in rows),
        }
        return {"ok": True, "passed": all(checks.values()), "checks": checks, "row_count": len(rows), "duplicate_key_count": len(keys) - len(set(keys)), "scope": "Public structural checks only; private expected rows not consulted."}

    @staticmethod
    def _canonical(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) for row in rows)

    def grade(self, gold: dict[str, Any] | str | Path) -> dict[str, Any]:
        if isinstance(gold, (str, Path)):
            gold = json.loads(Path(gold).read_text(encoding="utf-8"))
        task_id = self.task.get("task_id", self.task["id"])
        if gold.get("task_id") != task_id:
            raise ValueError("Gold belongs to a different task")
        expected = gold["expected_rows"]
        actual = self.output_rows
        by_key = {row["order_id"]: row for row in expected}
        key_counts = Counter(row.get("order_id") for row in actual)
        correct_rows = 0
        correct_cells = 0
        mismatches = []
        for row in actual:
            key = row.get("order_id")
            target = by_key.get(key)
            if target and key_counts[key] == 1:
                equal = {column: type(row.get(column)) is type(target[column]) and row.get(column) == target[column] for column in TARGET_COLUMNS}
                correct_cells += sum(equal.values())
                if all(equal.values()):
                    correct_rows += 1
                else:
                    mismatches.append({"order_id": key, "columns": [column for column, valid in equal.items() if not valid]})
        missing = sorted(set(by_key) - set(key_counts))
        extra = sorted(str(key) for key in set(key_counts) - set(by_key))
        public = self.validate_output()
        exact = correct_rows == len(expected) == len(actual) and public["passed"]
        repeat_ok = permutation_ok = False
        metamorphic_error = None
        if self.pipeline_sql:
            try:
                original = self._canonical(actual)
                repeat_ok = self._canonical(self._select(self.pipeline_sql)[1]) == original
                # Reverse insertion order in a fresh database. Source files are unchanged.
                with ETLEnvironment(self.task) as alternate:
                    alternate.connection.set_authorizer(None)
                    for table, rows in alternate.source_rows.items():
                        alternate.connection.execute(f"DELETE FROM {_quote(table)}")
                        columns = alternate.source_columns[table]
                        alternate.connection.executemany(f"INSERT INTO {_quote(table)} VALUES ({','.join('?' for _ in columns)})", [[row[column] for column in columns] for row in reversed(rows)])
                    alternate.connection.commit()
                    alternate.connection.set_authorizer(alternate._authorize)
                    permutation_ok = alternate._canonical(alternate._select(self.pipeline_sql)[1]) == original
            except (ValueError, sqlite3.Error, TypeError) as error:
                metamorphic_error = str(error)
        score = correct_rows / max(len(expected), len(actual), 1)
        return {"task_id": task_id, "passed": exact and repeat_ok and permutation_ok, "score": score, "exact_result": exact, "correct_rows": correct_rows, "expected_rows": len(expected), "actual_rows": len(actual), "row_accuracy": score, "cell_accuracy": correct_cells / (max(len(expected), len(actual), 1) * len(TARGET_COLUMNS)), "missing_keys": missing, "extra_keys": extra, "duplicate_keys": sum(count - 1 for count in key_counts.values()), "mismatches": mismatches[:20], "public_checks": public["checks"], "repeat_run_equal": repeat_ok, "source_permutation_equal": permutation_ok, "metamorphic_error": metamorphic_error, "tool_calls": self.tool_calls, "pipeline_executions": self.execution_count}

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ETLEnvironment":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
