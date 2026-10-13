"""Small SQLite adapter for the introduction, deliberately no model/network IO."""
import json
import re
import sqlite3


class SQLiteSandbox:
    """Only two ToolExecutor methods; not a PostgreSQL emulator."""
    def __init__(self, rows):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE orders(id INTEGER, status TEXT, currency TEXT, amount_cents INTEGER)")
        self.db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", rows)
        self.db.commit()
        self.db.execute("PRAGMA query_only=ON")

    def describe_table(self, table):
        if table != "orders":
            raise ValueError("Only orders is available")
        return {"table": table, "columns": [dict(r) for r in self.db.execute("PRAGMA table_info(orders)")]}

    def run_query(self, query):
        if not re.match(r"^\s*SELECT\b", query, re.I):
            raise ValueError("The teaching adapter accepts one SELECT only")
        rows = self.db.execute(query).fetchmany(51)
        return {"rows": [dict(r) for r in rows[:50]], "truncated": len(rows) > 50}

    @staticmethod
    def tool_json(value):
        return json.dumps(value, ensure_ascii=False)

    def close(self):
        self.db.close()
