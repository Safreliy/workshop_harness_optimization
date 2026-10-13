"""A tool-calling ETL agent. Only public task information enters model context."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .model import ModelClient

ETL_TOOLS = {"list_sources", "inspect_source", "profile_source", "query", "execute_pipeline", "validate_output"}


class ETLHarnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="baseline", max_length=100)
    prompt_patch: str = Field(default="", max_length=6000)
    max_steps: int = Field(default=6, ge=2, le=20)
    workflow: Literal["react", "profile_first"] = "react"
    review: Literal["none", "public_checks"] = "none"
    tools: list[str] = Field(default_factory=lambda: sorted(ETL_TOOLS))
    max_tokens: int = Field(default=3000, ge=256, le=8000)
    temperature: float = Field(default=0.0, ge=0, le=1)

    @field_validator("tools")
    @classmethod
    def known_tools(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or set(value) - ETL_TOOLS:
            raise ValueError("tools must be a unique subset of supported ETL tools")
        if "execute_pipeline" not in value:
            raise ValueError("execute_pipeline is required")
        return value

    @model_validator(mode="after")
    def active_modes(self):
        required = {"list_sources", "inspect_source", "profile_source"} if self.workflow == "profile_first" else set()
        if self.review == "public_checks":
            required.add("validate_output")
        if not required <= set(self.tools):
            raise ValueError(f"selected modes require tools: {sorted(required)}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "ETLHarnessConfig":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    """Allowlist is intentional: private answer fields must never enter a prompt."""
    keys = {"id", "task_id", "description", "request", "instructions", "target", "target_schema", "public_checks", "business_rules", "requirements", "rules", "sql_functions", "sources"}
    # Source metadata can contain paths but never trainer-added private keys.
    payload = {key: value for key, value in task.items() if key in keys}
    if isinstance(payload.get("sources"), list):
        payload["sources"] = [
            {key: value for key, value in source.items() if key in {"table", "name", "format", "description", "columns", "money_format"}}
            if isinstance(source, dict) else source for source in payload["sources"]
        ]
    return payload


def run_agent(task: dict[str, Any] | str | Path, config: ETLHarnessConfig | dict[str, Any], client: ModelClient, *, environment=None) -> dict[str, Any]:
    from .etl_env import ETLEnvironment

    if not isinstance(config, ETLHarnessConfig):
        config = ETLHarnessConfig.model_validate(config)
    env = environment or ETLEnvironment(task)
    raw_task = env.task if hasattr(env, "task") else (json.loads(Path(task).read_text(encoding="utf-8")) if isinstance(task, (str, Path)) else task)
    result: dict[str, Any] = {
        "task_id": raw_task.get("id", raw_task.get("task_id", "unknown")),
        "config": config.model_dump(mode="json"), "model": getattr(client, "model_name", "test-client"), "output": "", "events": [],
        "llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "error": None, "pipeline_sql": None, "public_validation": None, "budget_exhausted": False, "empty_turns": 0, "truncated_turns": 0,
    }
    start = time.perf_counter()
    system = (
        "Build and execute an ETL pipeline from the supplied public sources and requirements. "
        "Use only the tools provided. Source tables are immutable. execute_pipeline accepts SQLite SELECT/WITH SQL, "
        "and replaces the output snapshot. Do not claim success before executing. "
        "Preserve identifiers and distinguish missing data from zero. Explain assumptions and unresolved ambiguities.\n"
        + config.prompt_patch
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(public_task(raw_task), ensure_ascii=False)}]
    schemas = [schema for schema in env.tool_schemas if schema["function"]["name"] in config.tools]

    def execute(name: str, args: dict[str, Any], stage: str, step: int = 0):
        started = time.perf_counter()
        try:
            if name not in config.tools:
                raise ValueError(f"tool disabled: {name}")
            output = env.call(name, args)
        except Exception as exc:
            output = {"error": f"{type(exc).__name__}: {exc}"}
        result["tool_calls"] += 1
        result["events"].append({"stage": stage, "step": step, "name": name, "arguments": args, "output": output, "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
        if name == "execute_pipeline" and not (isinstance(output, dict) and output.get("error")):
            result["pipeline_sql"] = args.get("sql")
        if name == "validate_output":
            result["public_validation"] = output
        return output

    def complete(stage: str, tools=None):
        started = time.perf_counter()
        turn = client.complete(messages, tools=tools, temperature=config.temperature, max_tokens=config.max_tokens)
        result["llm_calls"] += 1
        result["input_tokens"] += turn.input_tokens
        result["output_tokens"] += turn.output_tokens
        result["events"].append({"stage": stage, "content": turn.content, "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in turn.tool_calls], "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens, "finish_reason": turn.finish_reason, "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
        messages.append(turn.as_message())
        return turn

    try:
        if config.workflow == "profile_first":
            listing = execute("list_sources", {}, "preflight")
            messages.append({"role": "user", "content": "Source inventory: " + json.dumps(listing, ensure_ascii=False)})
            sources = listing.get("sources", listing.get("tables", [])) if isinstance(listing, dict) else listing
            for source in sources or []:
                table = source if isinstance(source, str) else source.get("table", source.get("name"))
                if table:
                    for tool in ("inspect_source", "profile_source"):
                        observation = execute(tool, {"table": table}, "preflight")
                        messages.append({"role": "user", "content": f"{tool} {table}: " + json.dumps(observation, ensure_ascii=False)})
            messages.append({"role": "user", "content": "Before executing, state a short transformation plan with grain, joins, deduplication, units, null handling and checks. No tools in this planning turn."})
            complete("planning")
            messages.append({"role": "user", "content": "Now implement the plan using tools. Revise it if observations contradict your assumptions."})

        reviewed_version = object()
        for step in range(1, config.max_steps + 1):
            turn = complete("agent", schemas)
            if not turn.tool_calls and (not turn.content.strip() or turn.finish_reason == "length"):
                empty = not turn.content.strip()
                result["empty_turns"] += int(empty)
                result["truncated_turns"] += int(turn.finish_reason == "length")
                result["events"].append({"stage": "empty_response" if empty else "truncated_response", "step": step, "finish_reason": turn.finish_reason, "reason": "completion_token_limit" if turn.finish_reason == "length" else "empty_model_response"})
                messages.append({"role": "user", "content": (
                    ("Your preceding response was empty. " if empty else "Your preceding response was truncated and is not a completed deliverable. ")
                    + ("The provider reported completion-token exhaustion. " if turn.finish_reason == "length" else "")
                    + "Continue with a concise tool action or a final report grounded in actual execution. Avoid spending the entire response budget on internal planning."
                )})
                continue
            if not turn.tool_calls:
                # A harness review gets a repair turn even if the model forgot to validate.
                if config.review == "public_checks" and reviewed_version != result["pipeline_sql"]:
                    validation = execute("validate_output", {}, "harness_review", step)
                    reviewed_version = result["pipeline_sql"]
                    messages.append({"role": "user", "content": "Public validation of the current output: " + json.dumps(validation, ensure_ascii=False) + ". Correct the pipeline if necessary; otherwise give the final report."})
                    continue
                result["output"] = turn.content
                break
            for call in turn.tool_calls:
                try:
                    args = json.loads(call.arguments)
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be an object")
                    observation = execute(call.name, args, "tool", step)
                except (ValueError, TypeError) as exc:
                    observation = {"error": f"invalid arguments: {exc}"}
                    result["events"].append({"stage": "invalid_tool_arguments", "name": call.name, "arguments": call.arguments, "output": observation})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(observation, ensure_ascii=False)})
        if config.review == "public_checks":
            execute("validate_output", {}, "final_public_check")
        if not result["output"]:
            result["budget_exhausted"] = True
            messages.append({"role": "user", "content": "Execution budget exhausted. Give a concise final report of actual execution, checks and unresolved issues. Do not claim unobserved success."})
            result["output"] = complete("forced_final").content
        if result["pipeline_sql"] is None:
            result["error"] = "no_pipeline_executed"
        result["rows"] = getattr(env, "output", [])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["rows"] = getattr(env, "output", [])
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
    return result
