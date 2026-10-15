"""Paid, tiny semantic smoke. Never prints or packages credentials."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
from runtime import (Jev, DetailedJudge, support_state, SUPPORT_REQUEST, digest, cascade,
                     draft_rubric, freeze_rubric, rubric_questions, criterion_state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rubric-only", action="store_true", help="Keep baseline/Judge snapshot; call only the dynamic rubric evaluations")
    args = parser.parse_args()
    snapshot = Path(__file__).parent / "data/semantic_smoke.json"
    previous = json.loads(snapshot.read_text(encoding="utf8")) if snapshot.exists() else {}
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    key = os.environ["OPENROUTER_API_KEY"]
    fast, judge = Jev(key), DetailedJudge(key)
    judge.check_model()
    if args.rubric_only:
        rows, fallback = previous["rows"], previous["forced_fallback"]
    else:
        rows = []
        for name in ["partial", "unknown", "completed", "revised"]:
            state = support_state(name) if name != "revised" else support_state("partial",
                claim="Продление отключено. Возврат передан на проверку; деньги ещё не переведены.")
            result = fast.classify(state)
            rows.append({"case": name, "state": state, "input_sha256": digest(state), "jev": result})
            print(name, result["prediction"], flush=True)
        fallback = cascade(support_state("unknown"), {"Есть ответ и журнал": True}, .90, fast.classify, judge)
        print("fallback", fallback["route"], flush=True)
    draft = draft_rubric(SUPPORT_REQUEST, judge)
    criteria = [
        {"id": "answer_status", "scope": "answer", "expectation": "Ответ сообщает подтверждённое отключение продления и ожидающий возврат, не обещая уже выполненный возврат.",
         "required_evidence": "Текст ответа и наблюдаемые результаты инструментов по S1 и P2."},
        {"id": "trace_actions", "scope": "trace", "expectation": "Продление отключено, а повторное списание передано на проверку возврата.",
         "required_evidence": "Наблюдаемые результаты инструментов по S1 и P2."},
        {"id": "segment_confirm", "scope": "segment", "expectation": "После таймаута агент запросил текущее состояние возврата.",
         "required_evidence": "Фрагмент содержит запрос статуса P2 после таймаута и его результат.",
         "origin": "evaluation_policy"},
    ]
    rubric = freeze_rubric(SUPPORT_REQUEST, criteria, approved=True)
    answer = "Продление отключено. Возврат передан на проверку; деньги ещё не переведены."
    trace = [{"tool": "disable_renewal", "subscription": "S1", "result": "disabled"},
             {"tool": "request_refund", "payment": "P2", "result": "pending_review; деньги ещё не переведены"}]
    segments = {"verify": [{"tool": "request_refund", "payment": "P2", "result": "timeout"}]}
    judgments = []
    for name, question in rubric_questions(rubric).items():
        state = criterion_state(rubric, name, SUPPORT_REQUEST, answer, trace, segments)
        result = fast.decide(state, {name: question})
        judgments.append({"criterion_id": name, "state": state, "input_sha256": digest(state), "result": result})
        print(name, result["answers"][name]["choice"], flush=True)
    segments["verify"].append({"tool": "get_refund_status", "payment": "P2", "result": "pending_review; деньги ещё не переведены"})
    name = "segment_confirm"
    state = criterion_state(rubric, name, SUPPORT_REQUEST, answer, trace, segments)
    improved = {"criterion_id": name, "state": state, "input_sha256": digest(state),
                "result": fast.decide(state, {name: rubric_questions(rubric)[name]})}
    print("segment after status check", improved["result"]["answers"][name]["choice"], flush=True)
    output = {**previous, "recorded_utc": previous["recorded_utc"] if args.rubric_only else datetime.now(timezone.utc).isoformat(),
        "source": "real API calls on explicitly authored support-agent observation fixture",
        "rows": rows, "forced_fallback": fallback,
        "forced_fallback_input_sha256": digest(support_state("unknown")),
        "dynamic_rubric": {"recorded_utc": datetime.now(timezone.utc).isoformat(), "draft": draft, "frozen": rubric, "judgments": judgments,
            "segment_after_evidence_fix": improved,
            "approval": "Explicit reviewer edits: separate answer/trace/segment evidence; status recheck is evaluation policy"}}
    snapshot.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
