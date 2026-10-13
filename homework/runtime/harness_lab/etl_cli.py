"""Student entry point for the ETL agent and meta-harness experiments.

Run ``python -m harness_lab.etl_cli --help``. Private reference data is read only
by grading; it never enters the tool environment or the agent prompt.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import time

from .settings import Settings
from .model import ModelClient


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(path: Path):
    digest = hashlib.sha256()
    for source in sorted(p for p in path.parent.rglob("*") if p.is_file()):
        digest.update(source.relative_to(path.parent).as_posix().encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def task_paths(root: Path, split: str, limit: int | None = None):
    paths = sorted((root / "public" / split).glob("*/task.json"))
    if not paths:
        raise ValueError(f"No {split} tasks in {root}; run generate first")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not limit:
        return paths
    # A small smoke run should cover schema families rather than repeat one.
    families = {}
    for path in paths:
        family = json.loads(path.read_text(encoding="utf-8")).get("family_id", path.parent.name)
        families.setdefault(family, []).append(path)
    ordered = [group[i] for i in range(max(map(len, families.values()))) for group in families.values() if i < len(group)]
    return ordered[:limit]


def new_client(args):
    settings = Settings.load(args.env)
    if args.provider == "openrouter":
        if not args.model or not settings.openrouter_api_key:
            raise ValueError("OpenRouter requires --model and OPENROUTER_API_KEY")
        settings = replace(settings, api_key=settings.openrouter_api_key,
                           base_url="https://openrouter.ai/api/v1", model_name=args.model,
                           model_ca_file="", model_ca_allow_legacy=False)
    elif args.model:
        settings = replace(settings, model_name=args.model)
    return ModelClient(settings, timeout=90, max_attempts=2)


def summarize(records):
    n = len(records)
    return {
        "n": n,
        "passed": sum(bool(r.get("grade", {}).get("passed")) for r in records),
        "pass_rate": sum(bool(r.get("grade", {}).get("passed")) for r in records) / n if n else None,
        "mean_score": sum(float(r.get("grade", {}).get("score", 0)) for r in records) / n if n else None,
        "mean_llm_calls": sum(r.get("llm_calls", 0) for r in records) / n if n else None,
        "input_tokens": sum(r.get("input_tokens", 0) for r in records),
        "output_tokens": sum(r.get("output_tokens", 0) for r in records),
        "errors": sum(bool(r.get("error")) for r in records),
    }


def grade_for(root: Path):
    def grade(task, result, environment):
        task_id = Path(task).parent.name if isinstance(task, (str, Path)) else task.get("task_id", task.get("id"))
        return environment.grade(root / "private" / f"{task_id}.json")
    return grade


def command_generate(args):
    from .etl_data import generate_dataset
    report = generate_dataset(args.dataset, seed=args.seed, tasks_per_family=args.tasks_per_family)
    counts = {split: sum(task["split"] == split for task in report["tasks"]) for split in ("train", "dev", "test")}
    print(json.dumps({"dataset": str(args.dataset.resolve()), "seed": args.seed, "tasks": counts}, ensure_ascii=False, indent=2))


def export_pipeline(directory: Path, task_id: str, record: dict):
    if not isinstance(task_id, str) or not task_id or task_id in {".", ".."} or "/" in task_id or "\\" in task_id:
        raise ValueError("Task ID must be a single filename component")
    if record.get("pipeline_sql"):
        path = directory / "pipelines" / f"{task_id}.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record["pipeline_sql"].rstrip(";\n") + ";\n", encoding="utf-8")
    rows = record.get("rows", [])
    write_json(directory / "outputs" / f"{task_id}.json", rows)
    if rows:
        path = directory / "outputs" / f"{task_id}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def command_run(args):
    from .etl_agent import ETLHarnessConfig, run_agent
    from .etl_env import ETLEnvironment
    config = ETLHarnessConfig.model_validate(json.loads(args.config.read_text(encoding="utf-8")))
    paths = task_paths(args.dataset, args.split, args.limit)
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("Output directory is not empty; choose a new directory to preserve runs")
    client = new_client(args)
    records = []
    started = time.perf_counter()
    for index, path in enumerate(paths, 1):
        input_fingerprint = fingerprint(path)
        environment = ETLEnvironment(path)
        try:
            record = run_agent(path, config, client, environment=environment)
            record["grade"] = grade_for(args.dataset)(path, record, environment)
            record["task_sha256"] = input_fingerprint
            if fingerprint(path) != input_fingerprint:
                raise ValueError("Task sources changed during execution; this run is invalid")
            record["split"] = args.split
            records.append(record)
            write_json(args.output / "records" / f"{path.parent.name}.json", record)
            export_pipeline(args.output, path.parent.name, record)
            print(f"{index}/{len(paths)} {path.parent.name}: pass={record['grade'].get('passed')} score={record['grade'].get('score')} calls={record.get('llm_calls')}", flush=True)
        finally:
            if hasattr(environment, "close"):
                environment.close()
    report = {"config": config.model_dump(), "model": client.model_name, "provider": args.provider, "split": args.split,
              "dataset": str(args.dataset.resolve()), "seconds": time.perf_counter() - started,
              "summary": summarize(records), "task_ids": [p.parent.name for p in paths]}
    write_json(args.output / "summary.json", report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


def command_optimize(args):
    from .etl_agent import ETLHarnessConfig
    from .etl_optimizer import optimize
    config = ETLHarnessConfig.model_validate(json.loads(args.config.read_text(encoding="utf-8")))
    train = task_paths(args.dataset, "train", args.limit)
    dev = task_paths(args.dataset, "dev", args.dev_limit)
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("Output directory is not empty; choose a new directory")
    result = optimize(train, config, new_client(args), args.output, grade_task=grade_for(args.dataset),
                      rounds=args.rounds, proposals_per_round=args.proposals, dev_tasks=dev)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _comparison_run(directory: Path):
    """Reject incomparable or incomplete runs before writing a comparison report."""
    metadata = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError(f"Invalid run metadata in {directory}")
    for field in ("model", "provider", "split"):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError(f"Run {directory} must declare {field}")
    if metadata["split"] not in {"train", "dev", "test"}:
        raise ValueError(f"Invalid split in {directory}")
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((directory / "records").glob("*.json"))]
    if not records:
        raise ValueError(f"Run {directory} contains no records; comparison requires completed runs")
    task_ids = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("task_id"), str) or not record["task_id"]:
            raise ValueError(f"Invalid task record in {directory}")
        task_ids.append(record["task_id"])
        digest = record.get("task_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"Missing or invalid task fingerprint in {directory}")
        if record.get("model") != metadata["model"] or record.get("split") != metadata["split"]:
            raise ValueError(f"Record model/split disagrees with run metadata in {directory}")
        if "provider" in record and record["provider"] != metadata["provider"]:
            raise ValueError(f"Record provider disagrees with run metadata in {directory}")
        grade = record.get("grade")
        score = grade.get("score") if isinstance(grade, dict) else None
        if not isinstance(grade, dict) or type(grade.get("passed")) is not bool or type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f"Missing or invalid grade in {directory}")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError(f"Duplicate task IDs in {directory}")
    declared = metadata.get("task_ids")
    if not isinstance(declared, list) or any(not isinstance(value, str) for value in declared) or len(declared) != len(set(task_ids)) or set(declared) != set(task_ids):
        raise ValueError(f"Run metadata task IDs disagree with records in {directory}")
    if not isinstance(metadata.get("summary"), dict) or metadata["summary"].get("n") != len(records):
        raise ValueError(f"Run metadata record count disagrees with records in {directory}")
    return directory, metadata, records


def command_compare(args):
    if not args.runs:
        raise ValueError("At least one completed run is required")
    runs = []
    for directory in args.runs:
        runs.append(_comparison_run(directory))
    conditions = {(metadata["model"], metadata["provider"], metadata["split"]) for _, metadata, _ in runs}
    if len(conditions) != 1:
        raise ValueError("Harness comparisons require the same model, provider and split")
    identities = [{(r["task_id"], r["task_sha256"]) for r in records} for _, _, records in runs]
    if any(ids != identities[0] for ids in identities[1:]):
        raise ValueError("Runs must contain the same task IDs and task fingerprints")
    model, provider, split = next(iter(conditions))
    lines = ["# ETL harness comparison", "", f"Model: {model}; provider: {provider}; split: {split}.", "",
             "One measured run per configuration; this does not estimate run-to-run variance.", "",
             "| Harness | Run directory | Tasks | Passed | Pass rate | Mean score | Mean LLM calls | Input/output tokens |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for directory, meta, records in runs:
        s = summarize(records)
        name = meta.get("config", {}).get("name", directory.name)
        lines.append(f"| {name} | {directory.name} | {s['n']} | {s['passed']} | {s['pass_rate']:.1%} | {s['mean_score']:.3f} | {s['mean_llm_calls']:.1f} | {s['input_tokens']}/{s['output_tokens']} |")
    lines.extend(["", "## Per-task outcomes", ""])
    by_run = [{r["task_id"]: r for r in records} for _, _, records in runs]
    for task_id in sorted(by_run[0]):
        outcomes = [f"{directory.name}: {mapping[task_id]['grade'].get('passed')} ({mapping[task_id]['grade'].get('score')})" for (directory, _, _), mapping in zip(runs, by_run)]
        lines.append(f"- {task_id}: " + "; ".join(outcomes))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output.resolve())


def command_check_sql(args):
    from .etl_env import ETLEnvironment
    paths = task_paths(args.dataset, args.split)
    path = next((p for p in paths if p.parent.name == args.task), None)
    if path is None:
        raise ValueError("Task not found in selected split")
    with ETLEnvironment(path) as environment:
        result = environment.call("execute_pipeline", {"sql": args.sql.read_text(encoding="utf-8")})
        report = {"task_id": args.task, "execution": result, "public_validation": environment.validate_output(),
                  "grade": grade_for(args.dataset)(path, result, environment)}
    write_json(args.output, report)
    print(json.dumps(report["grade"], ensure_ascii=False, indent=2))


def command_verify(args):
    from .etl_data import reference_sql, load_task
    from .etl_env import ETLEnvironment
    records = []
    for split in ("train", "dev", "test"):
        for path in task_paths(args.dataset, split):
            task = load_task(path)
            with ETLEnvironment(task) as environment:
                execution = environment.call("execute_pipeline", {"sql": reference_sql(task)})
                grade = grade_for(args.dataset)(task, execution, environment)
                records.append({"task_id": path.parent.name, "split": split, "passed": grade["passed"]})
    report = {"kind": "instructor reference self-test, NOT an LLM agent benchmark", "n": len(records),
              "passed": sum(row["passed"] for row in records), "records": records}
    write_json(args.output, report)
    print(f"Instructor reference: {report['passed']}/{report['n']} passed")
    if report["passed"] != report["n"]:
        raise ValueError("Reference self-test failed; inspect output")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--provider", choices=["env", "openrouter"], default="env")
    parser.add_argument("--model", help="Explicit override; mandatory with --provider openrouter")
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/etl"))
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Generate reproducible public tasks and private references")
    generate.add_argument("--seed", type=int, default=20261010)
    generate.add_argument("--tasks-per-family", type=int, default=4)
    generate.set_defaults(func=command_generate)
    run = commands.add_parser("run", help="Run a fixed agent configuration, then grade externally")
    run.add_argument("--config", type=Path, default=Path("configs/etl/baseline.json"))
    run.add_argument("--split", choices=["train", "dev", "test"], default="train")
    run.add_argument("--limit", type=int)
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(func=command_run)
    opt = commands.add_parser("optimize", help="Search on train, select on dev; test is never loaded")
    opt.add_argument("--config", type=Path, default=Path("configs/etl/baseline.json"))
    opt.add_argument("--rounds", type=int, default=1)
    opt.add_argument("--proposals", type=int, default=2)
    opt.add_argument("--limit", type=int)
    opt.add_argument("--dev-limit", type=int)
    opt.add_argument("--output", type=Path, required=True)
    opt.set_defaults(func=command_optimize)
    compare = commands.add_parser("compare", help="Compare fixed configurations on exactly the same tasks")
    compare.add_argument("runs", type=Path, nargs="+")
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(func=command_compare)
    check = commands.add_parser("check-sql", help="Execute your saved pipeline and grade it without an LLM")
    check.add_argument("--task", required=True)
    check.add_argument("--split", choices=["train", "dev", "test"], default="train")
    check.add_argument("--sql", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.set_defaults(func=command_check_sql)
    verify = commands.add_parser("verify-data", help="Instructor reference self-test; not an agent run")
    verify.add_argument("--output", type=Path, default=Path("artifacts/etl/reference-check.json"))
    verify.set_defaults(func=command_verify)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
