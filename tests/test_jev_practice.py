import importlib.util
import json
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1] / "jev-workshop/practice"
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("jev_practice_runtime", ROOT / "runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def prediction(supported, error=False):
    return {"prediction": "supported" if supported > .5 else "contradicted",
            "probabilities": {"supported": supported, "contradicted": 1-supported, "insufficient_evidence": 0},
            **({"error": "Unavailable"} if error else {})}


def test_sql_regression_requires_currency_check():
    before = runtime.sql_fixture()
    assert before["result"] == [(3777,)] and all(before["hard_checks"].values())
    assert runtime.sql_fixture(add_currency_check=True)["hard_checks"]["settled_USD_reference"] is False
    fixed = runtime.sql_fixture(runtime.SQL_FIXED, True)
    assert fixed["result"] == [(3000,)] and all(fixed["hard_checks"].values())
    assert not runtime.sql_fixture("DROP TABLE payments")["hard_checks"]["executed"]


def test_cascade_calls_and_failures():
    calls = []
    def fast(state):
        calls.append("fast")
        return prediction(.99)
    def judge(state):
        calls.append("judge")
        return {"value": {"verdict": "supported"}}
    assert runtime.cascade({}, {"SQL": False}, .8, fast, judge)["route"] == "hard_fail"
    assert calls == []
    assert runtime.cascade({}, {"SQL": True}, .8, fast, judge)["route"] == "auto_accept"
    assert calls == ["fast"]
    result = runtime.cascade({}, {"SQL": True}, .8, lambda s: prediction(.01), judge)
    assert result["route"] == "judge" and result["p_supported"] == .01
    assert result["threshold"] == .8 and result["passes_threshold"] is False
    assert calls[-1] == "judge"
    # No acceptable calibration threshold means disable automatic acceptance.
    assert runtime.cascade({}, {"SQL": True}, None, lambda s: prediction(.99), judge)["route"] == "judge"
    def fail(state):
        raise RuntimeError("no")
    out = runtime.cascade({}, {"SQL": True}, .8, fail, fail)
    assert out["route"] == "judge_error" and out["accepted"] is False


def test_calibration_and_gate_dont_credit_judge():
    with pytest.raises(ValueError):
        runtime.calibrate_threshold([{**prediction(.99), "split": "test", "label": "supported"}])
    result = runtime.evaluate_gate([{**prediction(.95), "label": "contradicted"},
                                    {**prediction(.1), "label": "supported"}], .9)
    assert result["coverage"] == .5 and result["false_accept_rate"] == 1
    assert result["missed_bad_rate"] == 1 and result["escalations"] == 1
    assert result["final_cascade_quality"] is None and result["judge_calls_measured"] is None


def test_dataset_alignment_and_provenance():
    data = runtime.load_benchmark()
    assert {k: len(v) for k, v in data["cases"].items()} == {"calibration": 90, "test": 180, "challenge": 30}
    for name, splits in data["models"].items():
        for split, predictions in splits.items():
            assert {r["id"] for r in predictions} == {r["id"] for r in data["cases"][split]}
            assert all(runtime.validate_prediction(r) for r in predictions)
    assert len(data["provenance"]) == 15
    assert runtime.VERIFICATION["criteria"] == data["rubric"]["criteria"]
    # Historical benchmark keeps its exact original instructions; the new tool-message
    # recipe adds explicit observation provenance to those same three class definitions.
    assert runtime.VERIFICATION["instructions"].startswith(data["rubric"]["instructions"])


def test_rubric_frozen_before_answer():
    criteria = [{"id": "c1", "scope": "trace", "expectation": "Executed", "required_evidence": "Tool output"}]
    with pytest.raises(ValueError):
        runtime.freeze_rubric("request", criteria)
    rubric = runtime.freeze_rubric("request", criteria, approved=True)
    assert "answer" not in rubric
    state = runtime.rubric_state(rubric, "request", "done", [], {})
    assert state["rubric_version"] == rubric["version"]
    scoped = runtime.criterion_state(rubric, "c1", "request", "done", [], {})
    assert "trace" in scoped and "answer" not in scoped and "segments" not in scoped
    with pytest.raises(ValueError):
        runtime.rubric_state(rubric, "new request", "done", [], {})
    rubric["criteria"][0]["expectation"] = "mutated"
    with pytest.raises(ValueError):
        runtime.rubric_state(rubric, "request", "done", [], {})


def test_answer_is_compared_with_observed_facts_not_just_itself():
    criteria = [{"id": "truth", "scope": "answer", "expectation": "Matches observed facts",
                 "required_evidence": "Actual tool results"}]
    rubric = runtime.freeze_rubric("request", criteria, approved=True)
    trace = [{"tool": "refund", "result": "pending"}]
    scoped = runtime.criterion_state(rubric, "truth", "request", "Refund completed", trace, {})
    assert scoped["answer"] == "Refund completed" and scoped["evidence"] == trace


def test_semantic_snapshot_keeps_real_mistake_and_detailed_resolution():
    saved = runtime.recorded_support()
    by_case = {r["case"]: r for r in saved["rows"]}
    assert set(by_case) == {"partial", "unknown", "completed", "revised"}
    for row in saved["rows"]:
        assert runtime.digest(row["state"]) == row["input_sha256"]
        assert runtime.validate_prediction(row["jev"])
        assert row["jev"]["model"].startswith("typesafe/jev")
    assert by_case["partial"]["jev"]["prediction"] == "contradicted"
    assert by_case["revised"]["state"]["evidence"] == by_case["partial"]["state"]["evidence"]
    assert by_case["revised"]["jev"]["prediction"] == "supported"
    # The real fast-model error is intentionally visible, not edited into a perfect example.
    assert by_case["unknown"]["jev"]["prediction"] == "contradicted"
    detailed = saved["forced_fallback"]["judge"]
    assert detailed["model"] == "openai/gpt-6-luna"
    assert detailed["value"]["verdict"] == "insufficient_evidence"
    assert runtime.saved_prediction(runtime.support_state("partial")) == by_case["partial"]["jev"]
    with pytest.raises(ValueError, match="jev.classify"):
        runtime.saved_prediction(runtime.support_state("partial", claim="A new unrecorded answer"))


def test_probabilities_are_not_winner_confidence():
    row = {"prediction": "contradicted", "probabilities": {"supported": .01, "contradicted": .99, "insufficient_evidence": 0}}
    view = runtime.compact_decision(row)
    assert view["choice"] == "contradicted" and view["P(supported)"] == "1.0%"
    assert view["P(contradicted)"] == "99.0%"
    assert "model" not in view and "same input" not in view


def test_sdk_choice_and_editable_question_are_preserved_at_probability_tie():
    calls = []
    jev = object.__new__(runtime.Jev)
    def decide(state, questions):
        calls.append((state, questions))
        return {"answers": {"verification": {"choice": "insufficient_evidence",
            "probabilities": {"supported": .37, "contradicted": .26, "insufficient_evidence": .37}}}}
    jev.decide = decide
    question = {"type": "choice", "instructions": "My edited question", "criteria": runtime.VERIFICATION["criteria"]}
    state = {"request": "task", "claim": "answer", "evidence": []}
    result = jev.classify(state, question=question)
    assert calls == [(state, {"verification": question})]
    assert result["prediction"] == "insufficient_evidence"
    assert runtime.compact_decision(result)["choice"] == "insufficient_evidence"
    assert [runtime.observation_count(state["evidence"]) for _, _, state in runtime.trajectory_views()] == [0, 5, 5]


def test_notebook_uses_visible_editable_questions_and_explicit_state():
    notebook = json.loads((ROOT / "practice.ipynb").read_text(encoding="utf8"))
    sources = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
    question_cell = next(source for source in sources if source.startswith("verification ="))
    namespace = {"jev": None, "print": lambda *args: None, "table": lambda *args: None, "LABEL_TEXT": runtime.LABEL_TEXT}
    exec(question_cell, namespace)
    assert namespace["verification"] == runtime.VERIFICATION
    assert "question=verification" in question_cell
    text = "\n".join(sources)
    assert 'state = {"request": request, "claim": claim, "evidence": evidence}' in text
    assert "judge(state, question=verification)" in text
    assert "Объём evidence, символов" not in text


def test_judge_uses_sdk_and_never_silently_substitutes_model(monkeypatch):
    from types import SimpleNamespace
    import openai
    calls = []
    response = SimpleNamespace(model="openai/gpt-6-luna", usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"supported","reason":"facts"}'))])
    def create(**kwargs):
        calls.append(kwargs)
        return response
    available = ["openai/gpt-6-luna"]
    client = SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id=i) for i in available])),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: client)
    judge = runtime.DetailedJudge("test-key")
    assert judge.check_model() == "openai/gpt-6-luna"
    detailed = judge({"claim": "done"})
    assert detailed["value"]["verdict"] == "supported"
    assert detailed["exchange"]["request"] == calls[0]
    assert json.loads(detailed["exchange"]["request"]["messages"][1]["content"]) == {"claim": "done"}
    assert detailed["exchange"]["response"]["content"] == response.choices[0].message.content
    assert calls[0]["model"] == "openai/gpt-6-luna" and calls[0]["reasoning_effort"] == "none"
    available.clear()
    with pytest.raises(ValueError, match="автоматической замены нет"):
        judge.check_model()
    assert len(calls) == 1


def test_judge_error_keeps_current_attempt_without_credentials(monkeypatch):
    from types import SimpleNamespace
    import openai
    def fail(**kwargs):
        raise RuntimeError("unavailable")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: client)
    judge = runtime.DetailedJudge("do-not-log-this-key")
    judge.last_exchange = {"request": "old", "response": "old"}
    with pytest.raises(RuntimeError):
        judge({"claim": "new"})
    exchange = judge.last_exchange
    assert json.loads(exchange["request"]["messages"][1]["content"]) == {"claim": "new"}
    assert exchange["response"] is None
    assert "do-not-log-this-key" not in json.dumps(exchange)


def test_generated_criteria_flow_to_actual_questions_without_replacement():
    from notebook_cells import populate
    cells = []
    populate(lambda text: None, cells.append)
    generation = next(source for source in cells if source.startswith("request = SUPPORT_REQUEST"))
    evaluation = next(source for source in cells if source.startswith("# При необходимости отредактируйте criteria"))
    criteria = [{"id": "generated_unique", "scope": "answer", "expectation": "Unique user-generated condition",
                 "required_evidence": "Exact trace observations"}]
    proposal = {"value": {"criteria": criteria}, "exchange": {"request": {}, "response": {}}}
    calls = []
    def decide(state, questions):
        calls.append((state, questions))
        return {"answers": {name: {"choice": "insufficient_evidence", "probabilities":
            {"supported": .1, "contradicted": .2, "insufficient_evidence": .7}} for name in questions}}
    from types import SimpleNamespace
    namespace = {name: getattr(runtime, name) for name in dir(runtime) if not name.startswith("_")}
    namespace.update(json=json, escape=lambda text: text, display=lambda value: None, HTML=lambda text: text,
        print=lambda *args: None, table=lambda rows: None, draft_rubric=lambda *args: proposal,
        judge=None, jev=SimpleNamespace(decide=decide), claim="Actual answer", trace=runtime.TRAJECTORY)
    exec(generation, namespace)
    assert namespace["criteria"] == criteria and namespace["criteria"] is not criteria
    exec(evaluation, namespace)
    assert set(calls[0][1]) == {"generated_unique"}
    assert "Unique user-generated condition" in calls[0][1]["generated_unique"]["instructions"]
    assert calls[0][0]["answer"] == "Actual answer"
    assert calls[0][0]["evidence"] == runtime.TRAJECTORY
    assert namespace["rows"][0]["P(insufficient_evidence)"] == "70.0%"


def test_same_case_comparison_including_local_models():
    datasets = runtime.same_cases_comparison()
    assert len(datasets) == 6
    assert all(len(rows) == 12 for rows in datasets.values())
    ids = [{r["id"] for r in rows} for rows in datasets.values()]
    assert all(group == ids[0] for group in ids)


def test_archive_has_self_contained_safe_inputs():
    import zipfile
    with zipfile.ZipFile(ROOT / "practice.zip") as archive:
        names = archive.namelist()
        required = ["practice.ipynb", "runtime.py", "local_models.py", "data/benchmark.json",
                    "data/live_smoke.json", "data/semantic_smoke.json", "data/local_smoke.json", "data/pretrained_nli_smoke.json"]
        assert all("practice/" + name in names for name in required)
        assert not any(name.endswith((".js", ".pyc", ".pem")) or ".env" in name for name in names)
        assert not any(name.endswith(".zip") for name in names)
        notebook = json.loads(archive.read("practice/practice.ipynb"))
        assert all("harness_lab" not in "".join(cell["source"]) for cell in notebook["cells"])


def test_scope_ablation_preserves_claim_and_cannot_invent_evidence():
    rows = runtime.trajectory_views()
    assert len({row[2]["claim"] for row in rows}) == 1
    assert rows[0][2]["evidence"] == []
    assert rows[1][2]["evidence"] == runtime.brief_trajectory(runtime.TRAJECTORY)
    assert rows[2][2]["evidence"] == runtime.TRAJECTORY
    source = {event["event_id"]: event for event in runtime.TRAJECTORY}
    brief = runtime.brief_trajectory(runtime.TRAJECTORY)
    for row in brief["events"]:
        observed = source[row["source_event_id"]]
        assert observed["role"] == "tool"
        assert row["result"] == json.loads(observed["content"])
        call = next(call for call in source[row["source_call_event_id"]]["tool_calls"]
                    if call["id"] == row["tool_call_id"])
        assert row["arguments"] == json.loads(call["function"]["arguments"])
        assert row["tool"] == call["function"]["name"]
    assert len(brief["events"]) == sum(event["role"] == "tool" for event in runtime.TRAJECTORY)
    assert set(brief["source_event_ids"]).isdisjoint(brief["omitted_event_ids"])
    assert set(brief["source_event_ids"]) | set(brief["omitted_event_ids"]) == set(source)


def test_actual_agent_provenance_and_no_self_confirmation():
    import hashlib
    run = runtime.load_agent_run()
    assert run["events_sha256"] == runtime.digest(run["events"])
    assert run["environment"]["kind"] == "local_simulated_support_service"
    assert run["environment"]["real_financial_operations"] is False
    assert run["environment"]["source_sha256"] == hashlib.sha256((ROOT / "support_agent.py").read_bytes()).hexdigest()
    assert run["final_answer"] == run["events"][-1]["content"]
    assert run["events"][-1]["event_id"] == run["final_answer_event_id"]
    assert all(event["event_id"] != run["final_answer_event_id"] for event in runtime.TRAJECTORY)
    assert all(event["role"] != "system" for event in runtime.TRAJECTORY)
    calls = {call["id"]: call for event in run["events"] for call in event.get("tool_calls", [])}
    results = [event for event in run["events"] if event["role"] == "tool"]
    assert set(calls) == {event["tool_call_id"] for event in results}
    assert len(calls) == len(results)
    assert any(event["name"] == "get_refund_status" for event in results)
    status = runtime.check_operation_status()
    assert status["Продление отключено"] is True
    assert status["Статус возврата"] == "pending_review"
    assert status["Деньги возвращены"] is False
    assert status["Статус проверен после таймаута"] is True
    assert runtime.check_operation_status(runtime.trajectory_segment(("request_refund",)))["Статус возврата"] == "не проверен"


def test_trajectory_snapshot_is_actual_and_aligned():
    data = json.loads((ROOT / "data/trajectory_smoke.json").read_text(encoding="utf8"))
    assert data["source"].startswith("real API calls")
    assert data["questions"] == runtime.SEMANTIC_QUESTIONS
    assert data["agent_run_sha256"] == runtime.digest(runtime.load_agent_run())
    assert data["full_trace"] == runtime.TRAJECTORY
    assert data["brief_trace"] == runtime.brief_trajectory(runtime.TRAJECTORY)
    states = {scope: state for scope, _, state in runtime.trajectory_views()}
    for row in data["scope_rows"]:
        assert row["state"] == states[row["scope"]]
        assert row["input_sha256"] == runtime.digest(row["state"])
        runtime.validate_prediction(row["jev"])
    for row in data["semantic_rows"]:
        assert row["state"] == runtime.semantic_state(runtime.SEMANTIC_ANSWERS[row["case"]])
        assert row["input_sha256"] == runtime.digest(row["state"])
        score = row["result"]["answers"]["actionability"]
        assert 0 <= score["score"] <= 3
        assert set(score["probabilities"]) == {"0", "1", "2", "3"}
        assert runtime.semantic_view(row["state"]["answer"], row["result"])["Полезность, 0–3"] == round(score["score"], 2)


def test_notebook_requires_credentials_and_no_replay_mode():
    nb = json.loads((ROOT / "practice.ipynb").read_text(encoding="utf8"))
    text = "\n".join("".join(cell["source"]) for cell in nb["cells"])
    assert "getpass" in text and "Нужен ключ OpenRouter" in text
    assert "LIVE" not in text and "saved_prediction" not in text
    assert "SEMANTIC_QUESTIONS" in text and "brief_trajectory(trace)" in text
    assert '"Полный журнал", trace' in text and '"Краткий журнал", brief' in text
    assert '"state": state, "questions": {"verification": verification}' in text
