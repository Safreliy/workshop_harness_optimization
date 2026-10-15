"""Build a portable notebook and package real, aligned prediction snapshots."""
import hashlib
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def build_data():
    paths = {
        "Jev": lambda split: ROOT / f"results/jev_{split}.jsonl",
        "mmBERT head": lambda split: ROOT / f"experiment/artifacts/frozen/predictions_{split}.jsonl",
        "mmBERT LoRA": lambda split: ROOT / f"experiment/artifacts/lora/predictions_{split}.jsonl",
        "TF-IDF": lambda split: ROOT / f"results/lexical/predictions_{split}.jsonl",
    }
    data = {"rubric": json.loads((ROOT / "results/jev_test.metrics.json").read_text(encoding="utf8"))["rubric"],
            "source_note": "Recorded real inference on synthetic, deterministic NLI data; not the SQL teaching fixture.",
            "models": {}, "cases": {}, "provenance": []}
    for split in ("calibration", "test", "challenge"):
        source = ROOT / (f"experiment/data/{split}.jsonl" if split != "challenge" else "results/challenge.jsonl")
        cases = read(source)
        data["cases"][split] = cases
        ids = {r["id"] for r in cases}
        data["provenance"].append({"path": source.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        for name, path_for in paths.items():
            path = path_for(split)
            predictions = read(path)
            if {p["id"] for p in predictions} != ids:
                raise ValueError(f"Different IDs: {name} {split}")
            by_id = {p["id"]: p for p in predictions}
            compact = []
            for row in cases:
                p = by_id[row["id"]]
                if p["label"] != row["label"]:
                    raise ValueError("Labels changed")
                compact.append({k: p[k] for k in ("id", "label", "prediction", "probabilities", "error", "latency_ms", "usage") if k in p}
                               | {"split": split})
            data["models"].setdefault(name, {})[split] = compact
            data["provenance"].append({"model": name, "split": split, "path": path.relative_to(ROOT).as_posix(),
                                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    data["probability_calibration"] = {
        "Jev": {"method": "as returned by API; no local probability fit"},
        "TF-IDF": {"method": "LogisticRegression.predict_proba; no separate probability fit"},
    }
    for name, mode in (("mmBERT head", "frozen"), ("mmBERT LoRA", "lora")):
        metrics_path = ROOT / f"experiment/artifacts/{mode}/metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf8"))
        data["probability_calibration"][name] = {
            "method": "temperature scaling fitted before these predictions",
            "temperature": metrics["temperature"], "fit_split": "calibration",
            "objective": "minimum NLL; grid 0.25..5.0, 191 values",
            "source": metrics_path.relative_to(ROOT).as_posix(),
            "source_sha256": hashlib.sha256(metrics_path.read_bytes()).hexdigest(),
        }
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data/benchmark.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf8")


cells = []


def md(text):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)})


def code(text):
    cells.append({"cell_type": "code", "metadata": {}, "source": text.splitlines(True), "execution_count": None, "outputs": []})


from notebook_cells import populate
populate(md, code)


def package():
    allowed = ["practice.ipynb", "runtime.py", "support_agent.py", "trace_views.py", "local_models.py", "local_setup.md", "requirements.txt",
               "data/benchmark.json", "data/live_smoke.json", "data/semantic_smoke.json",
               "data/local_smoke.json", "data/pretrained_nli_smoke.json", "data/trajectory_smoke.json", "data/support_agent_run.json",
               "data/evaluation_smoke.json"]
    with zipfile.ZipFile(HERE / "practice.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in allowed:
            path = HERE / name
            if not path.is_file():
                raise FileNotFoundError(path)
            archive.write(path, "practice/" + name)
    print(f"Notebook: {len(cells)} cells; archive: {HERE / 'practice.zip'}")


def main():
    build_data()
    notebook = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                 "language_info": {"name": "python", "version": "3.11"}}, "nbformat": 4, "nbformat_minor": 5}
    for i, cell in enumerate(cells):
        cell["id"] = f"practice-{i:02d}"
    (HERE / "practice.ipynb").write_text(json.dumps(notebook, indent=1, ensure_ascii=False), encoding="utf8")
    package()


if __name__ == "__main__":
    main()
