"""Bounded, measured search. Proposals see train traces; test is rejected."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from .etl_agent import ETLHarnessConfig, run_agent
from .model import ModelClient

MUTABLE_FIELDS = {"prompt_patch", "workflow", "review", "tools", "max_steps", "max_tokens"}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(config: ETLHarnessConfig) -> str:
    payload = config.model_dump(mode="json", exclude={"name"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def task_fingerprint(task: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    public = {key: value for key, value in task.items() if not key.startswith("_")}
    digest.update(json.dumps(public, sort_keys=True).encode())
    if "_task_dir" in task:
        base = Path(task["_task_dir"])
        for source in task.get("sources", []):
            digest.update(source["path"].encode())
            digest.update((base / source["path"]).read_bytes())
    return digest.hexdigest()


def _load(task):
    if isinstance(task, (str, Path)):
        from .etl_env import load_task
        return load_task(task)
    return task


def _score(grade: dict[str, Any]) -> float:
    for key in ("score", "cell_accuracy", "row_accuracy"):
        if key in grade:
            return float(grade[key])
    return float(bool(grade.get("passed", grade.get("exact_match", False))))


def evaluate_tasks(tasks, config: ETLHarnessConfig, client: ModelClient, grade_task: Callable, output_dir: str | Path) -> dict[str, Any]:
    """The grader runs after the agent, on the same environment, outside context."""
    from .etl_env import ETLEnvironment
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for item in tasks:
        task = _load(item)
        env = ETLEnvironment(task)
        try:
            result = run_agent(task, config, client, environment=env)
            grade = grade_task(task, result, env)
            records.append({"task_id": task.get("id"), "split": task.get("split"), "task_sha256": task_fingerprint(task), "result": result, "grade": grade})
        finally:
            if hasattr(env, "close"):
                env.close()
        # Checkpoint after every task, including actual failed trajectories.
        _write(output_dir / "records.json", records)
    count = len(records)
    summary = {
        "tasks": count,
        "model": getattr(client, "model_name", "test-client"),
        "score": sum(_score(r["grade"]) for r in records) / max(count, 1),
        "pass_rate": sum(bool(r["grade"].get("passed", r["grade"].get("exact_match", False))) for r in records) / max(count, 1),
        "avg_llm_calls": sum(r["result"]["llm_calls"] for r in records) / max(count, 1),
        "avg_tool_calls": sum(r["result"]["tool_calls"] for r in records) / max(count, 1),
        "input_tokens": sum(r["result"]["input_tokens"] for r in records),
        "output_tokens": sum(r["result"]["output_tokens"] for r in records),
        "latency_ms": sum(r["result"]["latency_ms"] for r in records),
        "errors": sum(bool(r["result"]["error"]) for r in records),
    }
    _write(output_dir / "summary.json", summary)
    config.save(output_dir / "config.json")
    return {"summary": summary, "records": records, "output_dir": str(output_dir)}


def _feedback(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    # Gold tables and grader internals are not exported into proposal messages.
    feedback = []
    for record in evaluation["records"]:
        result = record["result"]
        feedback.append({
            "task_id": record["task_id"], "score": _score(record["grade"]),
            "passed": bool(record["grade"].get("passed", False)),
            "error": result["error"], "public_validation": result["public_validation"],
            "events": result["events"], "output": result["output"],
        })
    return feedback


def optimize(train_tasks, base_config: ETLHarnessConfig | dict, client: ModelClient, output_dir: str | Path, *, grade_task: Callable, rounds: int = 1, proposals_per_round: int = 2, dev_tasks=None, call_penalty: float = 0.002) -> dict[str, Any]:
    """Train-only proposals and execution; optional dev chooses among candidates.

    grade_task(task, result, environment) is an external objective grader callback.
    It must not mutate or feed hidden data back to the task agent.
    """
    if not 1 <= rounds <= 5 or not 1 <= proposals_per_round <= 4:
        raise ValueError("rounds must be 1..5; proposals_per_round must be 1..4")
    train_tasks = [_load(task) for task in train_tasks]
    dev_tasks = [_load(task) for task in (dev_tasks or [])]
    if not train_tasks or any(task.get("split") != "train" for task in train_tasks):
        raise ValueError("optimizer requires nonempty train split only")
    if any(task.get("split") not in {"dev", "validation"} for task in dev_tasks):
        raise ValueError("selection accepts dev/validation only; test is never searched")
    if not 0 <= call_penalty <= 0.1:
        raise ValueError("call_penalty must be 0..0.1")
    if not isinstance(base_config, ETLHarnessConfig):
        base_config = ETLHarnessConfig.model_validate(base_config)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "optimization_trace.json").exists():
        raise FileExistsError("search output already exists; use a new directory")
    trials, proposals_log = [], []
    configs, evaluations = {}, {}
    seen = set()

    def objective(summary):
        return summary["score"] - call_penalty * summary["avg_llm_calls"]

    def selection_key(summary):
        # Complete correctness (including metamorphic checks) takes precedence
        # over partial row accuracy and model-call savings.
        return (summary["pass_rate"], objective(summary))

    def measure(config, hypothesis, diff, round_number, parent):
        number = len(trials)
        evaluation = evaluate_tasks(train_tasks, config, client, grade_task, root / f"trial-{number:03d}" / "train")
        trial = {"number": number, "round": round_number, "parent": parent, "hypothesis": hypothesis, "diff": diff, "fingerprint": fingerprint(config), "train_summary": evaluation["summary"], "train_objective": objective(evaluation["summary"]), "train_selection_key": list(selection_key(evaluation["summary"])), "selected": False, "run_dir": str(root / f"trial-{number:03d}")}
        configs[number], evaluations[number] = config, evaluation
        seen.add(trial["fingerprint"])
        trials.append(trial)
        _write(root / "optimization_trace.json", trials)
        return number

    best = measure(base_config, "Measured baseline", {}, 0, None)
    for round_number in range(1, rounds + 1):
        parent_number = best
        parent = configs[parent_number]
        request = {
            "instruction": f"Propose {proposals_per_round} distinct harness changes addressing observed train failures. Return JSON {{proposals:[{{hypothesis:string,patch:object}}]}}. Change 1..3 fields. Do not encode task IDs, row values, exact SQL solutions or hidden answers. At least one proposal should alter workflow, review, tools or max_steps. Hold the task model fixed.",
            "mutable_fields": sorted(MUTABLE_FIELDS), "schema": ETLHarnessConfig.model_json_schema(),
            "parent_config": parent.model_dump(mode="json"),
            "train_feedback": _feedback(evaluations[best]),
            "history": trials,
        }
        started = time.perf_counter()
        try:
            turn = client.complete([{"role": "system", "content": "You optimize an ETL agent harness using measured train trajectories. JSON only."}, {"role": "user", "content": json.dumps(request, ensure_ascii=False)}], temperature=0, response_format={"type": "json_object"}, max_tokens=8000)
            log = {"round": round_number, "content": turn.content, "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens, "finish_reason": turn.finish_reason, "latency_ms": round((time.perf_counter() - started) * 1000, 2), "rejections": []}
            proposals_log.append(log)
            proposals = json.loads(turn.content).get("proposals", [])
            if not isinstance(proposals, list):
                raise ValueError("proposals must be an array")
            for proposal in proposals[:proposals_per_round]:
                try:
                    patch = proposal["patch"]
                    hypothesis = proposal["hypothesis"]
                    if not isinstance(patch, dict) or not 1 <= len(patch) <= 3 or not set(patch) <= MUTABLE_FIELDS:
                        raise ValueError("invalid patch fields")
                    if not isinstance(hypothesis, str) or not hypothesis.strip() or len(hypothesis) > 2000:
                        raise ValueError("hypothesis required, <=2000 chars")
                    candidate = ETLHarnessConfig.model_validate({**parent.model_dump(), **patch, "name": f"meta_r{round_number}_t{len(trials)}"})
                    diff = {key: {"from": getattr(parent, key), "to": getattr(candidate, key)} for key in patch if getattr(parent, key) != getattr(candidate, key)}
                    if not diff or fingerprint(candidate) in seen:
                        raise ValueError("unchanged or duplicate config")
                except Exception as exc:
                    log["rejections"].append({"proposal": proposal, "error": str(exc)})
                    continue
                number = measure(candidate, hypothesis, diff, round_number, parent_number)
                if trials[number]["train_selection_key"] > trials[best]["train_selection_key"]:
                    best = number
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if proposals_log and proposals_log[-1].get("round") == round_number and "content" in proposals_log[-1]:
                proposals_log[-1]["error"] = error
            else:
                proposals_log.append({"round": round_number, "error": error, "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
        _write(root / "proposals.json", proposals_log)

    if dev_tasks:
        # Dev results are never included in proposal history. Selection occurs once.
        for trial in trials:
            evaluation = evaluate_tasks(dev_tasks, configs[trial["number"]], client, grade_task, Path(trial["run_dir"]) / "dev")
            trial["dev_summary"] = evaluation["summary"]
            trial["dev_objective"] = objective(evaluation["summary"])
            trial["dev_selection_key"] = list(selection_key(evaluation["summary"]))
        best = max(range(len(trials)), key=lambda i: trials[i]["dev_selection_key"])
    trials[best]["selected"] = True
    config = configs[best].model_copy(deep=True)
    config.name = "selected_meta_harness"
    config.save(root / "best_config.json")
    _write(root / "optimization_trace.json", trials)
    summary = {"best_trial": best, "selection_split": "dev" if dev_tasks else "train", "selection_rule": "Lexicographic: maximize complete pass_rate first, then row score - call_penalty * average LLM calls", "train_only_proposals": True, "test_tasks_evaluated": 0, "trials": len(trials), "call_penalty": call_penalty, "proposal_calls": len(proposals_log), "proposal_input_tokens": sum(log.get("input_tokens", 0) for log in proposals_log), "proposal_output_tokens": sum(log.get("output_tokens", 0) for log in proposals_log), "proposal_errors": sum("error" in log for log in proposals_log)}
    _write(root / "summary.json", summary)
    return {"best_config": config.model_dump(mode="json"), "trials": trials, "summary": summary, "output_dir": str(root)}
