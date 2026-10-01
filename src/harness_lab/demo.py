from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .agent import AgentEvent, AgentResult, HarnessAgent
from .auditor import AuditError, AuditStore, OpenRouterAuditor, history_fingerprint
from .cases import BenchmarkCase, DialogueTurn, load_cases, public_case_payload
from .config import HarnessConfig
from .db import PostgresSandbox
from .model import ModelClient
from .runner import BenchmarkRecord, run_benchmark
from .settings import Settings
from .tools import ToolExecutor, build_tool_schemas
from .trajectory import grade_trajectory


CURATED_TASKS: dict[str, dict[str, Any]] = {
    "report_eval_01": {
        "display_title": "Удержание клиентов по месяцам",
        "label": "Хороший ответ, плохая траектория",
        "difficulty": "контекст + стоимость",
        "why": (
            "Стартовый агент способен собрать почти правильное ТЗ, но тратит семь "
            "вызовов и десять раз обращается к БД, хотя данные уже есть в диалоге."
        ),
        "expectations": [
            "учесть последние правки о Europe/London и периоде ожидания 7 дней",
            "не дублировать клиента по каналам продаж",
            "не обращаться к PostgreSQL без необходимости",
            "вернуть итоговый JSON до исчерпания бюджета",
        ],
    },
    "report_eval_03": {
        "display_title": "Недельная активность проектов",
        "label": "Маршрутизация по типу задачи",
        "difficulty": "поздние правки",
        "why": (
            "ТЗ полностью задано в двенадцати репликах. Разница возникает не в модели, "
            "а в том, разрешает ли harness ненужные инструменты для отчётной задачи."
        ),
        "expectations": [
            "последняя зона — Asia/Singapore, а не UTC",
            "группировка — ISO-неделя и устройство, без страны",
            "plan оставить открытым вопросом, а не выдумывать связь",
            "закончить без обращений к БД",
        ],
    },
    "sql_eval_02": {
        "display_title": "Заказы по редкому продукту",
        "label": "Недостающий инструмент",
        "difficulty": "индекс для JOIN",
        "why": (
            "Нужно не только предположить индекс, но и проверить его внутри откатываемой "
            "транзакции. У стартового агента такого инструмента нет."
        ),
        "expectations": [
            "сохранить агрегацию и фильтры paid/non-test",
            "найти последовательное чтение 360 000 строк order_items",
            "проверить индекс по product_id на точном запросе",
            "вернуть измеренные стоимости и отношение улучшения",
        ],
    },
    "sql_eval_03": {
        "display_title": "Редкая campaign внутри JSONB",
        "label": "Выбор типа индекса",
        "difficulty": "GIN + доказательство",
        "why": (
            "Агент должен сохранить оператор JSON containment, подобрать GIN и доказать "
            "эффект планом, а не ограничиться общей рекомендацией."
        ),
        "expectations": [
            "не менять оператор properties @>",
            "предложить GIN-индекс для properties",
            "сравнить планы исходного и точного итогового SQL",
            "зафиксировать, что пробный индекс был откатан",
        ],
    },
    "report_eval_02": {
        "display_title": "CAC и атрибуция маркетинга",
        "label": "Честная граница решения",
        "difficulty": "неполные данные",
        "why": (
            "Оптимизированный harness убирает лишние действия и улучшает общую оценку, "
            "но всё ещё не закрывает все требования. Этот кейс защищает демо от cherry-picking."
        ),
        "expectations": [
            "last non-direct touch за 30 дней",
            "не выдумывать tax_amount и таблицу валютных курсов",
            "CAC считать через NULLIF",
            "вернуть только JSON установленной структуры",
        ],
    },
}


ISSUE_LABELS = {
    "excessive_llm_calls": "слишком много вызовов модели",
    "forced_final_after_step_budget": "ответ сформирован после исчерпания шагов",
    "report_unnecessary_database_tools": "БД-инструменты не нужны для этой задачи",
    "excessive_sample_queries": "слишком много выборочных запросов",
    "missing_schema_inspection": "схема не проверена",
    "missing_baseline_explain": "план исходного запроса не получен",
    "missing_transactional_index_trial": "предложенный индекс не проверен",
    "tool_errors": "есть ошибочные вызовы инструментов",
    "agent_error": "агент завершился с ошибкой",
}


def _records(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            result[str(item["case_id"])] = item
    return result


def _summarize_tool_output(name: str, raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)[:240]
    if not payload.get("ok"):
        return f"ошибка: {payload.get('message', payload.get('error', 'неизвестно'))}"
    value = payload.get("result")
    if name == "list_tables" and isinstance(value, list):
        return f"найдено таблиц: {len(value)}"
    if name == "describe_table" and isinstance(value, dict):
        return (
            f"{value.get('table')}: {len(value.get('columns', []))} столбцов, "
            f"{len(value.get('indexes', []))} индексов, ~{value.get('estimated_rows', '?')} строк"
        )
    if name == "explain_query" and isinstance(value, dict):
        return f"стоимость плана: {value.get('total_cost', '?')}"
    if name == "trial_index_plan" and isinstance(value, dict):
        return (
            f"{value.get('baseline_total_cost', '?')} → {value.get('candidate_total_cost', '?')} · "
            f"{value.get('cost_ratio', '?')}× · индекс откатан"
        )
    if name == "run_readonly_query" and isinstance(value, dict):
        rows = value.get("rows", [])
        columns = ", ".join(str(item) for item in value.get("columns", []))
        sample = json.dumps(rows[:2], ensure_ascii=False)
        return f"{len(rows)} строк · {columns or 'результат'} · {sample[:120]}"
    return json.dumps(value, ensure_ascii=False)[:240]


def _compress_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for event in events:
        stage = str(event.get("stage", ""))
        data = event.get("data") or {}
        if stage == "tool":
            name = str(data.get("name", "tool"))
            arguments = data.get("arguments", "{}")
            try:
                parsed_arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                parsed_arguments = arguments
            compact.append(
                {
                    "stage": "tool",
                    "step": data.get("step"),
                    "name": name,
                    "arguments": parsed_arguments,
                    "observation": _summarize_tool_output(name, str(data.get("output", ""))),
                }
            )
        elif stage in {
            "planning",
            "context_compaction",
            "domain_specialist_review",
            "final_candidate",
            "forced_final",
        }:
            compact.append({"stage": stage, "step": data.get("step"), "data": data})
    return compact


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    trajectory = record.get("trajectory_details") or {}
    issues = [str(item) for item in trajectory.get("issues", [])]
    return {
        "score": record.get("score", 0),
        "passed": bool(record.get("passed")),
        "output_score": record.get("output_score", 0),
        "trajectory_score": record.get("trajectory_score", 0),
        "llm_calls": record.get("llm_calls", 0),
        "input_tokens": record.get("input_tokens", 0),
        "output_tokens": record.get("output_tokens", 0),
        "output": record.get("output", ""),
        "error": record.get("error"),
        "issues": [{"id": issue, "label": ISSUE_LABELS.get(issue, issue)} for issue in issues],
        "tool_calls": trajectory.get("tool_calls", 0),
        "tool_sequence": trajectory.get("tool_sequence", []),
        "forced_final": bool(trajectory.get("forced_final")),
        "grade_details": record.get("grade_details") or {},
        "events": _compress_events(record.get("events") or []),
    }


def build_replay_bundle(
    *,
    benchmark_dir: str | Path,
    comparison_path: str | Path,
    baseline_config_path: str | Path,
    optimized_config_path: str | Path,
) -> dict[str, Any]:
    benchmark_root = Path(benchmark_dir)
    comparison_file = Path(comparison_path)
    comparison = json.loads(comparison_file.read_text(encoding="utf-8"))
    base_dir = Path(comparison["baseline_run"])
    optimized_dir = Path(comparison["candidate_run"])
    baseline = _records(base_dir / "records.jsonl")
    optimized = _records(optimized_dir / "records.jsonl")
    cases = {case.id: case for case in load_cases(benchmark_root, split="eval")}

    tasks: list[dict[str, Any]] = []
    for case_id, meta in CURATED_TASKS.items():
        case = cases[case_id]
        before = _record_payload(baseline[case_id])
        after = _record_payload(optimized[case_id])
        tasks.append(
            {
                "id": case_id,
                "kind": case.kind,
                "title": meta["display_title"],
                "source_title": case.title,
                "label": meta["label"],
                "difficulty": meta["difficulty"],
                "why": meta["why"],
                "expectations": meta["expectations"],
                "input": public_case_payload(case),
                "delta": round(float(after["score"]) - float(before["score"]), 4),
                "agents": {"baseline": before, "optimized": after},
            }
        )

    base_config = HarnessConfig.from_yaml(baseline_config_path)
    optimized_config = HarnessConfig.from_yaml(optimized_config_path)
    return {
        "schema_version": 1,
        "mode": "recorded_eval",
        "model": comparison["candidate"].get("model", "fixed model"),
        "notice": (
            "Это воспроизведение сохранённых eval-прогонов. Для живого запуска откройте "
            "страницу через команду harness-lab demo."
        ),
        "summary": {
            "baseline": comparison["baseline"],
            "optimized": comparison["candidate"],
            "delta": comparison["delta"],
        },
        "agent_configs": {
            "baseline": base_config.model_dump(mode="json"),
            "optimized": optimized_config.model_dump(mode="json"),
        },
        "tasks": tasks,
    }


def write_replay_bundle(args: argparse.Namespace) -> Path:
    payload = build_replay_bundle(
        benchmark_dir=args.benchmark_dir,
        comparison_path=args.comparison,
        baseline_config_path=args.baseline,
        optimized_config_path=args.candidate,
    )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _live_record_payload(record: BenchmarkRecord) -> dict[str, Any]:
    return _record_payload(asdict(record))


def build_custom_case(kind: str, prompt: str, sql: str = "") -> BenchmarkCase:
    clean_prompt = prompt.strip()
    if kind not in {"report", "sql"}:
        raise ValueError("kind must be report or sql")
    if not clean_prompt:
        raise ValueError("task text is required")
    if len(clean_prompt) > 30_000:
        raise ValueError("task text is too long (maximum 30000 characters)")

    if kind == "sql":
        clean_sql = sql.strip()
        if not clean_sql:
            raise ValueError("SQL query is required")
        if len(clean_sql) > 12_000:
            raise ValueError("SQL query is too long (maximum 12000 characters)")
        if not re.match(r"^(select|with)\b", clean_sql, flags=re.IGNORECASE):
            raise ValueError("only a read-only SELECT/WITH query can be analyzed")
        return BenchmarkCase(
            id="custom_sql",
            kind="sql",
            title="Пользовательская диагностика SQL",
            question=clean_prompt,
            sql=clean_sql,
            min_cost_ratio=1.0,
            tags=["custom", "live"],
        )

    role_pattern = re.compile(
        r"^\s*(?:\[?\d+\]?\s*[.)-]?\s*)?([A-Za-zА-Яа-яЁё_ -]{2,40})\s*:\s*(.+)$"
    )
    parsed: list[DialogueTurn] = []
    for raw_line in clean_prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = role_pattern.match(line)
        if match:
            parsed.append(
                DialogueTurn(role=match.group(1).strip(), content=match.group(2).strip())
            )
        elif parsed:
            parsed[-1].content = f"{parsed[-1].content}\n{line}"
    if len(parsed) < 2:
        parsed = [DialogueTurn(role="user", content=clean_prompt)]
    return BenchmarkCase(
        id="custom_report",
        kind="report",
        title="Пользовательское ТЗ на отчёт",
        dialogue=parsed,
        tags=["custom", "live"],
    )


def _custom_result_payload(case: BenchmarkCase, result: Any) -> dict[str, Any]:
    trajectory = grade_trajectory(case, result)
    details = trajectory.details
    issues = [str(item) for item in details.get("issues", [])]
    return {
        "custom": True,
        "score": None,
        "passed": None,
        "output_score": None,
        "trajectory_score": trajectory.score,
        "trajectory_passed": trajectory.passed,
        "llm_calls": result.llm_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "output": result.output,
        "error": result.error,
        "issues": [
            {"id": issue, "label": ISSUE_LABELS.get(issue, issue)} for issue in issues
        ],
        "tool_calls": details.get("tool_calls", 0),
        "tool_sequence": details.get("tool_sequence", []),
        "forced_final": bool(details.get("forced_final")),
        "grade_details": {},
        "events": _compress_events([asdict(event) for event in result.events]),
    }


def validate_chat_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("messages must be a non-empty list")
    clean: list[dict[str, str]] = []
    total_chars = 0
    for item in value[-40:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise ValueError("chat history accepts only user and assistant messages")
        content = str(item.get("content", "")).strip()
        if not content:
            raise ValueError("message content cannot be empty")
        if len(content) > 20_000:
            raise ValueError("one message is too long")
        total_chars += len(content)
        clean.append({"role": str(item["role"]), "content": content})
    if total_chars > 60_000:
        raise ValueError("chat history is too long; clear the chat and try again")
    if clean[-1]["role"] != "user":
        raise ValueError("the last chat message must be from the user")
    return clean


def validate_arena_histories(value: Any) -> dict[str, list[dict[str, str]]]:
    """The two candidates may have different replies, but receive the same user turns."""
    if not isinstance(value, dict) or set(value) != {"baseline", "optimized"}:
        raise ValueError("arena requires histories for both agents")
    histories = {agent: validate_chat_messages(value[agent]) for agent in ("baseline", "optimized")}
    user_turns = [
        [message["content"] for message in histories[agent] if message["role"] == "user"]
        for agent in ("baseline", "optimized")
    ]
    if user_turns[0] != user_turns[1]:
        raise ValueError("arena agents must receive the same user turns")
    return histories


def _chat_needs_database(messages: list[dict[str, str]]) -> bool:
    recent = "\n".join(item["content"] for item in messages[-4:]).lower()
    signals = (
        "select ",
        "with ",
        "sql",
        "postgres",
        "explain",
        "index",
        "индекс",
        "запрос",
        "таблиц",
        "баз",
        "данн",
        "тариф",
        "orders",
        "customers",
        "events",
    )
    return any(signal in recent for signal in signals)


def _chat_system_prompt(config: HarnessConfig, database_mode: bool) -> str:
    if config.routing == "task_kind":
        base = (
            "You are a senior analytics and PostgreSQL assistant in an ordinary ongoing chat. "
            "Infer the user's intent from free-form messages. Do not require a special dialogue "
            "format, separate SQL field, or JSON response. Preserve corrections from earlier "
            "turns. Ask one concise clarification only when it changes the solution materially."
        )
        if database_mode:
            base += (
                " This request may need database evidence. Inspect only what is necessary, avoid "
                "repeated sampling, and distinguish measured facts from suggestions. Never execute "
                "writes; candidate indexes must be tested only through the rollback tool."
            )
        else:
            base += " Database tools are not needed for this turn; answer directly from the conversation."
    else:
        base = (
            "You are a helpful data assistant in an ordinary ongoing chat. Continue from the "
            "conversation history, answer the latest user message naturally, and use PostgreSQL "
            "tools when they seem useful. Do not require any special input format."
        )
    if config.prompt_patch:
        base += f"\n\nAdditional harness instruction:\n{config.prompt_patch}"
    return base


def run_free_chat(
    settings: Settings,
    config: HarnessConfig,
    history: list[dict[str, str]],
) -> tuple[AgentResult, float]:
    started = time.monotonic()
    result = AgentResult(case_id="free_chat", output="")
    database_mode = _chat_needs_database(history)
    schemas = build_tool_schemas(config)
    if config.routing == "task_kind" and not database_mode:
        schemas = []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _chat_system_prompt(config, database_mode)},
        *history,
    ]
    # Three tool rounds plus one forced final stay inside the UI's 90-second cap
    # even when the upstream model uses its full per-call timeout.
    model = ModelClient(settings, timeout=20.0, max_attempts=1)
    tools = ToolExecutor(PostgresSandbox(settings.pg_dsn))
    try:
        for step in range(1, min(config.max_steps, 3) + 1):
            turn = model.complete(messages, tools=schemas, temperature=config.temperature)
            result.add_turn(turn)
            messages.append(turn.as_message())
            if not turn.tool_calls:
                result.output = turn.content
                result.events.append(AgentEvent(stage="final_candidate", data={"step": step}))
                break
            names: list[str] = []
            for call in turn.tool_calls:
                names.append(call.name)
                output = tools.execute(call.name, call.arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": output}
                )
                result.events.append(
                    AgentEvent(
                        stage="tool",
                        data={
                            "step": step,
                            "name": call.name,
                            "arguments": call.arguments,
                            "output": output[:5000],
                        },
                    )
                )
            result.events.append(
                AgentEvent(stage="react_step", data={"step": step, "tools": names})
            )
        if not result.output:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Stop using tools and answer the user's latest message now in natural "
                        "language. State any uncertainty briefly."
                    ),
                }
            )
            turn = model.complete(messages, tools=None, temperature=config.temperature)
            result.add_turn(turn)
            result.output = turn.content
            result.events.append(
                AgentEvent(stage="forced_final", data={"reason": "chat_step_budget"})
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result, round(time.monotonic() - started, 3)


def _chat_result_payload(result: AgentResult, latency_seconds: float) -> dict[str, Any]:
    tool_events = [event for event in result.events if event.stage == "tool"]
    issues: list[dict[str, str]] = []
    if any(event.stage == "forced_final" for event in result.events):
        issues.append(
            {"id": "forced_final_after_step_budget", "label": "достигнут лимит шагов"}
        )
    if result.error:
        issues.append({"id": "agent_error", "label": "агент завершился с ошибкой"})
    return {
        "custom": True,
        "score": None,
        "passed": None,
        "output_score": None,
        "trajectory_score": None,
        "llm_calls": result.llm_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "output": result.output,
        "error": result.error,
        "issues": issues,
        "tool_calls": len(tool_events),
        "forced_final": any(event.stage == "forced_final" for event in result.events),
        "latency_seconds": latency_seconds,
        "grade_details": {},
        "events": _compress_events([asdict(event) for event in result.events]),
    }


class DemoHandler(SimpleHTTPRequestHandler):
    server_version = "HarnessLabDemo/0.1"

    def __init__(
        self,
        *args: Any,
        directory: str,
        settings: Settings,
        cases: dict[str, BenchmarkCase],
        configs: dict[str, HarnessConfig],
        artifact_root: Path,
        audit_store: AuditStore,
        **kwargs: Any,
    ) -> None:
        self.settings = settings
        self.cases = cases
        self.configs = configs
        self.artifact_root = artifact_root
        self.audit_store = audit_store
        super().__init__(*args, directory=directory, **kwargs)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.rstrip("/") == "/api/status":
            self._json(
                {
                    "mode": "live",
                    "model": self.settings.model_name,
                    "task_ids": list(self.cases),
                    "auditor": {
                        "enabled": bool(self.settings.openrouter_api_key),
                        "model": self.settings.auditor_model,
                        "jev_model": self.settings.jev_model,
                    },
                }
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        endpoint = self.path.rstrip("/")
        if endpoint not in {
            "/api/run", "/api/run-custom", "/api/chat",
            "/api/audit/plan", "/api/audit/check", "/api/arena/plan",
        }:
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100_000:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
            if endpoint == "/api/arena/plan":
                histories = validate_arena_histories(body.get("histories"))
                # Commit one rubric using only user turns, before either answer exists.
                user_history = [
                    message for message in histories["baseline"] if message["role"] == "user"
                ]
                criteria = OpenRouterAuditor(self.settings).plan(user_history)
                audit_ids = {
                    agent: self.audit_store.create(agent, histories[agent], criteria)
                    for agent in ("baseline", "optimized")
                }
                self._json({
                    "audit_ids": audit_ids,
                    "criteria": criteria,
                    "model": self.settings.auditor_model,
                    "jev_model": self.settings.jev_model,
                })
                return
            agent_key = str(body.get("agent", ""))
            if agent_key not in self.configs:
                raise ValueError("unknown agent")
            if endpoint == "/api/audit/plan":
                history = validate_chat_messages(body.get("messages"))
                criteria = OpenRouterAuditor(self.settings).plan(history)
                audit_id = self.audit_store.create(agent_key, history, criteria)
                self._json(
                    {
                        "audit_id": audit_id,
                        "criteria": criteria,
                        "model": self.settings.auditor_model,
                        "jev_model": self.settings.jev_model,
                    }
                )
                return
            if endpoint == "/api/audit/check":
                audit_id = str(body.get("audit_id", ""))
                session = self.audit_store.get(audit_id)
                if session.agent != agent_key or session.result is None:
                    raise ValueError("для этой оценки нет завершённого ответа агента")
                if session.evaluation is None:
                    evaluation = OpenRouterAuditor(self.settings).evaluate(
                        session.history,
                        session.criteria,
                        str(session.result["output"]),
                        session.result["events"],
                    )
                    self.audit_store.record_evaluation(audit_id, evaluation)
                else:
                    evaluation = session.evaluation
                self._json({"audit_id": audit_id, "evaluation": evaluation})
                return
            if endpoint == "/api/chat":
                history = validate_chat_messages(body.get("messages"))
                audit_id = str(body.get("audit_id", ""))
                if audit_id:
                    session = self.audit_store.get(audit_id)
                    if session.agent != agent_key or history_fingerprint(session.history) != history_fingerprint(history):
                        raise ValueError("оценка не соответствует запросу или агенту")
                chat_result, latency = run_free_chat(
                    self.settings, self.configs[agent_key], history
                )
                payload = {
                    "mode": "live",
                    "model": self.settings.model_name,
                    "agent": agent_key,
                    "result": _chat_result_payload(chat_result, latency),
                }
                if chat_result.error:
                    payload.update(error="chat_failed", message=chat_result.error)
                    self._json(payload, HTTPStatus.BAD_GATEWAY)
                else:
                    if audit_id:
                        self.audit_store.record_result(
                            audit_id, agent_key, history,
                            {
                                "output": chat_result.output,
                                "events": [asdict(event) for event in chat_result.events],
                            },
                        )
                    self._json(payload)
                return
            if endpoint == "/api/run-custom":
                case = build_custom_case(
                    str(body.get("kind", "")),
                    str(body.get("prompt", "")),
                    str(body.get("sql", "")),
                )
                agent = HarnessAgent(
                    ModelClient(self.settings),
                    ToolExecutor(PostgresSandbox(self.settings.pg_dsn)),
                )
                agent_result = agent.run(case, self.configs[agent_key])
                self._json(
                    {
                        "mode": "live",
                        "model": self.settings.model_name,
                        "case_id": case.id,
                        "agent": agent_key,
                        "result": _custom_result_payload(case, agent_result),
                    }
                )
                return

            case_id = str(body.get("case_id", ""))
            if case_id not in self.cases:
                raise ValueError("unknown case")
            result = run_benchmark(
                settings=self.settings,
                config=self.configs[agent_key],
                cases=[self.cases[case_id]],
                artifact_root=self.artifact_root,
                run_label=f"demo-{agent_key}-{case_id}",
            )
            self._json(
                {
                    "mode": "live",
                    "model": self.settings.model_name,
                    "case_id": case_id,
                    "agent": agent_key,
                    "result": _live_record_payload(result.records[0]),
                }
            )
        except AuditError as exc:
            self._json(
                {"error": "audit_failed", "message": str(exc)},
                HTTPStatus.BAD_GATEWAY,
            )
        except Exception as exc:
            self._json(
                {"error": type(exc).__name__, "message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )


def serve_demo(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    missing = settings.validate_model()
    if missing:
        raise ValueError(f"missing model settings: {', '.join(missing)}")
    all_cases = {case.id: case for case in load_cases(args.benchmark_dir, split="eval")}
    cases = {case_id: all_cases[case_id] for case_id in CURATED_TASKS}
    configs = {
        "baseline": HarnessConfig.from_yaml(args.baseline),
        "optimized": HarnessConfig.from_yaml(args.candidate),
    }
    static_dir = Path(args.static_dir).resolve()
    if not (static_dir / "demo.html").exists():
        raise FileNotFoundError(f"demo.html not found in {static_dir}")
    factory = partial(
        DemoHandler,
        directory=str(static_dir),
        settings=settings,
        cases=cases,
        configs=configs,
        artifact_root=Path(args.artifact_root),
        audit_store=AuditStore(),
    )
    server = ThreadingHTTPServer((args.host, args.port), factory)
    print(f"Harness Lab demo: http://{args.host}:{args.port}/demo.html")
    print("Use Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
