"""Small offline helpers: display tables and replay SELECT on public source rows.

No model calls, reference grading, credentials or network access.
"""
import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path

from IPython.display import HTML, display


def table(rows):
    if not rows:
        print("Нет строк")
        return
    columns = list(rows[0])
    cells = lambda values: "".join("<td>" + escape(str(v)) + "</td>" for v in values)
    header = "".join("<th>" + escape(c) + "</th>" for c in columns)
    body = "".join("<tr>" + cells(row.get(c, "") for c in columns) + "</tr>" for row in rows)
    display(HTML("<div style='overflow:auto'><table><thead><tr>" + header +
                 "</tr></thead><tbody>" + body + "</tbody></table></div>"))


def load_evidence():
    folder = Path(__file__).resolve().parent
    return json.loads((folder / "evidence/workshop_evidence.json").read_text(encoding="utf-8"))


def safe_id(value):
    return (str(value).strip() or None) if value is not None else None


def money_cents(value, fmt):
    # Same public parsing contract as harness_lab.etl_env.
    patterns = {"int_cents": r"[0-9]+", "decimal_dot": r"[0-9]+(?:\.[0-9]{1,2})?",
                "decimal_comma": r"[0-9]+(?:,[0-9]{1,2})?"}
    if value is None or fmt not in patterns:
        return None
    text = str(value).strip()
    if not re.fullmatch(patterns[fmt], text):
        return None
    try:
        amount = Decimal(text.replace(",", ".")) * (1 if fmt == "int_cents" else 100)
        if not amount.is_finite() or amount != amount.to_integral_value() or amount > 10**14:
            return None
        return int(amount)
    except (InvalidOperation, ValueError):
        return None


def diagnostic_db(source_rows):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function("SAFE_ID", 1, safe_id, deterministic=True)
    connection.create_function("MONEY_CENTS", 2, money_cents, deterministic=True)
    for name, rows in source_rows.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not rows:
            continue
        columns = list(rows[0])
        quote = lambda text: '"' + text.replace('"', '""') + '"'
        definition = ",".join(quote(c) + " TEXT" for c in columns)
        connection.execute(f"CREATE TABLE {quote(name)} ({definition})")
        placeholders = ",".join("?" for _ in columns)
        connection.executemany(f"INSERT INTO {quote(name)} VALUES ({placeholders})",
                               [[None if row.get(c) is None else str(row[c]) for c in columns] for row in rows])
    connection.commit()
    connection.execute("PRAGMA query_only=ON")
    # Permit only reads of the copied public tables, functions, SELECT and CTEs.
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               sqlite3.SQLITE_RECURSIVE}
    connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    # An accidentally unbounded edited query must not hang the notebook.
    ticks = [0]
    def bounded():
        ticks[0] += 1
        return int(ticks[0] > 1000)
    connection.set_progress_handler(bounded, 1000)
    return connection


def select(connection, sql):
    return [dict(row) for row in connection.execute(sql).fetchall()]


def compare_repetitions(results, *, control="control", candidate="currency-policy", category_labels=None):
    """Summarize recorded runs; comparisons use task ID and repetition, not position."""
    arms = (control, candidate)
    labels = category_labels or {}
    grouped = {arm: [row for row in results if row["arm"] == arm] for arm in arms}
    keyed = {arm: {(row["task_id"], row["repeat"]): row for row in rows}
             for arm, rows in grouped.items()}
    assert set(keyed[control]) == set(keyed[candidate]), "Different task/repetition pairs"
    assert all(len(keyed[arm]) == len(grouped[arm]) for arm in arms), "Duplicate runs"
    overview = [{"Вариант": arm, "Полный успех": f'{sum(row["passed"] for row in rows)}/{len(rows)}',
                 "Без pipeline": sum(not row["pipeline_executed"] for row in rows),
                 "LLM-вызовы": sum(row["llm_calls"] for row in rows),
                 "Входные токены": sum(row["input_tokens"] for row in rows),
                 "Выходные токены": sum(row["output_tokens"] for row in rows)}
                for arm, rows in grouped.items()]
    pairs = [{"Задача": task, "Повтор": repeat,
              "Baseline": "Успех" if keyed[control][(task, repeat)]["passed"] else "Провал",
              "Baseline + правило": "Успех" if keyed[candidate][(task, repeat)]["passed"] else "Провал",
              "Категория baseline": labels.get(keyed[control][(task, repeat)].get("category"), keyed[control][(task, repeat)].get("category", "")),
              "Категория + правило": labels.get(keyed[candidate][(task, repeat)].get("category"), keyed[candidate][(task, repeat)].get("category", ""))}
             for task, repeat in sorted(keyed[control])]
    wins = sum(not keyed[control][key]["passed"] and keyed[candidate][key]["passed"] for key in keyed[control])
    losses = sum(keyed[control][key]["passed"] and not keyed[candidate][key]["passed"] for key in keyed[control])
    return {"overview": overview, "pairs": pairs, "wins": wins, "losses": losses,
            "ties": len(pairs) - wins - losses}


def show_trajectory_links(results, *, title="Все траектории"):
    """Render local evidence links without pretending to execute the agent."""
    links = []
    for row in sorted(results, key=lambda item: (item["task_id"], item["repeat"], item["arm"])):
        label = f'{row["task_id"]} · повтор {row["repeat"]} · {row["arm"]}'
        status = "Успех" if row["passed"] else "Провал"
        links.append('<li><a href="' + escape(("" if (Path.cwd() / "notebook_support.py").is_file() else "_support/") + row["trajectory_viewer"], quote=True) + '">' +
                     escape(label) + '</a> — ' + status + '</li>')
    display(HTML('<details><summary>' + escape(title) + '</summary><ul>' + ''.join(links) + '</ul></details>'))
