"""Record real Jev evaluations of an authentic tool-calling run. No secrets in output."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-agent", action="store_true", help="Make a new real agent run before evaluation")
    args = parser.parse_args()
    load_dotenv(HERE.parents[1] / ".env")
    key = os.environ["OPENROUTER_API_KEY"]
    if args.new_agent:
        from support_agent import run_agent
        run = run_agent(key)
        (HERE / "data/support_agent_run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf8")
    from runtime import (Jev, trajectory_views, SEMANTIC_ANSWERS, SEMANTIC_CASE_TEXT,
                         SEMANTIC_QUESTIONS, semantic_state, TRAJECTORY, brief_trajectory, digest, load_agent_run, VERIFICATION)
    jev = Jev(key)
    scopes = []
    for scope, title, state in trajectory_views():
        result = jev.classify(state)
        scopes.append({"scope": scope, "scope_title": title, "state": state,
                       "input_sha256": digest(state), "jev": result})
        print(scope, result["prediction"], result["probabilities"], flush=True)
    semantic = []
    for case, answer in SEMANTIC_ANSWERS.items():
        state = semantic_state(answer)
        result = jev.decide(state, SEMANTIC_QUESTIONS)
        semantic.append({"case": case, "case_title": SEMANTIC_CASE_TEXT[case], "state": state,
            "input_sha256": digest(state), "result": result,
            "expected_risk": {"vague": "evasiveness", "overpromise": "overpromise", "actionable": "actionable"}[case],
            "expected_score": {"vague": 1, "overpromise": 0, "actionable": 3}[case]})
        print(case, result["answers"], flush=True)
    run = load_agent_run()
    output = {"recorded_utc": datetime.now(timezone.utc).isoformat(),
        "source": "real API calls on a recorded OpenRouter tool-calling agent run against local simulated tools",
        "agent_run_file": "data/support_agent_run.json", "agent_run_sha256": digest(run),
        "agent_model": run["api_responses"][-1]["model"], "agent_run_id": "support-" + run["events_sha256"][:12],
        "verification_question": VERIFICATION, "scope_rows": scopes, "semantic_rows": semantic, "questions": SEMANTIC_QUESTIONS,
        "full_trace": TRAJECTORY, "brief_trace": brief_trajectory(TRAJECTORY),
        "compression": "Every exact tool result extracted from recorded events, with source event/call references and explicit omitted IDs.",
        "evidence_policy": "Final answer and system policy excluded from evaluator observations; only actual tool results prove executed operations.",
        "author_expectations": "Semantic alternatives vague/overpromise are authored contrast examples; actionable is the real agent final answer. Labels are illustrative, not an independent test benchmark."}
    target = HERE / "data/trajectory_smoke.json"
    if target.exists():
        old = json.loads(target.read_text(encoding="utf8"))
        if "authored teaching trajectory" in old.get("source", ""):
            history = HERE / "data/trajectory_smoke_authored_history.json"
            if not history.exists():
                shutil.copyfile(target, history)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
