"""The distributable viewer must not require standalone Windows script files."""
import importlib.util
from pathlib import Path
import zipfile
import hashlib
import json
import base64
import io
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("homework_standalone", ROOT / "homework/standalone.py")
standalone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(standalone)


def student_archive():
    html = (ROOT / "homework/homework.html").read_text(encoding="utf-8")
    match = re.search(r'<script id="embedded-homework" type="application/json">(.*?)</script>',
                      html, flags=re.DOTALL)
    assert match, "Sendable HTML must embed the complete homework archive"
    assets = json.loads(match.group(1))
    assert assets["homework.zip"]["gzip"] is False
    return zipfile.ZipFile(io.BytesIO(base64.b64decode(assets["homework.zip"]["data"])))


def test_inline_trajectory_reads_sources_once_and_bundles_without_companions(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    (source / "trajectory.css").write_text("body{color:teal}", encoding="utf-8")
    (source / "trajectory-data.js").write_text('window.DATA="</SCRIPT>";', encoding="utf-8")
    (source / "trajectory.js").write_text("history.replaceState(null,'','?case='+selected);", encoding="utf-8")
    html = '<link rel="stylesheet" href="trajectory.css"><script src="trajectory-data.js"></script><script src="trajectory.js"></script>'
    viewer = standalone.inline_trajectory(html, source)
    assert 'src="' not in viewer and 'href="' not in viewer
    assert "<style>body{color:teal}</style>" in viewer
    assert '<\\/script>' in viewer
    assert "history.replaceState" in viewer
    # No source files are needed when preparing the inline HTML for a blob tab.
    blob_viewer = standalone.inline_trajectory(viewer, blob=True)
    assert "history.replaceState" not in blob_viewer
    assert standalone.inline_trajectory(viewer) == viewer


def test_student_archive_contains_selfcontained_viewer_and_complete_runtime():
    with student_archive() as archive:
        names = archive.namelist()
        assert not any(Path(name).suffix.lower() in {".js", ".css"} for name in names)
        manifest = json.loads(archive.read("homework/manifest.json"))
        assert set(names) == {"homework/" + name for name in manifest} | {"homework/manifest.json"}
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read("homework/" + name)).hexdigest() == digest
        for required in ("etl_homework.ipynb", "configs/control.json", "configs/candidate.json",
                         "runtime/harness_lab/etl_agent.py", "runtime/harness_lab/etl_optimizer.py",
                         "_support/experiment.py", "_support/evidence/workshop_evidence.json"):
            assert required in manifest
        viewer = archive.read("homework/_support/trajectory.html").decode("utf-8")
        assert "window.WORKSHOP_TRAJECTORIES" in viewer
        assert 'src="trajectory' not in viewer
        assert 'href="trajectory.css"' not in viewer


def test_builder_rebuilds_from_student_files_without_workshop_sources(tmp_path):
    with student_archive() as archive:
        archive.extractall(tmp_path)
    folder = tmp_path / "homework"
    for name in ("build.py", "notebook_template.py", "standalone.py"):
        shutil.copyfile(ROOT / "homework" / name, folder / name)
    config = folder / "configs/candidate.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    value["prompt_patch"] = "Student's saved hypothesis"
    config.write_text(json.dumps(value), encoding="utf-8")
    saved_config = config.read_bytes()
    generated = []
    for _ in range(2):
        result = subprocess.run([sys.executable, "-X", "utf8", str(folder / "build.py")],
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        assert config.read_bytes() == saved_config
        generated.append([(folder / name).read_bytes()
                          for name in ("homework.zip", "manifest.json", "homework.html")])
    assert generated[0] == generated[1], "Rebuilds must be deterministic"
