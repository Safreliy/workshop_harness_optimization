"""Threshold selection and classification accuracy are separate decisions."""
import importlib.util
import sys
from pathlib import Path

import pytest


PRACTICE = Path(__file__).resolve().parents[1] / "jev-workshop/practice"
sys.path.insert(0, str(PRACTICE))
SPEC = importlib.util.spec_from_file_location("jev_threshold_runtime", PRACTICE / "runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def row(p, label="supported", prediction=None):
    return {"split": "calibration", "label": label,
            "prediction": prediction or ("supported" if p >= .5 else "contradicted"),
            "probabilities": {"supported": p, "contradicted": 1 - p, "insufficient_evidence": 0}}


def test_no_admissible_threshold_is_disabled_not_impossible_probability():
    selected = runtime.calibrate_threshold([row(.9, "contradicted") for _ in range(12)])
    assert selected["threshold"] is None
    gate = runtime.evaluate_gate([row(1)], selected["threshold"])
    assert gate["accepted"] == 0 and gate["escalations"] == 1
    assert gate["false_accept_rate"] is None
    assert runtime.gate_view("model", "test", gate)["Порог P(supported)"] == "не найден"
    with pytest.raises(ValueError):
        runtime.evaluate_gate([row(1)], 1.01)


def test_maximum_coverage_with_false_accept_denominator_not_all_rows():
    rows = [row(.8) for _ in range(10)] + [row(.7, "contradicted")]
    selected = runtime.calibrate_threshold(rows)
    assert selected["threshold"] == .71
    gate = runtime.evaluate_gate(rows, selected["threshold"])
    assert gate["accepted"] == 10 and gate["false_accepts"] == 0
    assert gate["false_accept_rate"] == 0
    lower = runtime.evaluate_gate(rows, .7)
    assert lower["false_accept_rate"] == 1 / 11


def test_correct_negative_prediction_is_not_accepted_supported_answer():
    gate = runtime.evaluate_gate([row(.01, "contradicted", "contradicted")], .9)
    assert gate["accuracy"] == 1
    assert gate["accepted"] == 0 and gate["escalations"] == 1


def test_fixed_policy_preserves_observed_dataset_counts():
    benchmark = runtime.load_benchmark()
    expected = {"Jev": (20, 40, 9), "mmBERT head": (0, 0, 0),
                "mmBERT LoRA": (20, 20, 19), "TF-IDF": (0, 0, 0)}
    for model, counts in expected.items():
        splits = benchmark["models"][model]
        selected = runtime.calibrate_threshold(splits["calibration"])
        gates = [runtime.evaluate_gate(splits[s], selected["threshold"])
                 for s in ("calibration", "test", "challenge")]
        assert tuple(g["accepted"] for g in gates) == counts
    lora = runtime.calibrate_threshold(benchmark["models"]["mmBERT LoRA"]["calibration"])
    challenge = runtime.evaluate_gate(benchmark["models"]["mmBERT LoRA"]["challenge"], lora["threshold"])
    assert challenge["false_accepts"] == 15
    assert challenge["false_accept_rate"] == 15 / 19
