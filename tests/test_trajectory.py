from harness_lab.agent import AgentEvent, AgentResult
from harness_lab.cases import load_cases
from harness_lab.trajectory import grade_trajectory


def test_report_trajectory_penalizes_database_churn() -> None:
    case = load_cases(split="train", kinds={"report"})[0]
    clean = AgentResult(case_id=case.id, output="{}", llm_calls=1)
    noisy = AgentResult(
        case_id=case.id,
        output="{}",
        llm_calls=6,
        events=[
            AgentEvent(
                stage="tool",
                data={"name": "list_tables", "arguments": "{}", "output": '{"ok":true}'},
            ),
            AgentEvent(stage="forced_final", data={}),
        ],
    )
    assert grade_trajectory(case, clean).score == 1.0
    noisy_grade = grade_trajectory(case, noisy)
    assert noisy_grade.score < 0.7
    assert "report_unnecessary_database_tools" in noisy_grade.details["issues"]


def test_sql_trajectory_values_trial_and_ordering() -> None:
    case = load_cases(split="train", kinds={"sql"})[0]
    events = [
        AgentEvent(stage="tool", data={"name": name, "arguments": str(i), "output": '{"ok":true}'})
        for i, name in enumerate(
            ["describe_table", "explain_query", "trial_index_plan"], start=1
        )
    ]
    result = AgentResult(case_id=case.id, output="{}", llm_calls=4, events=events)
    grade = grade_trajectory(case, result)
    assert grade.passed
    assert grade.score == 1.0


def test_sql_trajectory_exposes_failure_taxonomy() -> None:
    case = load_cases(split="train", kinds={"sql"})[0]
    result = AgentResult(
        case_id=case.id,
        output="{}",
        llm_calls=7,
        events=[AgentEvent(stage="forced_final", data={})],
    )
    grade = grade_trajectory(case, result)
    assert not grade.passed
    assert "missing_transactional_index_trial" in grade.details["issues"]
