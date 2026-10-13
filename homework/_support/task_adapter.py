"""Minimal run/check interface to replace with your own task, no API required.

python task_adapter.py
Change CASES, expected values and run(), keep check() independent of run().
"""
import json
from coding_transfer import ACTIVE_DOCS, REGRESSION_DOCS, search_docs

CASES = [
    {"id": "active", "input": {"query": "VPN", "docs": ACTIVE_DOCS}, "expected": ["A1"]},
    {"id": "archived", "input": {"query": "VPN", "docs": REGRESSION_DOCS}, "expected": ["A1"]},
    {"id": "no_match", "input": {"query": "пароль", "docs": REGRESSION_DOCS}, "expected": []},
]

def run(config, inputs):
    """Replace with your agent: return artifact plus execution observations."""
    artifact = search_docs(inputs["query"], inputs["docs"], config["mode"])
    return {"artifact": artifact, "trace": [{"tool": "search_docs", "output": artifact}]}

def check(case, result):
    """Expected values stay with evaluator; don't pass them to agent run()."""
    return [] if result["artifact"] == case["expected"] else [
        {"criterion": "exact_ids", "expected": case["expected"], "actual": result["artifact"]}]

def evaluate(config, cases=CASES):
    episodes = []
    for case in cases:
        result = run(config, case["input"])
        issues = check(case, result)
        episodes.append({"case_id": case["id"], "passed": not issues, "issues": issues, **result})
    return {"config": config, "passed": sum(row["passed"] for row in episodes),
            "total": len(episodes), "episodes": episodes}

if __name__ == "__main__":
    reports = [evaluate({"mode": mode}) for mode in ("missing_archive_filter", "correct")]
    assert [report["passed"] for report in reports] == [2, 3]
    print(json.dumps(reports, ensure_ascii=False, indent=2))
