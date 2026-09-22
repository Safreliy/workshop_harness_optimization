import json

import pytest

from harness_lab.demo import (
    _chat_needs_database,
    _compress_events,
    _summarize_tool_output,
    build_custom_case,
    validate_chat_messages,
)


def test_summarize_trial_index_output_keeps_measured_evidence():
    raw = json.dumps(
        {
            "ok": True,
            "result": {
                "baseline_total_cost": 100.0,
                "candidate_total_cost": 20.0,
                "cost_ratio": 5.0,
                "rolled_back": True,
            },
        }
    )
    summary = _summarize_tool_output("trial_index_plan", raw)
    assert "100.0 → 20.0" in summary
    assert "5.0×" in summary
    assert "откатан" in summary


def test_compress_events_keeps_observable_actions_only():
    events = [
        {
            "stage": "tool",
            "data": {
                "step": 1,
                "name": "list_tables",
                "arguments": "{}",
                "output": json.dumps({"ok": True, "result": [{"table": "orders"}]}),
            },
        },
        {"stage": "react_step", "data": {"step": 1, "tools": ["list_tables"]}},
        {"stage": "final_candidate", "data": {"step": 2}},
    ]
    compact = _compress_events(events)
    assert [event["stage"] for event in compact] == ["tool", "final_candidate"]
    assert compact[0]["observation"] == "найдено таблиц: 1"


def test_custom_report_parses_role_prefixed_dialogue():
    case = build_custom_case(
        "report",
        "Аналитик: Нужен отчёт по дням.\nФинансы: Только оплаченные заказы.",
    )
    assert case.kind == "report"
    assert [turn.role for turn in case.dialogue] == ["Аналитик", "Финансы"]


def test_custom_sql_requires_read_only_query():
    with pytest.raises(ValueError, match="SELECT/WITH"):
        build_custom_case("sql", "Проверь запрос", "DELETE FROM orders")


def test_free_chat_accepts_natural_history_without_role_prefixes():
    messages = validate_chat_messages(
        [
            {"role": "user", "content": "Какие тарифы есть в базе?"},
            {"role": "assistant", "content": "Сейчас посмотрю."},
            {"role": "user", "content": "А какой из них самый дорогой?"},
        ]
    )
    assert messages[-1]["content"] == "А какой из них самый дорогой?"


def test_free_chat_requires_latest_message_from_user():
    with pytest.raises(ValueError, match="last chat message"):
        validate_chat_messages([{"role": "assistant", "content": "Готово"}])


def test_free_chat_detects_inline_sql_and_database_questions():
    assert _chat_needs_database(
        [{"role": "user", "content": "Проверь SELECT * FROM orders"}]
    )
    assert not _chat_needs_database(
        [{"role": "user", "content": "Сформулируй вывод короче"}]
    )
