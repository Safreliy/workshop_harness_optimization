"""Notebook setup must distinguish a partial extraction from a cached import."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def homework(tmp_path):
    folder = tmp_path / "homework (5)" / "homework"
    folder.mkdir(parents=True)
    shutil.copytree(ROOT / "homework/_support", folder / "_support")
    shutil.copytree(ROOT / "homework/runtime", folder / "runtime")
    shutil.copytree(ROOT / "homework/configs", folder / "configs")
    return folder


def run_script(homework, source):
    setup = f"""
import sys
from pathlib import Path
HOMEWORK = Path({str(homework)!r})
sys.path.insert(0, str(HOMEWORK / '_support'))
from experiment import prepare
"""
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", setup + source],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_partial_extraction_is_reported_before_install(homework):
    shutil.rmtree(homework / "runtime")
    shutil.rmtree(homework / "configs")
    run_script(homework, """
from unittest.mock import patch
with patch('experiment.subprocess.run') as pip:
    try:
        prepare(HOMEWORK, install=True)
    except RuntimeError as exc:
        message = str(exc)
        assert 'распакована не полностью' in message
        assert 'runtime/harness_lab/__init__.py' in message
        assert 'configs/control.json' in message
        assert 'Перезапустите ядро' not in message
    else:
        raise AssertionError('Partial extraction accepted')
    pip.assert_not_called()
""")


def test_retry_after_restoring_files_invalidates_missing_path_cache(homework):
    runtime = homework / "runtime"
    saved = homework / "saved-runtime"
    shutil.copytree(runtime, saved)
    shutil.rmtree(runtime)
    run_script(homework, """
import importlib.util, shutil
runtime = HOMEWORK / 'runtime'
sys.path.insert(0, str(runtime))
importlib.util.find_spec('harness_lab')
assert sys.path_importer_cache.get(str(runtime)) is None
try:
    prepare(HOMEWORK)
except RuntimeError as exc:
    assert 'распакована не полностью' in str(exc)
else:
    raise AssertionError('Partial extraction accepted')
shutil.copytree(HOMEWORK / 'saved-runtime', runtime)
prepare(HOMEWORK)
import harness_lab.etl_agent
assert Path(harness_lab.etl_agent.__file__).resolve().is_relative_to(runtime)
""")


def test_unimported_repository_install_does_not_override_bundle(homework):
    run_script(homework, f"""
sys.path.append({str(ROOT / 'src')!r})
prepare(HOMEWORK)
import harness_lab.etl_agent
assert Path(harness_lab.etl_agent.__file__).resolve().is_relative_to(HOMEWORK / 'runtime')
prepare(HOMEWORK)
""")


def test_already_imported_other_runtime_still_requires_restart(homework):
    run_script(homework, f"""
sys.path.insert(0, {str(ROOT / 'src')!r})
import harness_lab
try:
    prepare(HOMEWORK)
except RuntimeError as exc:
    assert 'Перезапустите ядро' in str(exc)
else:
    raise AssertionError('Cached foreign runtime accepted')
""")


def test_runtime_edit_requires_restart(homework):
    run_script(homework, """
prepare(HOMEWORK)
target = HOMEWORK / 'runtime/harness_lab/etl_agent.py'
target.write_text(target.read_text(encoding='utf-8') + '\\n# student change\\n', encoding='utf-8')
try:
    prepare(HOMEWORK)
except RuntimeError as exc:
    assert 'Код изменён' in str(exc)
else:
    raise AssertionError('Changed runtime accepted without restart')
""")
