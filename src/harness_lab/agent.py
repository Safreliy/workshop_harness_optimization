from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cases import BenchmarkCase
from .config import HarnessConfig
from .model import ModelClient, ModelTurn
from .prompts import (
    ledger_prompt,
    planning_prompt,
    render_task,
    reviewer_prompt,
    system_prompt,
)
from .tools import ToolExecutor, build_tool_schemas


@dataclass
class AgentEvent:
    stage: str
    data: dict[str, Any]


@dataclass
class AgentResult:
    case_id: str
    output: str
    events: list[AgentEvent] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    def add_turn(self, turn: ModelTurn) -> None:
        self.llm_calls += 1
        self.input_tokens += turn.input_tokens
        self.output_tokens += turn.output_tokens


class HarnessAgent:
    def __init__(self, model: ModelClient, tools: ToolExecutor):
        self.model = model
        self.tools = tools

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        head = max_chars // 3
        tail = max_chars - head
        return (
            text[:head]
            + "\n\n[... context clipped by harness ...]\n\n"
            + text[-tail:]
        )

    def _call(
        self,
        result: AgentResult,
        messages: list[dict[str, Any]],
        config: HarnessConfig,
        schemas: list[dict[str, Any]] | None = None,
    ) -> ModelTurn:
        turn = self.model.complete(
            messages,
            tools=schemas,
            temperature=config.temperature,
        )
        result.add_turn(turn)
        return turn

    def run(self, case: BenchmarkCase, config: HarnessConfig) -> AgentResult:
        result = AgentResult(case_id=case.id, output="")
        try:
            context_override: str | None = None
            if case.kind == "report" and config.context_strategy == "requirements_ledger":
                ledger_turn = self._call(
                    result,
                    [
                        {
                            "role": "system",
                            "content": "You compile faithful, loss-minimizing analytics requirements ledgers.",
                        },
                        {"role": "user", "content": ledger_prompt(case)},
                    ],
                    config,
                )
                context_override = (
                    "COMPILED REQUIREMENTS LEDGER\n"
                    + ledger_turn.content
                    + "\n\nLATEST SOURCE TURNS (for correction audit)\n"
                    + "\n".join(
                        f"[{len(case.dialogue) - len(case.dialogue[-4:]) + i:02d}] "
                        f"{turn.role}: {turn.content}"
                        for i, turn in enumerate(case.dialogue[-4:], start=1)
                    )
                )
                result.events.append(
                    AgentEvent(
                        stage="context_compaction",
                        data={"strategy": "requirements_ledger", "ledger": ledger_turn.content},
                    )
                )

            task_text = self._clip(
                render_task(case, context_override=context_override),
                config.max_context_chars,
            )
            sys_prompt = system_prompt(case, config)

            plan = ""
            if config.workflow == "plan_execute":
                plan_turn = self._call(
                    result,
                    [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": planning_prompt(case, task_text)},
                    ],
                    config,
                )
                plan = plan_turn.content
                result.events.append(
                    AgentEvent(stage="planning", data={"plan": plan_turn.content})
                )

            execution_system = sys_prompt
            if plan:
                execution_system += f"\n\nApproved execution plan:\n{plan}"
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": execution_system},
                {"role": "user", "content": task_text},
            ]

            schemas = build_tool_schemas(config)
            if case.kind == "report" and config.routing == "task_kind":
                schemas = []

            final_text = ""
            for step in range(1, config.max_steps + 1):
                turn = self._call(result, messages, config, schemas)
                messages.append(turn.as_message())
                if not turn.tool_calls:
                    final_text = turn.content
                    result.events.append(
                        AgentEvent(stage="final_candidate", data={"step": step})
                    )
                    break
                tool_names = []
                for call in turn.tool_calls:
                    tool_names.append(call.name)
                    tool_output = self.tools.execute(call.name, call.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": tool_output,
                        }
                    )
                    result.events.append(
                        AgentEvent(
                            stage="tool",
                            data={
                                "step": step,
                                "name": call.name,
                                "arguments": call.arguments,
                                "output": tool_output[:5000],
                            },
                        )
                    )
                result.events.append(
                    AgentEvent(stage="react_step", data={"step": step, "tools": tool_names})
                )

            if not final_text:
                messages.append(
                    {
                        "role": "user",
                        "content": "Tool budget is exhausted. Return the required final JSON now.",
                    }
                )
                final_turn = self._call(result, messages, config, schemas=None)
                final_text = final_turn.content
                result.events.append(
                    AgentEvent(stage="forced_final", data={"reason": "max_steps"})
                )

            if config.review_mode == "domain_specialist":
                review_task = render_task(case) if config.context_partition == "shared" else task_text
                review_turn = self._call(
                    result,
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a domain-specialist subagent. You receive an isolated review "
                                "context and must return a corrected deliverable, not a critique."
                            ),
                        },
                        {
                            "role": "user",
                            "content": reviewer_prompt(case, review_task, final_text),
                        },
                    ],
                    config,
                )
                final_text = review_turn.content
                result.events.append(
                    AgentEvent(
                        stage="domain_specialist_review",
                        data={"partition": config.context_partition},
                    )
                )

            result.output = final_text
            return result
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return result
