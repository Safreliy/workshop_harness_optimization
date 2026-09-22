from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .cases import BenchmarkCase
from .config import HarnessConfig, SearchConfig
from .graders import extract_json_object
from .model import ModelClient
from .prompts import render_task
from .runner import (
    BenchmarkRecord,
    RunnerResult,
    config_fingerprint,
    run_benchmark,
    summarize,
)
from .settings import Settings


MUTABLE_FIELDS = {
    "prompt_profile",
    "prompt_patch",
    "routing",
    "workflow",
    "context_strategy",
    "context_partition",
    "review_mode",
    "tool_description_profile",
    "enabled_tools",
    "tool_description_overrides",
    "max_steps",
    "max_context_chars",
}


@dataclass
class Trial:
    number: int
    round: int
    source: str
    operator: str
    hypothesis: str
    parent_number: int | None
    parent_fingerprint: str | None
    config_name: str
    fingerprint: str
    score: float
    report_score: float
    sql_score: float
    trajectory_score: float
    avg_llm_calls: float
    objective: float
    selected: bool
    run_dir: str
    changed_fields: list[str]
    changed: dict[str, Any]


@dataclass
class CandidateState:
    trial: Trial
    config: HarnessConfig
    result: RunnerResult


@dataclass
class ModelProposal:
    parent_number: int
    operator: str
    hypothesis: str
    config: HarnessConfig
    changed: dict[str, Any]


@dataclass
class OptimizationResult:
    best_config: HarnessConfig
    trials: list[Trial]
    output_dir: Path


class MetaHarnessOptimizer:
    """Model-generated, train-only search over a bounded harness schema."""

    def __init__(
        self,
        settings: Settings,
        search: SearchConfig,
        cases: list[BenchmarkCase],
        output_dir: str | Path,
        max_trials: int | None = None,
        resume: bool = False,
    ):
        if any("eval" in case.id or "test" in case.id for case in cases):
            raise ValueError("optimizer accepts train cases only")
        self.settings = settings
        self.search = search
        self.cases = cases
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_trials = max_trials
        self.model = ModelClient(settings)
        self.trials: list[Trial] = []
        self.states: dict[int, CandidateState] = {}
        self.cache: dict[str, tuple[RunnerResult, float]] = {}
        self.beam: list[int] = []
        self.max_raw_task_chars = max((len(render_task(case)) for case in cases), default=0)
        trace_path = self.output_dir / "optimization_trace.json"
        if trace_path.exists():
            if not resume:
                raise FileExistsError(
                    f"optimization trace already exists in {self.output_dir}; use --resume"
                )
            self._load_existing(trace_path)

    def _load_existing(self, trace_path: Path) -> None:
        raw_trace = trace_path.read_text(encoding="utf-8")
        payload = json.loads(raw_trace)
        loaded: list[tuple[Trial, HarnessConfig, RunnerResult]] = []
        failed_rounds: list[int] = []
        for item in payload:
            trial = Trial(**item)
            run_dir = Path(trial.run_dir)
            config = HarnessConfig.from_yaml(run_dir / "config.yaml")
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            records = [
                BenchmarkRecord(**json.loads(line))
                for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            result = RunnerResult(run_dir=run_dir, records=records, summary=summary)
            if any(record.error for record in records):
                failed_rounds.append(trial.round)
            loaded.append((trial, config, result))
        if failed_rounds:
            recovery_round = min(failed_rounds)
            backup = self.output_dir / "optimization_trace.before_infra_recovery.json"
            if not backup.exists():
                backup.write_text(raw_trace, encoding="utf-8")
            loaded = [item for item in loaded if item[0].round < recovery_round]
        for trial, config, result in loaded:
            state = CandidateState(trial=trial, config=config, result=result)
            self.trials.append(trial)
            self.states[trial.number] = state
            self.cache[trial.fingerprint] = (result, trial.objective)
        self.beam = [trial.number for trial in self.trials if trial.selected]
        if not self.beam and self.trials:
            self.beam = [max(self.trials, key=lambda trial: trial.objective).number]
        if failed_rounds:
            self._save_trace()

    def objective(self, result: RunnerResult) -> float:
        return round(
            float(result.summary["score"])
            - self.search.objective_call_penalty
            * max(float(result.summary["avg_llm_calls"]) - 1.0, 0.0),
            6,
        )

    @staticmethod
    def _kind_score(result: RunnerResult, kind: str) -> float:
        details = result.summary.get("by_kind", {}).get(kind, {})
        return float(details.get("score", result.summary["score"]))

    @staticmethod
    def _config_diff(
        parent: HarnessConfig, candidate: HarnessConfig
    ) -> dict[str, dict[str, Any]]:
        before = parent.model_dump(mode="json")
        after = candidate.model_dump(mode="json")
        return {
            field: {"from": before[field], "to": after[field]}
            for field in sorted(MUTABLE_FIELDS)
            if before[field] != after[field]
        }

    def _evaluate(
        self,
        *,
        config: HarnessConfig,
        round_number: int,
        source: str,
        operator: str,
        hypothesis: str,
        parent_number: int | None,
        changed: dict[str, Any],
    ) -> CandidateState:
        fingerprint = config_fingerprint(config)
        if fingerprint in self.cache:
            result, objective = self.cache[fingerprint]
        else:
            result = self._run_benchmark_resilient(
                config=config,
                run_label=f"train-{len(self.trials):02d}-{source}-{operator}",
            )
            objective = self.objective(result)
            self.cache[fingerprint] = (result, objective)
        parent_fingerprint = (
            self.states[parent_number].trial.fingerprint
            if parent_number is not None
            else None
        )
        trial = Trial(
            number=len(self.trials),
            round=round_number,
            source=source,
            operator=operator,
            hypothesis=hypothesis,
            parent_number=parent_number,
            parent_fingerprint=parent_fingerprint,
            config_name=config.name,
            fingerprint=fingerprint,
            score=float(result.summary["score"]),
            report_score=self._kind_score(result, "report"),
            sql_score=self._kind_score(result, "sql"),
            trajectory_score=float(result.summary["trajectory_score"]),
            avg_llm_calls=float(result.summary["avg_llm_calls"]),
            objective=objective,
            selected=False,
            run_dir=str(result.run_dir),
            changed_fields=sorted(changed),
            changed=changed,
        )
        state = CandidateState(trial=trial, config=config, result=result)
        self.trials.append(trial)
        self.states[trial.number] = state
        self._save_trace()
        return state

    def _run_benchmark_resilient(
        self,
        *,
        config: HarnessConfig,
        run_label: str,
    ) -> RunnerResult:
        result = run_benchmark(
            settings=self.settings,
            config=config,
            cases=self.cases,
            artifact_root=self.output_dir / "runs",
            run_label=run_label,
            model=self.model,
        )
        retry_runs: list[str] = []
        cases_by_id = {case.id: case for case in self.cases}
        for attempt in range(1, 4):
            failed_ids = [record.case_id for record in result.records if record.error]
            if not failed_ids:
                break
            retry = run_benchmark(
                settings=self.settings,
                config=config,
                cases=[cases_by_id[case_id] for case_id in failed_ids],
                artifact_root=self.output_dir / "infrastructure_retries",
                run_label=f"{run_label}-retry-{attempt}",
                model=self.model,
            )
            retry_runs.append(str(retry.run_dir))
            replacements = {record.case_id: record for record in retry.records}
            result.records = [
                replacements.get(record.case_id, record) for record in result.records
            ]
        remaining_errors = [record.error for record in result.records if record.error]
        if remaining_errors:
            raise RuntimeError(
                "benchmark still has agent errors after infrastructure retries: "
                + "; ".join(str(error) for error in remaining_errors[:3])
            )
        if retry_runs:
            original_metadata = result.summary
            result.summary = summarize(result.records, config)
            result.summary.update(
                {
                    "model": self.settings.model_name,
                    "created_at": original_metadata.get("created_at"),
                    "run_label": original_metadata.get("run_label"),
                    "infrastructure_retries": retry_runs,
                }
            )
            (result.run_dir / "records.jsonl").write_text(
                "".join(
                    json.dumps(asdict(record), ensure_ascii=False, default=str) + "\n"
                    for record in result.records
                ),
                encoding="utf-8",
            )
            (result.run_dir / "summary.json").write_text(
                json.dumps(result.summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return result

    def _save_trace(self) -> None:
        (self.output_dir / "optimization_trace.json").write_text(
            json.dumps([asdict(trial) for trial in self.trials], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _budget_available(self) -> bool:
        return self.max_trials is None or len(self.trials) < self.max_trials

    @staticmethod
    def _result_feedback(result: RunnerResult) -> dict[str, Any]:
        issue_counts: dict[str, int] = {}
        failures: list[dict[str, Any]] = []
        for record in result.records:
            for issue in record.trajectory_details.get("issues", []):
                issue_counts[issue] = issue_counts.get(issue, 0) + 1
            if not record.passed:
                failures.append(
                    {
                        "kind": record.kind,
                        "score": record.score,
                        "output_score": record.output_score,
                        "trajectory_score": record.trajectory_score,
                        "trajectory_issues": record.trajectory_details.get("issues", []),
                        "failed_invariants": [
                            check.get("target")
                            for check in record.grade_details.get("checks", [])
                            if not check.get("ok")
                        ][:8],
                        "sql_signals": {
                            key: record.grade_details.get(key)
                            for key in (
                                "equivalent",
                                "cost_ratio",
                                "expected_index_score",
                                "expected_sql_score",
                            )
                            if key in record.grade_details
                        },
                    }
                )
        return {
            "trajectory_issue_counts": issue_counts,
            "failed_cases": failures,
        }

    def _history_feedback(self) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for trial in self.trials[-18:]:
            history.append(
                {
                    "number": trial.number,
                    "round": trial.round,
                    "parent_number": trial.parent_number,
                    "operator": trial.operator,
                    "hypothesis": trial.hypothesis,
                    "changed_fields": trial.changed_fields,
                    "selected": trial.selected,
                    "score": trial.score,
                    "report_score": trial.report_score,
                    "sql_score": trial.sql_score,
                    "trajectory_score": trial.trajectory_score,
                    "avg_llm_calls": trial.avg_llm_calls,
                    "objective": trial.objective,
                    **self._result_feedback(self.states[trial.number].result),
                }
            )
        return history

    def _proposal_prompt(
        self,
        round_number: int,
        still_needed: int,
        used_operators: set[str],
        validation_notes: list[str],
    ) -> str:
        parents = []
        for number in self.beam:
            state = self.states[number]
            parents.append(
                {
                    "number": number,
                    "config": state.config.model_dump(mode="json"),
                    "metrics": {
                        "score": state.trial.score,
                        "report_score": state.trial.report_score,
                        "sql_score": state.trial.sql_score,
                        "trajectory_score": state.trial.trajectory_score,
                        "avg_llm_calls": state.trial.avg_llm_calls,
                        "objective": state.trial.objective,
                    },
                    "feedback": self._result_feedback(state.result),
                }
            )
        return f"""You are the meta-optimizer of an agent harness. The task model is fixed.
Generate {still_needed} falsifiable harness mutations from the current parent beam.

You choose both the parent and the mutation. Do not use a predefined mutation list and do not encode case IDs, exact answers, or hidden grader logic. Prefer structural changes over prompt text when traces indicate a structural cause.

Return exactly one JSON object:
{{"proposals": [{{
  "parent_number": 0,
  "operator": "short mechanism name",
  "hypothesis": "what observed failure this should fix and why",
  "patch": {{"one_or_more_config_fields": "new value"}}
}}]}}

Rules for this portfolio:
- choose only parent_number values present below;
- each proposal must use a different operator and a materially different mechanism;
- at least one proposal must change a non-prompt architectural field;
- at most one proposal may change prompt_patch;
- change 1 to 4 fields, and only fields whose value actually differs from the parent;
- do not change name or temperature;
- avoid configurations and changed-field signatures already present in history;
- tool_description_overrides may only use known tool names;
- enabled_tools may only contain known tool names.

Mutable schema:
- prompt_profile: baseline | postgres_specialist
- prompt_patch: general instruction, maximum 6000 chars
- routing: none | task_kind
- workflow: react | plan_execute
- context_strategy: raw | requirements_ledger
- context_partition: shared | stage_isolated
- review_mode: none | domain_specialist
- tool_description_profile: terse | diagnostic
- enabled_tools: subset of [list_tables, describe_table, inspect_indexes, explain_query, run_readonly_query, database_context, trial_index_plan]
- tool_description_overrides: descriptions keyed by those known tool names
- max_steps: integer 1..12
- max_context_chars: integer 4000..200000

Current parent beam:
{json.dumps(parents, ensure_ascii=False)}

Complete search history, including rejected candidates and failed trajectories:
{json.dumps(self._history_feedback(), ensure_ascii=False)}

Operators already used in this round: {json.dumps(sorted(used_operators), ensure_ascii=False)}
Validation notes from earlier attempts in this round: {json.dumps(validation_notes[-8:], ensure_ascii=False)}
Round: {round_number}. Return JSON only."""

    def _semantic_validation_notes(
        self,
        parent: HarnessConfig,
        candidate: HarnessConfig,
        changed: dict[str, Any],
    ) -> list[str]:
        notes: list[str] = []
        if "context_partition" in changed and candidate.review_mode == "none":
            notes.append("context_partition is inactive while review_mode=none")
        if "tool_description_overrides" in changed:
            inactive_overrides = set(candidate.tool_description_overrides) - set(
                candidate.enabled_tools
            )
            if inactive_overrides:
                notes.append(
                    "tool_description_overrides target disabled tools: "
                    + ", ".join(sorted(inactive_overrides))
                )
        if "tool_description_profile" in changed and not candidate.enabled_tools:
            notes.append("tool_description_profile is inactive with no enabled tools")
        if "max_context_chars" in changed:
            lower_limit = min(parent.max_context_chars, candidate.max_context_chars)
            if lower_limit >= self.max_raw_task_chars:
                notes.append(
                    "max_context_chars is above every train task and cannot affect this run"
                )
        if "prompt_patch" in changed and not candidate.prompt_patch.strip():
            notes.append("prompt_patch must not be blank")
        return notes

    def _model_proposals(self, round_number: int) -> list[ModelProposal]:
        proposals: list[ModelProposal] = []
        used_operators: set[str] = set()
        validation_notes: list[str] = []
        prompt_patch_count = 0
        for _ in range(3):
            still_needed = self.search.proposals_per_round - len(proposals)
            if still_needed <= 0:
                break
            turn = self.model.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "You optimize the architecture and operating policy of an agent harness. "
                            "Produce a diverse, schema-valid JSON portfolio grounded in measured traces."
                        ),
                    },
                    {
                        "role": "user",
                        "content": self._proposal_prompt(
                            round_number,
                            still_needed,
                            used_operators,
                            validation_notes,
                        ),
                    },
                ],
                temperature=self.search.proposal_temperature,
            )
            try:
                payload = extract_json_object(turn.content)
            except Exception as exc:
                validation_notes.append(f"invalid JSON: {type(exc).__name__}")
                continue
            raw_proposals = payload.get("proposals")
            if not isinstance(raw_proposals, list):
                validation_notes.append("top-level proposals must be an array")
                continue
            for raw in raw_proposals:
                if len(proposals) >= self.search.proposals_per_round:
                    break
                if not isinstance(raw, dict):
                    validation_notes.append("proposal must be an object")
                    continue
                try:
                    parent_number = int(raw["parent_number"])
                    operator = str(raw["operator"]).strip().lower()
                    hypothesis = str(raw["hypothesis"]).strip()
                    patch = raw["patch"]
                except (KeyError, TypeError, ValueError):
                    validation_notes.append("proposal metadata is incomplete")
                    continue
                if parent_number not in self.beam:
                    validation_notes.append(f"parent {parent_number} is not in the beam")
                    continue
                if not operator or operator in used_operators:
                    validation_notes.append(f"operator must be unique: {operator!r}")
                    continue
                if not hypothesis or not isinstance(patch, dict):
                    validation_notes.append("hypothesis and patch are required")
                    continue
                fields = set(patch)
                if not 1 <= len(fields) <= 4 or not fields <= MUTABLE_FIELDS:
                    validation_notes.append(f"invalid mutable fields: {sorted(fields)}")
                    continue
                if "prompt_patch" in fields and prompt_patch_count >= 1:
                    validation_notes.append("only one prompt_patch proposal is allowed per round")
                    continue
                parent = self.states[parent_number].config
                merged = parent.model_dump(mode="json")
                merged.update(patch)
                safe_operator = re.sub(r"[^a-z0-9]+", "_", operator).strip("_")[:32]
                merged["name"] = (
                    f"meta_r{round_number:02d}_p{len(proposals) + 1:02d}_{safe_operator}"
                )
                try:
                    candidate = HarnessConfig.model_validate(merged)
                except Exception as exc:
                    validation_notes.append(f"schema rejected {operator}: {exc}")
                    continue
                changed = self._config_diff(parent, candidate)
                if not changed:
                    validation_notes.append(f"{operator} produced no actual change")
                    continue
                semantic_notes = self._semantic_validation_notes(parent, candidate, changed)
                if semantic_notes:
                    validation_notes.extend(f"{operator}: {note}" for note in semantic_notes)
                    continue
                fingerprint = config_fingerprint(candidate)
                if fingerprint in self.cache or any(
                    config_fingerprint(item.config) == fingerprint for item in proposals
                ):
                    validation_notes.append(f"{operator} duplicated an evaluated config")
                    continue
                proposals.append(
                    ModelProposal(
                        parent_number=parent_number,
                        operator=operator,
                        hypothesis=hypothesis,
                        config=candidate,
                        changed=changed,
                    )
                )
                used_operators.add(operator)
                if "prompt_patch" in changed:
                    prompt_patch_count += 1
        return proposals

    @staticmethod
    def _dominates(left: CandidateState, right: CandidateState) -> bool:
        left_metrics = (
            left.trial.report_score,
            left.trial.sql_score,
            left.trial.trajectory_score,
            -left.trial.avg_llm_calls,
        )
        right_metrics = (
            right.trial.report_score,
            right.trial.sql_score,
            right.trial.trajectory_score,
            -right.trial.avg_llm_calls,
        )
        return all(a >= b for a, b in zip(left_metrics, right_metrics)) and any(
            a > b for a, b in zip(left_metrics, right_metrics)
        )

    def _select_beam(self, candidate_numbers: list[int]) -> list[int]:
        states = [self.states[number] for number in dict.fromkeys(candidate_numbers)]
        frontier = [
            state
            for state in states
            if not any(
                self._dominates(other, state)
                for other in states
                if other.trial.number != state.trial.number
            )
        ]
        frontier.sort(key=lambda state: state.trial.objective, reverse=True)
        selected = frontier[: self.search.beam_width]
        if len(selected) < self.search.beam_width:
            selected_numbers = {state.trial.number for state in selected}
            remainder = sorted(
                (state for state in states if state.trial.number not in selected_numbers),
                key=lambda state: state.trial.objective,
                reverse=True,
            )
            selected.extend(remainder[: self.search.beam_width - len(selected)])
        return [state.trial.number for state in selected]

    def optimize(self) -> OptimizationResult:
        if not self.trials:
            baseline = HarnessConfig.from_yaml(self.search.base_config)
            baseline_state = self._evaluate(
                config=baseline,
                round_number=0,
                source="baseline",
                operator="baseline",
                hypothesis="Measured starting point",
                parent_number=None,
                changed={},
            )
            baseline_state.trial.selected = True
            self.beam = [baseline_state.trial.number]
            self._save_trace()

        completed_round = max((trial.round for trial in self.trials), default=0)
        for round_number in range(completed_round + 1, self.search.model_rounds + 1):
            if not self._budget_available():
                break
            proposals = self._model_proposals(round_number)
            evaluated: list[int] = []
            for proposal in proposals:
                if not self._budget_available():
                    break
                state = self._evaluate(
                    config=proposal.config,
                    round_number=round_number,
                    source="model_portfolio",
                    operator=proposal.operator,
                    hypothesis=proposal.hypothesis,
                    parent_number=proposal.parent_number,
                    changed=proposal.changed,
                )
                evaluated.append(state.trial.number)
            if not evaluated:
                continue
            self.beam = self._select_beam(self.beam + evaluated)
            for trial in self.trials:
                trial.selected = trial.number in self.beam
            self._save_trace()

        best_state = max(self.states.values(), key=lambda state: state.trial.objective)
        best_config = best_state.config.model_copy(deep=True)
        best_config.name = "trained_harness"
        best_config.to_yaml(self.output_dir / "best_config.yaml")
        (self.output_dir / "best_summary.json").write_text(
            json.dumps(
                {
                    "search_mode": "model_generated_portfolio",
                    "train_only": True,
                    "best_trial": best_state.trial.number,
                    "best_objective": best_state.trial.objective,
                    "best_train_summary": best_state.result.summary,
                    "trials": len(self.trials),
                    "rounds": self.search.model_rounds,
                    "proposals_per_round": self.search.proposals_per_round,
                    "final_beam": self.beam,
                    "eval_cases_evaluated": 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return OptimizationResult(
            best_config=best_config,
            trials=self.trials,
            output_dir=self.output_dir,
        )
