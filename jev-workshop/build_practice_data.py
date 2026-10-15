"""Export aligned recorded predictions for the static workshop diagrams."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parent

def main():
    benchmark = json.loads((ROOT / "practice/data/benchmark.json").read_text(encoding="utf-8"))
    names = {"Jev": "Jev", "mmBERT head": "mmBERT · frozen head", "mmBERT LoRA": "mmBERT · LoRA"}
    output = {}
    for source, display in names.items():
        output[display] = {}
        for split, rows in benchmark["models"][source].items():
            cases = {r["id"]: r for r in benchmark["cases"][split]}
            output[display][split] = [
                {k: cases[r["id"]][k] for k in ("id", "request", "evidence", "claim", "label")}
                | {k: r[k] for k in ("probabilities", "prediction")}
                for r in rows if not r.get("error")
            ]
    payload = json.dumps(output, ensure_ascii=False).replace("</", "<\\/")
    live = json.loads((ROOT / "practice/data/live_smoke.json").read_text(encoding="utf-8"))
    (ROOT / "practice-data.js").write_text("window.PRACTICE_DATA = " + payload + ";\nwindow.PRACTICE_LIVE = " + json.dumps(live, ensure_ascii=False).replace("</", "<\\/") + ";\n", encoding="utf-8")

def export_semantic():
    path = ROOT / "practice/data/semantic_smoke.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        with (ROOT / "practice-data.js").open("a", encoding="utf-8") as out:
            out.write("window.PRACTICE_SEMANTIC = " + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";\n")

def export_trajectory():
    path = ROOT / "practice/data/trajectory_smoke.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        with (ROOT / "practice-data.js").open("a", encoding="utf-8") as out:
            out.write("window.PRACTICE_TRAJECTORY = " + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";\n")

def export_agent_run():
    path = ROOT / "practice/data/support_agent_run.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        with (ROOT / "practice-data.js").open("a", encoding="utf-8") as out:
            out.write("window.PRACTICE_AGENT_RUN = " + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";\n")

if __name__ == "__main__":
    main()
    export_semantic()
    export_trajectory()
    export_agent_run()
