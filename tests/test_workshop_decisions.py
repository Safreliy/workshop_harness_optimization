import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("workshop_compare", Path(__file__).parents[1] / "jev-workshop" / "compare.py")
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def test_probabilities_reject_invalid_distributions():
    for probabilities in ({"supported": 1}, dict(zip(compare.LABELS, [float("nan"), .5, .5])),
                          dict(zip(compare.LABELS, [1, 1, 0]))):
        with pytest.raises(ValueError):
            compare.validate_probabilities({"probabilities": probabilities})


def test_metrics_include_all_classes_and_errors():
    rows = [{"label": label, "prediction": label,
             "probabilities": {k: float(k == label) for k in compare.LABELS}, "latency_ms": 10}
            for label in compare.LABELS]
    rows.append({"error": "TimeoutError"})
    result = compare.metrics(rows)
    assert result["accuracy"] == result["macro_f1"] == 1
    assert result["brier"] == result["nll"] == result["ece_10"] == 0
    assert result["errors"] == 1


def test_gold_label_is_never_sent_as_model_input():
    row = {"id": "one", "request": "r", "evidence": "e", "claim": "c", "label": "supported"}
    assert set(compare.state_for(row)) == {"request", "evidence", "claim"}
    first = compare.fingerprint(row, "jev")
    row["label"] = "contradicted"
    assert first == compare.fingerprint(row, "jev")
    row["evidence"] = "different"
    assert first != compare.fingerprint(row, "jev")


def test_calibration_policy_does_not_choose_using_test_labels():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "jev-workshop"))
    from export_results import calibrated_policy
    calibration = [{"label": "supported", "prediction": "supported",
                    "probabilities": {"supported": .9, "contradicted": .05, "insufficient_evidence": .05}} for _ in range(30)]
    good = calibration
    bad = [{**r, "label": "contradicted"} for r in calibration]
    a, b = calibrated_policy(calibration, good), calibrated_policy(calibration, bad)
    assert a["threshold"] == b["threshold"]
    assert a["test"]["risk"] == 0
    assert b["test"]["risk"] == 1
