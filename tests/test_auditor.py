import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from harness_lab.auditor import (
    AuditError,
    AuditStore,
    OpenRouterAuditor,
    _validated_criteria,
    combine_judgments,
)
from harness_lab.settings import Settings
from harness_lab.agent import AgentResult
from harness_lab.config import HarnessConfig
from harness_lab.demo import DemoHandler, validate_arena_histories
from harness_lab import demo as demo_module


CRITERIA = [
    {"id": "c1", "expectation": "Назвать узкое место", "evidence": "Указан Seq Scan"},
    {"id": "c2", "expectation": "Обосновать индекс", "evidence": "Есть план запроса"},
]


def test_criteria_are_frozen_as_checkable_expectations():
    criteria = _validated_criteria(
        [
            {"expectation": "Назвать узкое место", "evidence": "Указан Seq Scan"},
            {"expectation": "Обосновать индекс", "evidence": "Есть план запроса"},
        ]
    )
    assert criteria == CRITERIA
    with pytest.raises(AuditError):
        _validated_criteria([{"expectation": "Хороший ответ", "evidence": ""}])


def test_disagreement_is_uncertain_not_a_pass():
    grade = combine_judgments(
        CRITERIA,
        {"c1": {"noul": 0.91}, "c2": {"noul": 0.12}},
        {
            "c1": {"verdict": "fail", "reason": "Нет измерения"},
            "c2": {"verdict": "fail", "reason": "План не приведён"},
        },
    )
    assert [row["verdict"] for row in grade["criteria"]] == ["uncertain", "fail"]
    assert (grade["passed"], grade["failed"], grade["uncertain"]) == (0, 1, 1)


def test_audit_session_is_bound_to_agent_and_exact_history():
    store = AuditStore()
    history = [{"role": "user", "content": "Как ускорить SQL?"}]
    audit_id = store.create("baseline", history, CRITERIA)
    with pytest.raises(ValueError):
        store.record_result(audit_id, "optimized", history, {"output": "x"})
    with pytest.raises(ValueError):
        store.record_result(
            audit_id, "baseline", [{"role": "user", "content": "Другой вопрос"}],
            {"output": "x"},
        )
    store.record_result(audit_id, "baseline", history, {"output": "x"})
    assert store.get(audit_id).result == {"output": "x"}


def test_external_auditor_uses_precommitted_criteria_and_observations(monkeypatch):
    auditor = OpenRouterAuditor(
        Settings("internal", "https://internal.test", "model", "postgresql://test", "router-key")
    )
    calls = []

    def fake_chat(system, user, max_tokens):
        calls.append((system, user, max_tokens))
        if "ДО того" in system:
            return {"criteria": [{"expectation": c["expectation"], "evidence": c["evidence"]} for c in CRITERIA]}
        return {
            "checks": [
                {"id": "c1", "verdict": "pass", "reason": "Seq Scan в EXPLAIN"},
                {"id": "c2", "verdict": "pass", "reason": "Индекс проверен"},
            ],
            "summary": "Оба требования выполнены.",
        }

    def fake_jev(state, criteria):
        assert criteria == CRITERIA
        assert state["agent_answer"] == "План: Seq Scan"
        assert state["tool_observations"][0]["tool"] == "explain_query"
        return {"c1": {"noul": 0.94}, "c2": {"noul": 0.83}}

    monkeypatch.setattr(auditor, "_chat_json", fake_chat)
    monkeypatch.setattr(auditor, "_jev_decisions", fake_jev)
    history = [{"role": "user", "content": "Как ускорить SQL?"}]
    assert auditor.plan(history) == CRITERIA
    result = auditor.evaluate(
        history, CRITERIA, "План: Seq Scan",
        [{"stage": "tool", "data": {"name": "explain_query", "arguments": "{}", "output": "Seq Scan"}}],
    )
    assert result["passed"] == 2
    assert result["failed"] == 0
    assert result["summary"] == "Подтверждено 2/2; не выполнено 0; неоднозначно 0."
    assert result["model_summary"] == "Оба требования выполнены."
    assert calls[0][1] == {"history": history}
    assert calls[1][1]["criteria"] == CRITERIA


def test_live_chat_audit_api_freezes_plan_before_agent_and_returns_grade(monkeypatch, tmp_path):
    calls = []

    class FakeAuditor:
        def __init__(self, settings):
            pass

        def plan(self, history):
            calls.append("plan")
            return CRITERIA

        def evaluate(self, history, criteria, answer, events):
            calls.append("check")
            assert criteria == CRITERIA
            assert answer == "## Ответ\n- пункт"
            return {
                **combine_judgments(
                    CRITERIA,
                    {"c1": {"noul": 0.9}, "c2": {"noul": 0.9}},
                    {"c1": {"verdict": "pass"}, "c2": {"verdict": "pass"}},
                ),
                "summary": "Проверено.",
            }

    def fake_run(settings, config, history):
        calls.append("agent")
        return AgentResult(case_id="free_chat", output="## Ответ\n- пункт"), 0.1

    monkeypatch.setattr(demo_module, "OpenRouterAuditor", FakeAuditor)
    monkeypatch.setattr(demo_module, "run_free_chat", fake_run)
    settings = Settings("internal", "https://internal.test", "model", "postgresql://test", "router-key")
    handler = partial(
        DemoHandler,
        directory="presentation/dist",
        settings=settings,
        cases={},
        configs={"baseline": HarnessConfig.from_yaml("configs/baseline.yaml")},
        artifact_root=tmp_path,
        audit_store=AuditStore(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, payload):
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    try:
        history = [{"role": "user", "content": "Что делать с запросом?"}]
        planned = post("/api/audit/plan", {"agent": "baseline", "messages": history})
        assert planned["criteria"] == CRITERIA
        with pytest.raises(HTTPError) as error:
            post(
                "/api/chat",
                {"agent": "baseline", "messages": [{"role": "user", "content": "Другой запрос"}], "audit_id": planned["audit_id"]},
            )
        assert error.value.code == 400
        answered = post(
            "/api/chat",
            {"agent": "baseline", "messages": history, "audit_id": planned["audit_id"]},
        )
        assert answered["result"]["output"] == "## Ответ\n- пункт"
        checked = post("/api/audit/check", {"agent": "baseline", "audit_id": planned["audit_id"]})
        assert checked["evaluation"]["passed"] == 2
        assert calls == ["plan", "agent", "check"]
    finally:
        server.shutdown()
        server.server_close()


def test_arena_accepts_separate_replies_but_requires_identical_user_turns():
    histories = {
        "baseline": [
            {"role": "user", "content": "Первый вопрос"},
            {"role": "assistant", "content": "Первый ответ"},
            {"role": "user", "content": "Уточнение"},
        ],
        "optimized": [
            {"role": "user", "content": "Первый вопрос"},
            {"role": "assistant", "content": "Другой ответ"},
            {"role": "user", "content": "Уточнение"},
        ],
    }
    assert validate_arena_histories(histories) == histories
    histories["optimized"][-1]["content"] = "Иное уточнение"
    with pytest.raises(ValueError, match="same user turns"):
        validate_arena_histories(histories)


def test_arena_plans_once_before_both_agents_and_grades_on_shared_rubric(monkeypatch, tmp_path):
    calls = []

    class FakeAuditor:
        def __init__(self, settings):
            pass

        def plan(self, history):
            calls.append(("plan", history))
            return CRITERIA

        def evaluate(self, history, criteria, answer, events):
            calls.append(("check", answer))
            assert criteria == CRITERIA
            return {
                **combine_judgments(
                    criteria,
                    {"c1": {"noul": 0.9 if answer == "optimized" else 0.1}, "c2": {"noul": 0.9}},
                    {"c1": {"verdict": "pass" if answer == "optimized" else "fail"}, "c2": {"verdict": "pass"}},
                ),
                "summary": "Проверено.",
            }

    def fake_run(settings, config, history):
        answer = "optimized" if config.routing == "task_kind" else "baseline"
        calls.append(("agent", answer, history))
        return AgentResult(case_id="free_chat", output=answer), 0.1

    monkeypatch.setattr(demo_module, "OpenRouterAuditor", FakeAuditor)
    monkeypatch.setattr(demo_module, "run_free_chat", fake_run)
    store = AuditStore()
    handler = partial(
        DemoHandler,
        directory="presentation/dist",
        settings=Settings("internal", "https://internal.test", "model", "postgresql://test", "router-key"),
        cases={},
        configs={
            "baseline": HarnessConfig.from_yaml("configs/baseline.yaml"),
            "optimized": HarnessConfig.from_yaml("configs/baseline.yaml").model_copy(update={"routing": "task_kind"}),
        },
        artifact_root=tmp_path,
        audit_store=store,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, payload):
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    try:
        histories = {
            "baseline": [{"role": "user", "content": "Вопрос"}, {"role": "assistant", "content": "А"}, {"role": "user", "content": "Уточнение"}],
            "optimized": [{"role": "user", "content": "Вопрос"}, {"role": "assistant", "content": "Б"}, {"role": "user", "content": "Уточнение"}],
        }
        with pytest.raises(HTTPError) as mismatch:
            post("/api/arena/plan", {"histories": {**histories, "optimized": [{"role": "user", "content": "Иной вопрос"}]}})
        assert mismatch.value.code == 400
        planned = post("/api/arena/plan", {"histories": histories})
        assert planned["criteria"] == CRITERIA
        assert len([item for item in calls if item[0] == "plan"]) == 1
        assert calls[0][1] == [{"role": "user", "content": "Вопрос"}, {"role": "user", "content": "Уточнение"}]
        for agent in ("baseline", "optimized"):
            audit_id = planned["audit_ids"][agent]
            assert store.get(audit_id).criteria == CRITERIA
            with pytest.raises(HTTPError) as tamper:
                post("/api/chat", {"agent": agent, "messages": [{"role": "user", "content": "Другой запрос"}], "audit_id": audit_id})
            assert tamper.value.code == 400
            post("/api/chat", {"agent": agent, "messages": histories[agent], "audit_id": audit_id})
            checked = post("/api/audit/check", {"agent": agent, "audit_id": audit_id})
            assert checked["evaluation"]["passed"] == (2 if agent == "optimized" else 1)
        assert [item[0] for item in calls] == ["plan", "agent", "check", "agent", "check"]
    finally:
        server.shutdown()
        server.server_close()
