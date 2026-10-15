"""Export only measured predictions and public synthetic examples for the deck."""
import hashlib
import json
from pathlib import Path
from compare import ROOT, LABELS, metrics, fingerprint


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()] if path.exists() else []


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def risk_at(rows, threshold):
    valid = [r for r in rows if not r.get("error")]
    accepted = [r for r in valid if r["probabilities"][r["prediction"]] >= threshold]
    errors = sum(r["prediction"] != r["label"] for r in accepted)
    return {"n": len(valid), "accepted": len(accepted), "errors": errors,
            "coverage": len(accepted) / len(valid) if valid else 0,
            "risk": errors / len(accepted) if accepted else None}


def calibrated_policy(calibration, test):
    # A teaching policy, not a confidence bound on future risk. Choose only on calibration.
    candidates = [i / 100 for i in range(50, 101)]
    eligible = [(t, risk_at(calibration, t)) for t in candidates]
    eligible = [(t, r) for t, r in eligible if r["accepted"] >= 20 and r["risk"] <= .05]
    if not eligible:
        return {"threshold": None, "note": "No threshold meets the empirical calibration rule (>=20 accepted, <=5% errors)."}
    threshold, report = max(eligible, key=lambda x: (x[1]["coverage"], -x[0]))
    return {"threshold": threshold, "calibration": report, "test": risk_at(test, threshold),
            "note": "Selected on calibration only. <=5% is observed calibration error, not a future-risk guarantee."}


def main():
    data = ROOT / "experiment" / "data"
    artifacts = ROOT / "experiment" / "artifacts"
    cases = read_jsonl(data / "test.jsonl") + read_jsonl(ROOT / "results" / "challenge.jsonl")
    cases += [{**r, "split": "paraphrase"} for r in read_jsonl(ROOT / "results" / "test_paraphrases.jsonl")]
    index = {r["id"]: {**r, "predictions": {}} for r in cases}
    definitions = [
        ("jev", "Jev 1.13 · zero-shot", ROOT / "results" / "jev_test.jsonl", ROOT / "results" / "jev_challenge.jsonl", "HTTP end-to-end, включая сеть"),
        ("frozen", "mmBERT · обученная голова", artifacts / "frozen" / "predictions_test.jsonl", artifacts / "frozen" / "predictions_challenge.jsonl", "GPU batch, амортизированное время на пример"),
        ("local", "mmBERT · LoRA + голова", artifacts / "lora" / "predictions_test.jsonl", artifacts / "lora" / "predictions_challenge.jsonl", "GPU batch, амортизированное время на пример"),
    ]
    models = []
    for model_id, label, test_path, challenge_path, scope in definitions:
        test, challenge = read_jsonl(test_path), read_jsonl(challenge_path)
        paraphrase_path = ROOT / "results" / "jev_paraphrase.jsonl" if model_id == "jev" else test_path.parent / "predictions_paraphrase.jsonl"
        paraphrases = read_jsonl(paraphrase_path)
        calibration_path = ROOT / "results" / "jev_calibration.jsonl" if model_id == "jev" else test_path.parent / "predictions_calibration.jsonl"
        calibration = read_jsonl(calibration_path)
        if not test:
            continue
        expected = {r["id"] for r in cases if r.get("split") == "test"}
        actual = {r["id"] for r in test}
        if expected != actual:
            raise ValueError(f"{model_id}: expected {len(expected)} test IDs, got {len(actual)}")
        model = {"id": model_id, "label": label, "metrics": metrics(test),
                 "challenge_metrics": metrics(challenge), "latency_scope": scope,
                 "paraphrase_metrics": metrics(paraphrases), "calibration_policy": calibrated_policy(calibration, test),
                 "source": str(test_path.relative_to(ROOT)).replace("\\", "/")}
        model["metrics"]["latency_scope"] = scope
        stress_scope = scope if model_id == "jev" else "GPU single example, model forward; без сети и токенизации"
        model["challenge_metrics"]["latency_scope"] = stress_scope
        model["paraphrase_metrics"]["latency_scope"] = stress_scope
        if model_id != "jev":
            model["training"] = read_json(test_path.parent / "metrics.json")
            model["raw_metrics"] = metrics(read_jsonl(test_path.parent / "predictions_test_raw.jsonl"))
            model["raw_metrics"]["latency_scope"] = scope
            expected_hash = model["training"].get("data_sha256", {}).get("test")
            if expected_hash and expected_hash != hashlib.sha256((data / "test.jsonl").read_bytes()).hexdigest():
                raise ValueError(f"{model_id}: stale test predictions")
        models.append(model)
        for row in test + challenge + paraphrases:
            if row["id"] not in index:
                raise ValueError(f"Unknown prediction ID {row['id']}")
            if row["label"] != index[row["id"]]["label"]:
                raise ValueError(f"Gold mismatch for {row['id']}")
            if model_id == "jev" and row.get("fingerprint") != fingerprint(index[row["id"]], row["model"]):
                raise ValueError(f"Input/rubric mismatch for {row['id']}")
            index[row["id"]]["predictions"][model_id] = row
    manifest = read_json(data / "manifest.json")
    payload = {
        "models": models, "cases": list(index.values()),
        "experiment": {"task": "Семантическая проверка утверждений по наблюдениям инструментов", "model": "jhu-clsp/mmBERT-base",
                       "manifest": manifest, "train_count": len(read_jsonl(data / "train.jsonl")),
                       "dev_count": len(read_jsonl(data / "dev.jsonl")),
                       "calibration_count": len(read_jsonl(data / "calibration.jsonl")),
                       "test_count": len(read_jsonl(data / "test.jsonl")),
                       "test_sha256": hashlib.sha256((data / "test.jsonl").read_bytes()).hexdigest(),
                       "majority_accuracy": max(sum(r["label"] == k for r in cases if r.get("split") == "test") for k in LABELS) / max(1, sum(r.get("split") == "test" for r in cases)),
                       "limitations": ["Синтетический набор. Семейства операций разделены, но структура шаблонов общая.",
                                        "Пилот использовался для исправления неоднозначных gold labels, а не настройки моделей на финальном тесте.",
                                        "Challenge: 30 примеров ассистента, без независимой человеческой разметки. Один исходный gold спорный.",
                                        "Paraphrase: 18 принятых перефразировок DeepSeek из 30. Проверяет тот же учитель. Классы несбалансированы, набор мал и зависим от исходного test.",
                                        "Порог в интерактивном тесте служит исследованию ошибок. Рабочий порог выбирается на calibration.",
                                        "Время API Jev включает сеть, время batch GPU локальной модели имеет другой протокол. Отношение скоростей по этим столбцам считать нельзя."]}}
    destination = ROOT / "results" / "workshop-data.json"
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(models)} measured models, {len(cases)} cases to {destination}")


if __name__ == "__main__":
    main()
