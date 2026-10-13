"""Rebuild the student notebook, ZIP and sendable HTML from this folder.

Maintainer command: python homework/build.py. The committed support assets and
editable runtime are canonical student sources; no workshop checkout or private
experiment outputs are needed. Existing student configs are preserved.
"""
from pathlib import Path
import hashlib
import json
import zipfile

from notebook_template import make_notebook
from standalone import build_standalone, inline_trajectory

HERE = Path(__file__).resolve().parent
SUPPORT = HERE / "_support"
FILES = [
    "notebook_support.py", "coding_transfer.py", "task_adapter.py",
    "evidence/workshop_evidence.json", "evidence/etl-benchmark.json",
    "evidence/manual-currency-v1/protocol.json",
    "evidence/manual-currency-v1/teaching-evidence.json",
    "evidence/manual-baseline-reproduction-v1/protocol.json",
    "evidence/manual-baseline-currency-policy-v1/protocol.json",
    "evidence/baseline-currency-comparison.json", "trajectory.html",
    "etl_diagnostics.ipynb", "example_pipeline.sql", "experiment.py",
]
RUNTIME_FILES = [
    "__init__.py", "etl_agent.py", "etl_env.py", "etl_data.py", "etl_cli.py",
    "etl_optimizer.py", "model.py", "settings.py",
]


def build():
    # The viewer is committed inline and needs no companion scripts.
    viewer = SUPPORT / "trajectory.html"
    viewer.write_text(inline_trajectory(viewer.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
    notebook = make_notebook()
    for index, item in enumerate(notebook["cells"]):
        item["id"] = f"homework-{index:02d}"
    (HERE / "etl_homework.ipynb").write_text(
        json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    allowed = [HERE / name for name in (
        "README.md", "index.html", "requirements.txt", "etl_homework.ipynb",
        "configs/control.json", "configs/candidate.json",
    )]
    allowed += [SUPPORT / name for name in FILES]
    allowed += [HERE / "runtime/harness_lab" / name for name in RUNTIME_FILES]
    # Strict allowlist: never package credentials, student results or local work.
    manifest = {
        path.relative_to(HERE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(allowed)
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    allowed += [HERE / "manifest.json"]
    with zipfile.ZipFile(HERE / "homework.zip", "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for path in sorted(allowed):
            entry = zipfile.ZipInfo("homework/" + path.relative_to(HERE).as_posix(),
                                    date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes(), compresslevel=9)
    standalone = build_standalone(HERE)
    print(f"homework: {len(notebook['cells'])} cells; configs preserved; "
          f"{standalone.name}: {standalone.stat().st_size:,} bytes")


if __name__ == "__main__":
    build()
