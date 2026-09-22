from harness_lab.cases import load_cases
from harness_lab.config import HarnessConfig
from harness_lab.prompts import render_task, system_prompt


def test_grader_checks_are_not_in_prompt() -> None:
    case = load_cases(split="eval", kinds={"sql"})[0]
    prompt = render_task(case)
    assert "min_cost_ratio" not in prompt
    assert "expected_index_terms" not in prompt


def test_routing_changes_domain_prompt() -> None:
    case = load_cases(split="train", kinds={"sql"})[0]
    baseline = system_prompt(case, HarnessConfig())
    routed = system_prompt(
        case,
        HarnessConfig(prompt_profile="postgres_specialist", routing="task_kind"),
    )
    assert "PostgreSQL performance engineer" in routed
    assert baseline != routed
