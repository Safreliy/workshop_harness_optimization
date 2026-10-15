"""Train a compact NLI judge on a multilingual encoder.

Run once with --mode frozen (classification head only) and then --mode lora
(the same encoder with low-rank adapters). Model selection uses dev, temperature
uses calibration, and test is read only after the checkpoint is fixed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import model_info
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from dataset import LABELS, serialize_input

MODEL_ID = "jhu-clsp/mmBERT-base"
DEFAULT_REVISION = "c5955035435e2bf121cde7f3c8863ef52ff35d82"
LABEL_TO_ID = {name: i for i, name in enumerate(LABELS)}


class Records(Dataset):
    def __init__(self, path: Path, tokenizer, max_length: int):
        self.rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        self.encodings = []
        for row in self.rows:
            first, second = serialize_input(row)
            self.encodings.append(tokenizer(first, second, truncation=True, max_length=max_length))

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return self.encodings[index], LABEL_TO_ID[self.rows[index]["label"]]


def collator(tokenizer):
    def collate(items):
        encoded, labels = zip(*items)
        batch = tokenizer.pad(list(encoded), padding=True, return_tensors="pt")
        batch["labels"] = torch.tensor(labels, dtype=torch.long)
        return batch
    return collate


def input_length_stats(paths, tokenizer, max_length):
    result = {}
    for name, path in paths.items():
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        lengths = np.array([len(tokenizer(*serialize_input(row), truncation=False)["input_ids"]) for row in rows])
        result[name] = {
            "min": int(lengths.min()), "p50": float(np.percentile(lengths, 50)),
            "p95": float(np.percentile(lengths, 95)), "max": int(lengths.max()),
            "truncated_fraction": float((lengths > max_length).mean()),
        }
    return result


def predictions(model, dataset, tokenizer, batch_size, device):
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, collate_fn=collator(tokenizer))
    model.eval()
    logits, durations = [], []
    with torch.inference_mode():
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            start = time.perf_counter()
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
                output = model(**batch)
            if device.type == "cuda":
                torch.cuda.synchronize()
            elapsed = (time.perf_counter() - start) * 1000 / len(batch["labels"])
            logits.extend(output.logits.detach().float().cpu().numpy().tolist())
            durations.extend([elapsed] * len(batch["labels"]))
    return np.array(logits), durations


def score(rows, logits, temperature=1.0):
    y = np.array([LABEL_TO_ID[row["label"]] for row in rows])
    pred = (logits / temperature).argmax(axis=1)
    z = logits / temperature
    z = z - z.max(axis=1, keepdims=True)
    probabilities = np.exp(z) / np.exp(z).sum(axis=1, keepdims=True)
    confidence = probabilities.max(axis=1)
    correct = (pred == y).astype(float)
    ece = sum(
        float((mask.mean()) * abs(correct[mask].mean() - confidence[mask].mean()))
        for low, high in zip(np.linspace(0, 1, 11)[:-1], np.linspace(0, 1, 11)[1:])
        if (mask := (confidence >= low) & (confidence < high if high < 1 else confidence <= high)).any()
    )
    return {
        "accuracy": float(accuracy_score(y, pred)),
        "macro_f1": float(f1_score(y, pred, average="macro", labels=list(range(3)), zero_division=0)),
        "confusion_matrix": confusion_matrix(y, pred, labels=list(range(3))).tolist(),
        "support": {label: int((y == index).sum()) for index, label in enumerate(LABELS)},
        "nll": float(-np.log(np.clip(probabilities[np.arange(len(y)), y], 1e-9, 1)).mean()),
        "brier": float(np.square(probabilities - np.eye(len(LABELS))[y]).sum(axis=1).mean()),
        "ece_10_bins": float(ece),
    }


def fit_temperature(rows, logits):
    y = np.array([LABEL_TO_ID[row["label"]] for row in rows])
    best_t, best_nll = 1.0, float("inf")
    for t in np.linspace(0.25, 5.0, 191):
        z = logits / t
        z = z - z.max(axis=1, keepdims=True)
        probabilities = np.exp(z) / np.exp(z).sum(axis=1, keepdims=True)
        nll = -np.log(np.clip(probabilities[np.arange(len(y)), y], 1e-9, 1)).mean()
        if nll < best_nll:
            best_t, best_nll = float(t), float(nll)
    return best_t, best_nll


def write_predictions(path, rows, logits, durations, temperature):
    z = logits / temperature
    z = z - z.max(axis=1, keepdims=True)
    probs = np.exp(z) / np.exp(z).sum(axis=1, keepdims=True)
    with path.open("w", encoding="utf-8") as output:
        for row, probabilities, latency in zip(rows, probs, durations):
            output.write(json.dumps({
                "id": row["id"],
                "label": row["label"],
                "prediction": LABELS[int(probabilities.argmax())],
                "probabilities": {label: float(probabilities[i]) for i, label in enumerate(LABELS)},
                "latency_ms": float(latency),
            }, ensure_ascii=False) + "\n")


def save_checkpoint(model, tokenizer, output: Path, mode: str, max_length: int, revision: str):
    output.mkdir(parents=True, exist_ok=True)
    tokenizer.save_pretrained(output)
    if mode == "frozen":
        backbone_prefix = model.base_model_prefix + "."
        task_head = {name: parameter.cpu() for name, parameter in model.state_dict().items() if not name.startswith(backbone_prefix)}
        if not task_head:
            raise RuntimeError("No task-head parameters found")
        torch.save(task_head, output / "task_head.pt")
    else:
        model.save_pretrained(output)
    (output / "experiment_config.json").write_text(json.dumps({
        "model_id": MODEL_ID, "model_revision": revision, "mode": mode, "labels": LABELS, "max_length": max_length,
    }, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["frozen", "lora"], required=True)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    args = parser.parse_args()
    run_start = time.perf_counter()

    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device}; mode={args.mode}; model={MODEL_ID}", flush=True)
    revision = args.revision
    if model_info(MODEL_ID, revision=revision).sha != revision:
        raise RuntimeError("Requested model revision did not resolve to the pinned commit")
    print(f"revision={revision}", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=revision)
    length_stats = input_length_stats({split: args.data / f"{split}.jsonl" for split in ("train", "dev", "calibration", "test")}, tokenizer, args.max_length)
    print(f"input_length_stats={json.dumps(length_stats)}", flush=True)
    if any(item["truncated_fraction"] > 0 for item in length_stats.values()):
        raise RuntimeError(f"max_length={args.max_length} would truncate labeled examples")
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_ID, num_labels=len(LABELS), id2label=dict(enumerate(LABELS)),
        label2id=LABEL_TO_ID, revision=revision,
    )
    backbone_prefix = model.base_model_prefix + "."
    task_head_names = sorted({name.split(".")[0] for name, _ in model.named_parameters() if not name.startswith(backbone_prefix)})
    print(f"task_head_modules={task_head_names}", flush=True)
    if args.mode == "frozen":
        for name, parameter in model.named_parameters():
            parameter.requires_grad = not name.startswith(backbone_prefix)
    else:
        from peft import LoraConfig, TaskType, get_peft_model
        model = get_peft_model(model, LoraConfig(
            task_type=TaskType.SEQ_CLS, r=8, lora_alpha=16, lora_dropout=0.05,
            target_modules="all-linear", modules_to_save=task_head_names,
        ))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"trainable_parameters={trainable}", flush=True)
    if trainable == 0:
        raise RuntimeError("No trainable parameters")
    model.to(device)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats()
    train = Records(args.data / "train.jsonl", tokenizer, args.max_length)
    dev = Records(args.data / "dev.jsonl", tokenizer, args.max_length)
    calibration = Records(args.data / "calibration.jsonl", tokenizer, args.max_length)
    test = Records(args.data / "test.jsonl", tokenizer, args.max_length)
    loader = DataLoader(train, batch_size=args.batch_size, shuffle=True, collate_fn=collator(tokenizer))
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=5e-4 if args.mode == "frozen" else 1e-4, weight_decay=0.01)
    best_f1, best_epoch = -1.0, 0
    mode_dir = args.output / args.mode
    mode_dir.mkdir(parents=True, exist_ok=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        losses = []
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
                logits = model(**{k: v for k, v in batch.items() if k != "labels"}).logits
                loss = torch.nn.functional.cross_entropy(logits.float(), batch["labels"])
            loss.backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            optimizer.step()
            losses.append(loss.item())
        dev_logits, _ = predictions(model, dev, tokenizer, args.batch_size, device)
        dev_score = score(dev.rows, dev_logits)
        print(f"epoch={epoch} loss={np.mean(losses):.4f} dev_macro_f1={dev_score['macro_f1']:.4f} dev_accuracy={dev_score['accuracy']:.4f}", flush=True)
        if dev_score["macro_f1"] > best_f1:
            best_f1, best_epoch = dev_score["macro_f1"], epoch
            save_checkpoint(model, tokenizer, mode_dir / "checkpoint", args.mode, args.max_length, revision)
    # Reconstruct exactly the dev-selected checkpoint for calibration and test.
    from infer import load_model
    del model
    if device.type == "cuda":
        torch.cuda.empty_cache()
    model, tokenizer, _ = load_model(mode_dir / "checkpoint", device)
    calib_logits, calib_latencies = predictions(model, calibration, tokenizer, args.batch_size, device)
    temperature, calib_nll = fit_temperature(calibration.rows, calib_logits)
    test_logits, latencies = predictions(model, test, tokenizer, args.batch_size, device)
    from infer import infer_one
    warm_row = test.rows[0]
    for _ in range(2):
        infer_one(model, tokenizer, temperature, warm_row, device, args.max_length)
    single_latencies = [infer_one(model, tokenizer, temperature, warm_row, device, args.max_length)["latency_ms"] for _ in range(10)]
    metrics = {
        "mode": args.mode, "model_id": MODEL_ID, "selected_epoch": best_epoch,
        "model_revision": revision,
        "input_length_stats": length_stats,
        "data_sha256": {split: hashlib.sha256((args.data / f"{split}.jsonl").read_bytes()).hexdigest() for split in ("train", "dev", "calibration", "test")},
        "package_versions": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "peft", "scikit-learn")},
        "dev_macro_f1": best_f1, "temperature": temperature,
        "calibration_raw": score(calibration.rows, calib_logits, 1.0),
        "calibration_calibrated": score(calibration.rows, calib_logits, temperature),
        "test_raw": score(test.rows, test_logits, 1.0),
        "test": score(test.rows, test_logits, temperature),
        "median_batch_test_latency_ms_per_record": float(np.median(latencies)),
        "median_warm_single_infer_latency_ms": float(np.median(single_latencies)),
        "peak_process_gpu_memory_mib": float(torch.cuda.max_memory_allocated() / (1024**2)) if device.type == "cuda" else 0.0,
        "duration_seconds": float(time.perf_counter() - run_start),
        "train_count": len(train), "dev_count": len(dev), "calibration_count": len(calibration), "test_count": len(test),
    }
    (mode_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (mode_dir / "checkpoint" / "temperature.json").write_text(json.dumps({"temperature": temperature}), encoding="utf-8")
    write_predictions(mode_dir / "predictions_calibration_raw.jsonl", calibration.rows, calib_logits, calib_latencies, 1.0)
    write_predictions(mode_dir / "predictions_calibration.jsonl", calibration.rows, calib_logits, calib_latencies, temperature)
    write_predictions(mode_dir / "predictions_test_raw.jsonl", test.rows, test_logits, latencies, 1.0)
    write_predictions(mode_dir / "predictions_test.jsonl", test.rows, test_logits, latencies, temperature)
    print(json.dumps(metrics, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
