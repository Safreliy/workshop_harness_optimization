from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .cases import load_cases
from .config import HarnessConfig, SearchConfig
from .db import PostgresSandbox
from .demo import serve_demo, write_replay_bundle
from .model import ModelClient
from .optimizer import MetaHarnessOptimizer
from .runner import RunnerResult, run_benchmark
from .settings import Settings


console = Console()


def _kinds(value: str) -> set[str]:
    parsed = {part.strip() for part in value.split(",") if part.strip()}
    unknown = parsed - {"report", "sql"}
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown benchmark kinds: {sorted(unknown)}")
    return parsed


def _show_result(result: RunnerResult) -> None:
    table = Table(title=f"Run: {result.summary['config']}")
    table.add_column("Case")
    table.add_column("Kind")
    table.add_column("Output", justify="right")
    table.add_column("Trajectory", justify="right")
    table.add_column("Combined", justify="right")
    table.add_column("Pass")
    table.add_column("LLM calls", justify="right")
    for record in result.records:
        table.add_row(
            record.case_id,
            record.kind,
            f"{record.output_score:.3f}",
            f"{record.trajectory_score:.3f}",
            f"{record.score:.3f}",
            "yes" if record.passed else "no",
            str(record.llm_calls),
        )
    console.print(table)
    console.print_json(json.dumps(result.summary, ensure_ascii=False))
    console.print(f"Artifacts: [link=file://{result.run_dir.resolve()}]{result.run_dir.resolve()}[/link]")


def command_doctor(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    table = Table(title="Harness lab doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    missing = settings.validate_model()
    table.add_row(
        "model config",
        "ok" if not missing else "fail",
        f"model={settings.model_name}" if not missing else f"missing={','.join(missing)}",
    )
    model_ok = False
    if not missing:
        try:
            client = ModelClient(settings)
            models = client.list_models()
            advertised = settings.model_name in models
            model_ok = True
            table.add_row(
                "model API",
                "ok",
                f"/models reachable; configured model advertised={advertised}",
            )
        except Exception as exc:
            table.add_row("model API", "fail", f"{type(exc).__name__}: {exc}")
    db_ok, detail = PostgresSandbox(settings.pg_dsn).ping()
    table.add_row("PostgreSQL", "ok" if db_ok else "fail", detail[:200])
    try:
        train_count = len(load_cases(split="train"))
        eval_count = len(load_cases(split="eval"))
        data_ok = True
        data_detail = f"train={train_count}, eval={eval_count}"
    except Exception as exc:
        data_ok = False
        data_detail = str(exc)
    table.add_row("benchmark data", "ok" if data_ok else "fail", data_detail)
    console.print(table)
    return 0 if not missing and model_ok and db_ok and data_ok else 1


def command_db_up(_: argparse.Namespace) -> int:
    completed = subprocess.run(
        ["docker", "compose", "up", "-d", "--wait"], check=False
    )
    return completed.returncode


def command_db_reset(_: argparse.Namespace) -> int:
    completed = subprocess.run(
        ["docker", "compose", "down", "--volumes", "--remove-orphans"], check=False
    )
    if completed.returncode:
        return completed.returncode
    return subprocess.run(["docker", "compose", "up", "-d", "--wait"], check=False).returncode


def command_benchmark(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    config = HarnessConfig.from_yaml(args.config)
    cases = load_cases(split=args.split, kinds=args.kinds)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        console.print("No cases selected.")
        return 2
    result = run_benchmark(
        settings=settings,
        config=config,
        cases=cases,
        artifact_root=args.artifact_root,
        run_label=args.split,
    )
    _show_result(result)
    return 0


def command_train(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    search = SearchConfig.from_yaml(args.search_config)
    cases = load_cases(split="train", kinds=args.kinds)
    if args.limit:
        cases = cases[: args.limit]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path(args.output or f"artifacts/training/{timestamp}")
    optimizer = MetaHarnessOptimizer(
        settings=settings,
        search=search,
        cases=cases,
        output_dir=output,
        max_trials=args.budget,
        resume=args.resume,
    )
    result = optimizer.optimize()
    table = Table(title="Model-generated meta-harness search")
    table.add_column("#", justify="right")
    table.add_column("Parent", justify="right")
    table.add_column("Operator")
    table.add_column("Score", justify="right")
    table.add_column("Calls", justify="right")
    table.add_column("Objective", justify="right")
    table.add_column("In beam")
    for trial in result.trials:
        table.add_row(
            str(trial.number),
            "-" if trial.parent_number is None else str(trial.parent_number),
            trial.operator,
            f"{trial.score:.3f}",
            f"{trial.avg_llm_calls:.2f}",
            f"{trial.objective:.3f}",
            "yes" if trial.selected else "no",
        )
    console.print(table)
    console.print(f"Best config: {output.resolve() / 'best_config.yaml'}")
    console.print("Run `harness-lab eval` to compare the baseline and selected config on eval.")
    return 0


def command_eval(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    baseline = HarnessConfig.from_yaml(args.baseline)
    candidate = HarnessConfig.from_yaml(args.candidate)
    cases = load_cases(split="eval", kinds=args.kinds)
    if args.limit:
        cases = cases[: args.limit]
    root = Path(args.artifact_root)
    baseline_result = run_benchmark(
        settings=settings,
        config=baseline,
        cases=cases,
        artifact_root=root,
        run_label="eval-baseline",
    )
    candidate_result = run_benchmark(
        settings=settings,
        config=candidate,
        cases=cases,
        artifact_root=root,
        run_label="eval-candidate",
    )
    by_base = {record.case_id: record for record in baseline_result.records}
    by_candidate = {record.case_id: record for record in candidate_result.records}
    table = Table(title="Eval comparison")
    table.add_column("Case")
    table.add_column("Baseline", justify="right")
    table.add_column("Candidate", justify="right")
    table.add_column("Delta", justify="right")
    for case in cases:
        before = by_base[case.id].score
        after = by_candidate[case.id].score
        table.add_row(case.id, f"{before:.3f}", f"{after:.3f}", f"{after-before:+.3f}")
    before = float(baseline_result.summary["score"])
    after = float(candidate_result.summary["score"])
    comparison = {
        "baseline": baseline_result.summary,
        "candidate": candidate_result.summary,
        "delta": round(after - before, 4),
        "baseline_run": str(baseline_result.run_dir),
        "candidate_run": str(candidate_result.run_dir),
    }
    root.mkdir(parents=True, exist_ok=True)
    output = root / "latest_comparison.json"
    output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(table)
    console.print(f"Overall: {before:.3f} -> {after:.3f} ({after-before:+.3f})")
    console.print(f"Comparison: {output.resolve()}")
    console.print("Eval is reusable development feedback; it is not an untouched holdout.")
    return 0


def command_demo_export(args: argparse.Namespace) -> int:
    target = write_replay_bundle(args)
    console.print(f"Demo replay bundle: {target.resolve()}")
    return 0


def command_demo(args: argparse.Namespace) -> int:
    return serve_demo(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-lab",
        description="Reproducible PostgreSQL agent harness optimization workshop",
    )
    parser.add_argument("--env", default=".env", help="dotenv file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check model, database, and dataset")
    doctor.set_defaults(func=command_doctor)

    db_up = subparsers.add_parser("db-up", help="start and initialize PostgreSQL")
    db_up.set_defaults(func=command_db_up)
    db_reset = subparsers.add_parser(
        "db-reset", help="delete the benchmark volume and rebuild deterministic data"
    )
    db_reset.set_defaults(func=command_db_reset)

    benchmark = subparsers.add_parser("benchmark", help="evaluate one harness config")
    benchmark.add_argument("--config", default="configs/baseline.yaml")
    benchmark.add_argument("--split", choices=["train", "eval", "all"], default="train")
    benchmark.add_argument("--kinds", type=_kinds, default={"report", "sql"})
    benchmark.add_argument("--limit", type=int)
    benchmark.add_argument("--artifact-root", default="artifacts/runs")
    benchmark.set_defaults(func=command_benchmark)

    train = subparsers.add_parser("train", help="optimize harness on train only")
    train.add_argument("--search-config", default="configs/search.yaml")
    train.add_argument("--kinds", type=_kinds, default={"report", "sql"})
    train.add_argument("--limit", type=int, help="quick smoke mode; use all cases for workshop")
    train.add_argument("--budget", type=int, help="maximum evaluated harness configs")
    train.add_argument("--output")
    train.add_argument(
        "--resume",
        action="store_true",
        help="continue a search from optimization_trace.json in --output",
    )
    train.set_defaults(func=command_train)

    evaluate = subparsers.add_parser(
        "eval", help="compare baseline and candidate on the reusable eval split"
    )
    evaluate.add_argument("--baseline", default="configs/baseline.yaml")
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--kinds", type=_kinds, default={"report", "sql"})
    evaluate.add_argument("--limit", type=int)
    evaluate.add_argument("--artifact-root", default="artifacts/eval")
    evaluate.set_defaults(func=command_eval)

    demo_export = subparsers.add_parser(
        "demo-export", help="build the static replay bundle from saved eval artifacts"
    )
    demo_export.add_argument("--benchmark-dir", default="benchmarks")
    demo_export.add_argument(
        "--comparison", default="artifacts/eval/meta_v4/latest_comparison.json"
    )
    demo_export.add_argument("--baseline", default="configs/baseline.yaml")
    demo_export.add_argument(
        "--candidate", default="artifacts/training/meta_v4/best_config.yaml"
    )
    demo_export.add_argument("--output", default="presentation/demo-data.json")
    demo_export.set_defaults(func=command_demo_export)

    demo = subparsers.add_parser(
        "demo", help="serve the interactive comparison with live agent runs"
    )
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--benchmark-dir", default="benchmarks")
    demo.add_argument("--baseline", default="configs/baseline.yaml")
    demo.add_argument(
        "--candidate", default="artifacts/training/meta_v4/best_config.yaml"
    )
    demo.add_argument("--static-dir", default="presentation/dist")
    demo.add_argument("--artifact-root", default="artifacts/demo")
    demo.set_defaults(func=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        console.print("Interrupted.")
        return 130
    except Exception as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
