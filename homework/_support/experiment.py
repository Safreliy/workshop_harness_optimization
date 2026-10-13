"""Notebook interface to the bundled ETL runtime. Credentials stay in memory."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

_LOADED_RUNTIME = None
_RUNTIME_FILES = (
    "__init__.py", "etl_agent.py", "etl_env.py", "etl_data.py",
    "etl_cli.py", "etl_optimizer.py", "model.py", "settings.py",
)


def prepare(homework, install=False):
    global _LOADED_RUNTIME
    root = Path(homework).resolve()
    runtime = root / "runtime"
    required = [runtime / "harness_lab" / name for name in _RUNTIME_FILES]
    required += [root / "configs" / f"{name}.json" for name in ("control", "candidate")]
    missing = [path.relative_to(root).as_posix() for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Папка homework распакована не полностью. Отсутствуют: "
            + ", ".join(missing)
            + ". Распакуйте архив homework.zip целиком, включая runtime и configs, "
              "и откройте ноутбук из полученной папки homework."
        )
    if install:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(root / "requirements.txt")], check=True)
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    # A previous attempt may have cached this path while it was still absent.
    importlib.invalidate_caches()
    # Never silently run the repository's editable installation instead.
    spec = importlib.util.find_spec("harness_lab")
    if spec is None or not Path(spec.origin).resolve().is_relative_to(runtime):
        raise RuntimeError("Перезапустите ядро: harness_lab уже импортирован из другой папки.")
    hashes = {p.name: digest(p) for p in sorted((runtime / "harness_lab").glob("*.py"))}
    if _LOADED_RUNTIME is not None and hashes != _LOADED_RUNTIME:
        raise RuntimeError("Код изменён: перезапустите ядро, чтобы загрузить новую реализацию.")
    _LOADED_RUNTIME = hashes
    return root


def save_config(path, payload):
    try:
        from harness_lab.etl_agent import ETLHarnessConfig
    except ModuleNotFoundError as exc:
        raise RuntimeError("В первой ячейке включите INSTALL_DEPS=True и выполните её, затем повторите сохранение.") from exc
    ETLHarnessConfig.model_validate(payload).save(path)


def connect(*, base_url, model, api_key, proxy="", ca_file=""):
    from harness_lab.model import ModelClient
    from harness_lab.settings import Settings
    if not model.strip() or not api_key.strip():
        raise ValueError("Укажите модель и API-ключ.")
    return ModelClient(Settings(api_key=api_key, base_url=base_url.rstrip("/"),
        model_name=model, pg_dsn="", model_proxy_url=proxy, model_ca_file=ca_file),
        timeout=90, max_attempts=2)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Experiment:
    """One fixed model, dataset and pair of configs; stages never overwrite runs."""
    def __init__(self, homework, name, client=None, seed=2026101301):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", name):
            raise ValueError("EXPERIMENT: используйте латиницу, цифры, _ или -.")
        self.homework = Path(homework).resolve()
        self.root = self.homework / "results" / name
        self.client = client
        self.seed = seed
        self.dataset = self.homework / "results" / f"dataset-{seed}"

    def _identity(self):
        settings = self.client.settings
        return {"model": self.client.model_name,
                "endpoint_sha256": hashlib.sha256(settings.base_url.encode()).hexdigest(),
                "seed": self.seed,
                "configs": {n: digest(self.homework / "configs" / f"{n}.json") for n in ("control", "candidate")},
                "runtime": {p.name: digest(p) for p in sorted((self.homework / "runtime/harness_lab").glob("*.py"))}}

    def _initialize(self):
        prepare(self.homework)
        from harness_lab.etl_data import generate_dataset
        if self.client is None:
            raise ValueError("Включите CONNECT и выполните ячейку подключения.")
        identity = self._identity()
        protocol = self.root / "protocol.json"
        if protocol.exists() and read(protocol)["identity"] != identity:
            raise ValueError("Модель, конфиг или код изменились. Задайте новое имя EXPERIMENT и повторите оба варианта.")
        manifest = self.dataset / "manifest.json"
        if not manifest.exists():
            if self.dataset.exists() and any(self.dataset.iterdir()):
                raise ValueError("Неполный dataset: выберите другой DATA_SEED.")
            generate_dataset(self.dataset, seed=self.seed, tasks_per_family=4)
        # Lock every input and private grading file, not only the manifest.
        dataset_identity = {p.relative_to(self.dataset).as_posix(): digest(p)
                            for p in sorted(self.dataset.rglob("*")) if p.is_file()}
        if protocol.exists():
            if read(protocol)["dataset"] != dataset_identity:
                raise ValueError("Набор данных изменён. Верните исходный набор или начните новый эксперимент.")
            from harness_lab.etl_agent import ETLHarnessConfig
            for name in ("control", "candidate"):
                current = ETLHarnessConfig.model_validate(read(self.homework / "configs" / f"{name}.json")).model_dump()
                if read(self.root / "configs" / f"{name}.json") != current:
                    raise ValueError("Снимок конфига в результатах изменён; начните новый эксперимент.")
        else:
            from harness_lab.etl_agent import ETLHarnessConfig
            for name in ("control", "candidate"):
                payload = read(self.homework / "configs" / f"{name}.json")
                write(self.root / "configs" / f"{name}.json", ETLHarnessConfig.model_validate(payload).model_dump())
            write(protocol, {"identity": identity, "dataset": dataset_identity,
                             "selection_rule": "dev: pass_rate, mean_score, fewer calls, fewer total tokens; tie -> control"})

    def _run_one(self, name, split, limit, directory, config_path):
        from harness_lab.etl_agent import ETLHarnessConfig, run_agent
        from harness_lab.etl_env import ETLEnvironment
        from harness_lab.etl_cli import task_paths, fingerprint, summarize, export_pipeline
        if directory.exists():
            raise ValueError(f"Запуск {directory.name} уже существует. Для повтора задайте новый EXPERIMENT.")
        directory.mkdir(parents=True)
        config = ETLHarnessConfig.model_validate(read(config_path))
        records = []
        paths = task_paths(self.dataset, split, limit)
        for i, task in enumerate(paths, 1):
            before = fingerprint(task)
            with ETLEnvironment(task) as environment:
                record = run_agent(task, config, self.client, environment=environment)
                record["grade"] = environment.grade(self.dataset / "private" / f"{task.parent.name}.json")
            if fingerprint(task) != before:
                raise ValueError("Источники изменились во время запуска.")
            record.update(task_sha256=before, split=split)
            write(directory / "records" / f"{task.parent.name}.json", record)
            export_pipeline(directory, task.parent.name, record)
            records.append(record)
            print(f"{name} {split}: {i}/{len(paths)} · {task.parent.name} · pass={record['grade']['passed']} · calls={record['llm_calls']}", flush=True)
        report = {"config": config.model_dump(), "model": self.client.model_name,
                  "provider": "notebook-" + self._identity()["endpoint_sha256"], "split": split,
                  "task_ids": [p.parent.name for p in paths], "summary": summarize(records)}
        write(directory / "summary.json", report)

    def execute(self, action, selected="auto"):
        if action not in {"smoke", "train", "dev", "select", "test"}:
            raise ValueError("ACTION: smoke, train, dev, select или test.")
        self._initialize()
        if (self.root / "selection.json").exists() and action != "test":
            raise ValueError("Выбор уже зафиксирован; для новой итерации создайте новый EXPERIMENT.")
        if action == "select":
            return self._select(selected)
        if action == "dev":
            self._require("train", 24)
        if action == "test":
            selection = read(self.root / "selection.json") if (self.root / "selection.json").exists() else None
            if selection is None:
                raise ValueError("Сначала выполните train → dev → select.")
            if selection["identity"] != read(self.root / "protocol.json")["identity"]:
                raise ValueError("Условия выбора изменены.")
            for name, checksum in selection["frozen"].items():
                if digest(self.root / "configs" / f"{name}-frozen.json") != checksum:
                    raise ValueError("Зафиксированный конфиг изменён.")
            arms = [("control", self.root / "configs/control-frozen.json"),
                    ("selected", self.root / "configs/selected-frozen.json")]
        else:
            arms = [(n, self.root / "configs" / f"{n}.json") for n in ("control", "candidate")]
        # Reject the entire pair before issuing requests if either arm exists.
        if any((self.root / f"{name}-{action}").exists() for name, _ in arms):
            raise ValueError("Эта стадия уже запускалась. Для повторной серии задайте новый EXPERIMENT.")
        split = "train" if action == "smoke" else action
        for name, config in arms:
            self._run_one(name, split, 3 if action == "smoke" else None, self.root / f"{name}-{action}", config)
        from argparse import Namespace
        from harness_lab.etl_cli import command_compare
        command_compare(Namespace(runs=[self.root / f"{name}-{action}" for name, _ in arms],
                                  output=self.root / f"{action}-comparison.md"))
        return self.metrics()

    def _require(self, action, expected):
        from harness_lab.etl_cli import _comparison_run, fingerprint
        runs = [_comparison_run(self.root / f"{n}-{action}") for n in ("control", "candidate")]
        identity = read(self.root / "protocol.json")["identity"]
        for name, (_, meta, records) in zip(("control", "candidate"), runs):
            expected_config = read(self.root / "configs" / f"{name}.json")
            if len(records) != expected or meta["model"] != identity["model"] or meta["provider"] != "notebook-" + identity["endpoint_sha256"] or meta["config"] != expected_config:
                raise ValueError("Неполные результаты или изменённые условия.")
            for record in records:
                task = self.dataset / "public" / action / record["task_id"] / "task.json"
                if record["config"] != expected_config or record["task_sha256"] != fingerprint(task):
                    raise ValueError("Результат не соответствует текущему конфигу или данным.")
        if {(r["task_id"], r["task_sha256"]) for r in runs[0][2]} != {(r["task_id"], r["task_sha256"]) for r in runs[1][2]}:
            raise ValueError("Контроль и кандидат должны проверяться на одинаковых данных.")
        return {n: meta for n, (_, meta, _) in zip(("control", "candidate"), runs)}

    def _select(self, selected):
        self._require("train", 24)
        dev = self._require("dev", 12)
        if selected not in {"auto", "control", "candidate"}:
            raise ValueError("SELECT: auto, control или candidate.")
        def rank(n):
            s = dev[n]["summary"]
            return (s["pass_rate"], s["mean_score"], -s["mean_llm_calls"], -(s["input_tokens"] + s["output_tokens"]), n == "control")
        if selected == "auto":
            selected = max(dev, key=rank)
        for destination, source in (("control", "control"), ("selected", selected)):
            write(self.root / "configs" / f"{destination}-frozen.json", read(self.root / "configs" / f"{source}.json"))
        write(self.root / "selection.json", {"selected": selected, "basis": "dev", "dev": dev,
            "identity": read(self.root / "protocol.json")["identity"],
            "frozen": {n: digest(self.root / "configs" / f"{n}-frozen.json") for n in ("control", "selected")}})
        print("Зафиксирован вариант:", selected, "· следующий ACTION = 'test'")
        return self.metrics()

    def metrics(self):
        rows = []
        for path in sorted(self.root.glob("*/summary.json")):
            s = read(path)["summary"]
            rows.append({"Серия": path.parent.name, "Успех": f"{s['passed']}/{s['n']}",
                         "LLM-вызовы/случай": round(s["mean_llm_calls"], 2),
                         "Input / output tokens": f"{s['input_tokens']} / {s['output_tokens']}", "API-ошибки": s["errors"]})
        return rows

    def outcomes(self, action):
        names = ("control", "selected") if action == "test" else ("control", "candidate")
        records = [{p.stem: read(p) for p in (self.root / f"{n}-{action}" / "records").glob("*.json")} for n in names]
        return [{"Задача": task, names[0]: records[0][task]["grade"]["passed"],
                 names[1]: records[1][task]["grade"]["passed"],
                 "Вызовы": f"{records[0][task]['llm_calls']} → {records[1][task]['llm_calls']}"}
                for task in sorted(records[0].keys() & records[1].keys())]
