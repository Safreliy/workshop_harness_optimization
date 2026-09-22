from harness_lab.cases import load_cases, public_case_payload


def test_expected_dataset_sizes() -> None:
    train = load_cases(split="train")
    eval_cases = load_cases(split="eval")
    assert len(train) == 10
    assert len(eval_cases) == 7
    assert {case.kind for case in train} == {"report", "sql"}
    assert all("eval" in case.id for case in eval_cases)


def test_public_payload_never_leaks_grader_fields() -> None:
    for case in load_cases(split="all"):
        payload = public_case_payload(case)
        assert "checks" not in payload
        assert "expected_index_terms" not in payload
        assert "min_cost_ratio" not in payload
