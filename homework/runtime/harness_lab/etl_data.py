"""Deterministic, locally generated ETL benchmark with separate public/private files.

The benchmark is synthetic. Splits isolate schema families, not merely random rows.
Private files are instructor artifacts; a local checkout is not an access boundary.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

TARGET_COLUMNS = ["order_id", "customer_id", "currency", "order_amount_cents", "paid_cents", "refunded_cents", "net_cents", "status"]
FAMILIES = [
    ("train", "standard_cents", "int_cents", "csv", False),
    ("train", "merchant_decimal", "decimal_dot", "json", False),
    ("train", "localized_comma", "decimal_comma", "sqlite", False),
    ("train", "delivery_retries", "int_cents", "json", True),
    ("train", "revised_status", "decimal_dot", "csv", True),
    ("train", "partial_refunds", "decimal_comma", "csv", True),
    ("dev", "settlement_feed", "decimal_dot", "sqlite", True),
    ("dev", "warehouse_exports", "int_cents", "csv", True),
    ("dev", "accounting_feed", "decimal_comma", "json", True),
    ("test", "partner_ledgers", "decimal_comma", "sqlite", True),
    ("test", "marketplace_events", "decimal_dot", "json", True),
    ("test", "legacy_sales", "int_cents", "csv", True),
]
SEMANTICS = {
    "orders": {"id": "Order identifier; preserve leading zeros, trim surrounding whitespace.", "customer": "Customer identifier; trim surrounding whitespace.", "version": "Integer order revision. Keep the highest revision for each order identifier.", "amount": "Order amount in the source money format.", "currency": "ISO currency, trim whitespace and uppercase.", "status": "Latest order status; trim whitespace and lowercase. Cancelled orders remain in output."},
    "payments": {"id": "Payment event identifier; retries with the same identifier are one event.", "order": "Referenced order identifier; trim whitespace and preserve leading zeros.", "version": "Integer event revision; keep highest revision before filtering status.", "amount": "Payment amount in the source money format; invalid values are ignored.", "currency": "ISO currency; only events matching the order currency contribute.", "status": "Only latest events with status settled contribute; trim whitespace and lowercase.", "time": "Event timestamp with UTC offset; arrival/file order does not select the revision."},
    "refunds": {"id": "Refund event identifier; retries with the same identifier are one event.", "order": "Referenced order identifier; trim whitespace and preserve leading zeros.", "version": "Integer event revision; keep highest revision before filtering status.", "amount": "Refund amount in the source money format; invalid values are ignored.", "currency": "ISO currency; only events matching the order currency contribute.", "status": "Only latest events with status settled contribute; trim whitespace and lowercase.", "time": "Event timestamp with UTC offset; arrival/file order does not select the revision."},
}
ALIASES = [
    ["id", "buyer", "revision", "amount", "currency", "status", "occurred_at"],
    ["reference", "account", "generation", "total", "ccy", "state", "timestamp"],
    ["номер", "клиент", "версия", "сумма", "валюта", "статус", "время"],
    ["key", "owner", "seq", "value", "unit", "phase", "event_time"],
    ["record_id", "customer_ref", "update_no", "gross", "currency_code", "lifecycle", "created_at"],
    ["document", "party", "rev", "money", "iso", "result", "date_time"],
    ["external_ref", "debtor", "edition", "sum_value", "money_unit", "resolution", "effective_at"],
    ["object_key", "client_key", "row_version", "minor_units", "denomination", "workflow_state", "observed_at"],
    ["operation", "counterparty", "modification", "total_value", "settlement_currency", "processing_state", "operation_time"],
    ["номер_объекта", "код_покупателя", "редакция", "величина", "код_валюты", "состояние", "момент"],
    ["entity_ref", "principal_ref", "change_seq", "monetary_value", "asset_code", "stage", "business_time"],
    ["sale_key", "person_key", "snapshot_no", "value_in_cents", "currency_iso", "record_state", "registered_at"],
]
RULES = [
    "Produce one row per order after choosing its greatest integer revision. Repeated identical deliveries are duplicates, not additional orders.",
    "Preserve identifiers as strings including leading zeros; trim whitespace. Uppercase currency and lowercase status.",
    "Keep every latest order, including cancelled orders. Monetary values are integer cents in output; source metadata specifies the input format.",
    "Deduplicate payments and refunds independently by event identifier, choosing greatest integer revision BEFORE filtering status. Equal highest revisions are identical copies.",
    "Sum only settled events with a valid nonnegative amount and currency matching the latest order. Ignore pending/failed events, invalid amounts, foreign currency and references to missing orders.",
    "paid_cents and refunded_cents are independent sums. Do not join raw payment and refund rows together: their multiplicity is independent.",
    "net_cents = paid_cents - refunded_cents. No clamping to order amount or zero. Missing events contribute zero.",
    "The pipeline must return the exact target columns. File order and row order carry no business meaning. Run again from the same immutable inputs to replace the target snapshot.",
]


def _money(cents: int, fmt: str) -> str:
    if fmt == "int_cents":
        return str(cents)
    value = f"{cents // 100}.{cents % 100:02d}"
    return value.replace(".", ",") if fmt == "decimal_comma" else value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_task(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    task = json.loads(path.read_text(encoding="utf-8"))
    task["_task_dir"] = str(path.parent)
    return task


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in task.items() if not key.startswith("_")}


def generate_dataset(root: str | Path, seed: int = 20261010, tasks_per_family: int = 2) -> dict[str, Any]:
    """Generate 24 tasks by default. Gold derives from canonical events before serialization.

    Existing generated files at the same IDs are replaced; unrelated files are untouched.
    SQLite sources contain no random binary state (fixed insertion order, VACUUM).
    """
    if not 1 <= tasks_per_family <= 100:
        raise ValueError("tasks_per_family must be between 1 and 100")
    root = Path(root).resolve()
    old_manifest_path = root / "manifest.json"
    old_hashes = json.loads(old_manifest_path.read_text(encoding="utf-8")).get("sha256", {}) if old_manifest_path.exists() else {}
    manifest: dict[str, Any] = {"version": 1, "seed": seed, "synthetic": True, "tasks_per_family": tasks_per_family, "split_policy": "Disjoint schema families; instances of a family never cross splits.", "tasks": [], "sha256": {}}
    for family_idx, (split, family, fmt, file_format, adversarial) in enumerate(FAMILIES):
        for instance in range(tasks_per_family):
            task_seed = seed + family_idx * 10000 + instance
            rng = random.Random(task_seed)
            task_id = f"{split}-{family}-{instance:02d}"
            folder = root / "public" / split / task_id
            folder.mkdir(parents=True, exist_ok=True)
            canonical: dict[str, list[dict[str, Any]]] = {"orders": [], "payments": [], "refunds": []}
            expected = []
            order_count = 7 + instance % 3
            for index in range(order_count):
                oid = f"{index + 1:05d}"
                customer = f"C{rng.randrange(1, 5):03d}"
                currency = ["RUB", "USD", "EUR"][index % 3]
                amount = rng.randrange(10, 300) * 100 + rng.randrange(100)
                status = "cancelled" if index == order_count - 1 else "completed"
                latest = {"id": oid, "customer": customer, "version": 2, "amount": amount, "currency": currency, "status": status}
                canonical["orders"].append(latest)
                if adversarial:
                    canonical["orders"].append({**latest, "version": 1, "customer": "OLD", "amount": amount + 500, "status": "pending"})
                    if index == 1:
                        canonical["orders"].append(latest.copy())
                paid = refunded = 0
                for event_idx in range(index % 3):
                    value = amount // 3 + event_idx
                    event = {"id": f"P{oid}-{event_idx}", "order": oid, "version": 2, "amount": value, "currency": currency, "status": "settled", "time": "2026-09-02T09:00:00+03:00"}
                    canonical["payments"].append(event)
                    paid += value
                    if adversarial:
                        canonical["payments"].append(event.copy())
                        canonical["payments"].append({**event, "version": 1, "status": "pending", "amount": value + 100, "time": "2026-09-03T07:00:00+00:00"})
                for event_idx in range(index % 2 + (1 if adversarial and index == 2 else 0)):
                    value = (amount // 10) + event_idx
                    event = {"id": f"R{oid}-{event_idx}", "order": oid, "version": 2, "amount": value, "currency": currency, "status": "settled", "time": "2026-09-04T00:00:00Z"}
                    canonical["refunds"].append(event)
                    refunded += value
                    if adversarial:
                        canonical["refunds"].extend([event.copy(), {**event, "version": 1, "status": "failed"}])
                if adversarial:
                    # A previously settled event subsequently fails: latest first is essential.
                    for table, prefix in [("payments", "PX"), ("refunds", "RX")]:
                        old = {"id": f"{prefix}{oid}", "order": oid, "version": 1, "amount": 777, "currency": currency, "status": "settled", "time": "2026-09-01T00:00:00Z"}
                        canonical[table].extend([old, {**old, "version": 2, "status": "failed"}])
                        canonical[table].append({**old, "id": f"INVALID-{prefix}{oid}", "amount": "not-a-number", "version": 2})
                        canonical[table].append({**old, "id": f"FOREIGN-{prefix}{oid}", "currency": "GBP", "version": 2})
                expected.append(dict(zip(TARGET_COLUMNS, [oid, customer, currency, amount, paid, refunded, paid - refunded, status], strict=True)))
            if adversarial:
                for table in ("payments", "refunds"):
                    canonical[table].append({"id": "ORPHAN", "order": "99999", "version": 1, "amount": 9999, "currency": "RUB", "status": "settled", "time": "2026-09-01T00:00:00Z"})
            sources = []
            aliases = ALIASES[family_idx]
            for table_index, (table, records) in enumerate(canonical.items()):
                # Every task spans three storage formats; family specifies orders format.
                formats = ["csv", "json", "sqlite"]
                source_format = formats[(formats.index(file_format) + table_index) % len(formats)]
                semantic_keys = list(SEMANTICS[table])
                # Distinct family aliases; event order reference is separate from its ID.
                actual_names = {key: (f"{aliases[0]}_order" if key == "order" else aliases[["id", "customer", "version", "amount", "currency", "status", "time"].index(key)]) for key in semantic_keys}
                columns = {actual_names[key]: SEMANTICS[table][key] for key in semantic_keys}
                rows = []
                for record in records:
                    row = {}
                    for key in semantic_keys:
                        value = record[key]
                        if key == "amount" and isinstance(value, int):
                            value = _money(value, fmt)
                        elif key in {"id", "order", "customer"} and adversarial and rng.random() < 0.3:
                            value = f" {value} "
                        elif key == "currency" and adversarial:
                            value = f" {str(value).lower()} "
                        elif key == "status" and adversarial:
                            value = f" {str(value).upper()} "
                        row[actual_names[key]] = str(value)
                    rows.append(row)
                rng.shuffle(rows)
                extension = "db" if source_format == "sqlite" else source_format
                source_path = folder / f"{table}.{extension}"
                if source_format == "csv":
                    with source_path.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(columns))
                        writer.writeheader()
                        writer.writerows(rows)
                elif source_format == "json":
                    _write_json(source_path, rows)
                else:
                    # Recreate only this known generated file; no directory deletion.
                    if source_path.exists():
                        source_path.unlink()
                    with closing(sqlite3.connect(source_path)) as connection:
                        quoted = ",".join('"' + name.replace('"', '""') + '" TEXT' for name in columns)
                        connection.execute(f'CREATE TABLE "{table}" ({quoted})')
                        connection.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in columns)})', [[row[name] for name in columns] for row in rows])
                        connection.commit()
                        connection.execute("VACUUM")
                sources.append({"table": table, "path": source_path.name, "format": source_format, "columns": columns, "money_format": fmt})
            task = {"id": task_id, "task_id": task_id, "split": split, "family_id": family, "description": "Build an orders settlement snapshot from independently delivered order, payment and refund exports. Return every latest order with independently aggregated payments and refunds.", "target": {"table": "target_orders", "columns": TARGET_COLUMNS, "key": "order_id"}, "rules": RULES, "sources": sources, "sql_functions": {"SAFE_ID(value)": "Trim text while preserving leading zeros; blank returns NULL.", "MONEY_CENTS(value, format)": "Parse nonnegative money exactly. format is int_cents, decimal_dot or decimal_comma. Invalid/sub-cent values return NULL.", "UTC_TIMESTAMP(value)": "Parse ISO timestamp with timezone into UTC ISO text; invalid returns NULL."}}
            _write_json(folder / "task.json", task)
            _write_json(root / "private" / f"{task_id}.json", {"task_id": task_id, "family_id": family, "expected_rows": expected, "synthetic_seed": task_seed})
            manifest["tasks"].append({"id": task_id, "split": split, "family_id": family, "public_path": str((folder / "task.json").relative_to(root)).replace("\\", "/"), "private_path": f"private/{task_id}.json"})
    for entry in manifest["tasks"]:
        folder = (root / entry["public_path"]).parent
        task = load_task(folder / "task.json")
        for path in sorted([folder / "task.json", *(folder / source["path"] for source in task["sources"])]):
            manifest["sha256"][str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
        path = root / entry["private_path"]
        manifest["sha256"][entry["private_path"]] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Remove only retired generated files whose hash still matches our last manifest.
    # Modified student files and all unknown files are preserved.
    for relative, previous_hash in old_hashes.items():
        if relative in manifest["sha256"]:
            continue
        path = (root / relative).resolve()
        known_name = path.name == "task.json" or re.fullmatch(r"(?:orders|payments|refunds)\.(?:csv|json|db)", path.name) or (relative.startswith("private/") and path.suffix == ".json")
        if path.is_relative_to(root) and known_name and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == previous_hash:
            path.unlink()
    _write_json(root / "manifest.json", manifest)
    return manifest


def reference_sql(task: dict[str, Any]) -> str:
    """Instructor reference, never automatically sent to the agent. Uses public semantics."""
    by_table = {source["table"]: source for source in task["sources"]}
    def q(table: str, semantic: str) -> str:
        keys = list(SEMANTICS[table])
        column = list(by_table[table]["columns"])[keys.index(semantic)]
        return '"' + column.replace('"', '""') + '"'
    ctes = []
    for table in ("orders", "payments", "refunds"):
        fmt = by_table[table]["money_format"]
        projection = [f"SAFE_ID({q(table, 'id')}) AS id", f"CAST({q(table, 'version')} AS INTEGER) AS version", f"MONEY_CENTS({q(table, 'amount')}, '{fmt}') AS amount", f"UPPER(TRIM({q(table, 'currency')})) AS currency", f"LOWER(TRIM({q(table, 'status')})) AS status"]
        projection.append(f"SAFE_ID({q(table, 'customer' if table == 'orders' else 'order')}) AS {'customer' if table == 'orders' else 'order_ref'}")
        ctes.append(f"{table}_normalized AS (SELECT {', '.join(projection)} FROM {table})")
        ctes.append(f"{table}_ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY version DESC) AS rn FROM {table}_normalized)")
    return "WITH " + ",\n".join(ctes) + """,
    latest_orders AS (SELECT * FROM orders_ranked WHERE rn=1),
    payment_totals AS (SELECT e.order_ref, e.currency, SUM(e.amount) AS amount FROM payments_ranked e WHERE e.rn=1 AND e.status='settled' AND e.amount IS NOT NULL GROUP BY e.order_ref,e.currency),
    refund_totals AS (SELECT e.order_ref, e.currency, SUM(e.amount) AS amount FROM refunds_ranked e WHERE e.rn=1 AND e.status='settled' AND e.amount IS NOT NULL GROUP BY e.order_ref,e.currency)
    SELECT o.id AS order_id, o.customer AS customer_id, o.currency, o.amount AS order_amount_cents,
           COALESCE(p.amount,0) AS paid_cents, COALESCE(r.amount,0) AS refunded_cents,
           COALESCE(p.amount,0)-COALESCE(r.amount,0) AS net_cents, o.status
    FROM latest_orders o LEFT JOIN payment_totals p ON p.order_ref=o.id AND p.currency=o.currency
    LEFT JOIN refund_totals r ON r.order_ref=o.id AND r.currency=o.currency"""
