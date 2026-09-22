import pytest
from pydantic import ValidationError

from harness_lab.config import HarnessConfig, SearchConfig
from harness_lab.optimizer import MetaHarnessOptimizer
from harness_lab.runner import config_fingerprint


def test_configs_load() -> None:
    baseline = HarnessConfig.from_yaml("configs/baseline.yaml")
    search = SearchConfig.from_yaml("configs/search.yaml")
    assert baseline.workflow == "react"
    assert search.model_rounds == 3
    assert search.proposals_per_round == 3
    assert search.beam_width == 3


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(ValidationError):
        HarnessConfig(enabled_tools=["shell"])


def test_excessive_tool_description_is_rejected() -> None:
    with pytest.raises(ValidationError):
        HarnessConfig(tool_description_overrides={"list_tables": "x" * 1201})


def test_config_fingerprint_ignores_display_name() -> None:
    left = HarnessConfig(name="left")
    right = HarnessConfig(name="right")
    assert config_fingerprint(left) == config_fingerprint(right)


def test_optimizer_records_actual_before_after_diff() -> None:
    parent = HarnessConfig(workflow="react", max_steps=6)
    candidate = HarnessConfig(workflow="plan_execute", max_steps=8)
    assert MetaHarnessOptimizer._config_diff(parent, candidate) == {
        "max_steps": {"from": 6, "to": 8},
        "workflow": {"from": "react", "to": "plan_execute"},
    }


def test_optimizer_rejects_inactive_context_partition() -> None:
    optimizer = object.__new__(MetaHarnessOptimizer)
    optimizer.max_raw_task_chars = 10_000
    parent = HarnessConfig(context_partition="shared", review_mode="none")
    candidate = HarnessConfig(context_partition="stage_isolated", review_mode="none")
    changed = MetaHarnessOptimizer._config_diff(parent, candidate)
    assert optimizer._semantic_validation_notes(parent, candidate, changed) == [
        "context_partition is inactive while review_mode=none"
    ]
