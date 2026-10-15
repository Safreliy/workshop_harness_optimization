"""Inference CLI and localhost HTTP service for the trained NLI judge."""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from dataset import LABELS, serialize_input


def load_model(checkpoint: Path, device: torch.device):
    config = json.loads((checkpoint / "experiment_config.json").read_text(encoding="utf-8"))
    tokenizer = AutoTokenizer.from_pretrained(checkpoint)
    base = AutoModelForSequenceClassification.from_pretrained(
        config["model_id"], num_labels=len(LABELS), id2label=dict(enumerate(LABELS)),
        label2id={label: i for i, label in enumerate(LABELS)}, revision=config["model_revision"],
    )
    if config["mode"] == "frozen":
        weights = torch.load(checkpoint / "task_head.pt", map_location="cpu", weights_only=True)
        missing, unexpected = base.load_state_dict(weights, strict=False)
        backbone_prefix = base.base_model_prefix + "."
        missing_head = [name for name in missing if not name.startswith(backbone_prefix)]
        if unexpected or missing_head or not weights:
            raise RuntimeError(f"Invalid task-head checkpoint: missing={missing_head}, unexpected={unexpected}")
        model = base
    else:
        from peft import PeftModel
        model = PeftModel.from_pretrained(base, checkpoint)
    model.to(device)
    model.eval()
    temperature_path = checkpoint / "temperature.json"
    temperature = json.loads(temperature_path.read_text(encoding="utf-8"))["temperature"] if temperature_path.exists() else 1.0
    return model, tokenizer, temperature


def infer_one(model, tokenizer, temperature: float, row: dict, device: torch.device, max_length: int = 256) -> dict:
    first, second = serialize_input(row)
    full_length = len(tokenizer(first, second, truncation=False)["input_ids"])
    inputs = tokenizer(first, second, truncation=True, max_length=max_length, return_tensors="pt").to(device)
    start = time.perf_counter()
    with torch.inference_mode():
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=device.type == "cuda"):
            logits = model(**inputs).logits.float()[0] / temperature
        if device.type == "cuda":
            torch.cuda.synchronize()
    probabilities = torch.softmax(logits, dim=-1).cpu().tolist()
    return {
        "id": row.get("id"),
        "label": row.get("label"),
        "prediction": LABELS[max(range(len(LABELS)), key=lambda i: probabilities[i])],
        "probabilities": {label: float(probabilities[i]) for i, label in enumerate(LABELS)},
        "latency_ms": (time.perf_counter() - start) * 1000,
        "model": "mmBERT-base NLI",
        "input_tokens": full_length,
        "truncated": full_length > max_length,
    }


def serve(model, tokenizer, temperature, device, port, max_length):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/predict":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 65536:
                    raise ValueError("Content-Length must be between 1 and 65536")
                row = json.loads(self.rfile.read(size))
                if not all(isinstance(row.get(key), str) and row[key] for key in ("request", "evidence", "claim")):
                    raise ValueError("request, evidence, claim must be nonempty strings")
                with lock:
                    result = infer_one(model, tokenizer, temperature, row, device, max_length)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
            except (ValueError, json.JSONDecodeError) as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving http://127.0.0.1:{port}/predict", flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, tokenizer, temperature = load_model(args.checkpoint, device)
    config = json.loads((args.checkpoint / "experiment_config.json").read_text(encoding="utf-8"))
    max_length = config["max_length"]
    if args.serve:
        serve(model, tokenizer, temperature, device, args.port, max_length)
        return
    if not args.input or not args.output:
        parser.error("--input and --output required unless --serve")
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line]
    with args.output.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(infer_one(model, tokenizer, temperature, row, device, max_length), ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} predictions to {args.output}", flush=True)


if __name__ == "__main__":
    main()
