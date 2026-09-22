from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


KNOWN_TOOLS = {
    "list_tables",
    "describe_table",
    "inspect_indexes",
    "explain_query",
    "run_readonly_query",
    "database_context",
    "trial_index_plan",
}


class HarnessConfig(BaseModel):
    name: str = "unnamed"
    prompt_profile: Literal["baseline", "postgres_specialist"] = "baseline"
    prompt_patch: str = Field(default="", max_length=6000)
    routing: Literal["none", "task_kind"] = "none"
    workflow: Literal["react", "plan_execute"] = "react"
    context_strategy: Literal["raw", "requirements_ledger"] = "raw"
    context_partition: Literal["shared", "stage_isolated"] = "shared"
    review_mode: Literal["none", "domain_specialist"] = "none"
    tool_description_profile: Literal["terse", "diagnostic"] = "terse"
    enabled_tools: list[str] = Field(
        default_factory=lambda: [
            "list_tables",
            "describe_table",
            "explain_query",
            "run_readonly_query",
        ]
    )
    tool_description_overrides: dict[str, str] = Field(default_factory=dict)
    max_steps: int = Field(default=6, ge=1, le=12)
    max_context_chars: int = Field(default=60_000, ge=4_000, le=200_000)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("enabled_tools")
    @classmethod
    def validate_tools(cls, value: list[str]) -> list[str]:
        unknown = set(value) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        if len(value) != len(set(value)):
            raise ValueError("enabled_tools contains duplicates")
        return value

    @field_validator("tool_description_overrides")
    @classmethod
    def validate_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"overrides for unknown tools: {sorted(unknown)}")
        for name, description in value.items():
            if len(description) > 1200:
                raise ValueError(f"tool description for {name} is too long")
        return value

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(payload)

    def to_yaml(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(
                self.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )


class SearchConfig(BaseModel):
    base_config: str = "configs/baseline.yaml"
    objective_call_penalty: float = Field(default=0.006, ge=0.0, le=0.1)
    model_rounds: int = Field(default=3, ge=1, le=10)
    proposals_per_round: int = Field(default=3, ge=2, le=6)
    beam_width: int = Field(default=3, ge=1, le=8)
    proposal_temperature: float = Field(default=0.0, ge=0.0, le=1.0)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SearchConfig":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(payload)
