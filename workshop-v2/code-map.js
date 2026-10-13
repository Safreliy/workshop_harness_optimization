"use strict";
(() => {
const views = {
agent: {
 title: 'Один эпизод · HarnessAgent.run',
 code: `task = prepare_context(case, config)  # ledger / clip
messages = [system(config), user(task)]
schemas = build_tool_schemas(config)
if case.kind == "report" and config.routing == "task_kind":
    schemas = []

final = ""
for step in range(config.max_steps):
    turn = model.complete(messages, tools=schemas)
    messages.append(turn.as_message())
    if not turn.tool_calls:
        final = turn.content
        break
    for call in turn.tool_calls:
        observation = tools.execute(call.name, call.arguments)
        messages.append(tool_message(call.id, observation))

if not final:
    final = model.complete(messages + [ask_final], tools=None).content
if config.review_mode == "domain_specialist":
    final = review_in_separate_context(case, final, config)
return AgentResult(output=final, events=events)`,
 notes: [['Вызов ≠ исполнение', 'Модель выдаёт имя и JSON-аргументы. ToolExecutor выполняет Python / SQL и возвращает observation.'], ['Лимит ≠ качество', 'При исчерпании шагов — ещё один вызов LLM без tools. Grader запускается после эпизода.']],
 sources: [['agent.py', 'agent', 81], ['ToolExecutor.execute', 'tools', 105], ['run_benchmark → grader', 'runner', 99]],
 bottom: 'Подготовка ledger, план и reviewer — дополнительные LLM-вызовы вокруг этого цикла.'
},
interfaces: {
 title: 'Ручки · проверяемая конфигурация + схемы tools',
 code: `class HarnessConfig(BaseModel):
    workflow: Literal["react", "plan_execute"] = "react"
    context_strategy: Literal["raw", "requirements_ledger"] = "raw"
    review_mode: Literal["none", "domain_specialist"] = "none"
    tool_description_profile: Literal["terse", "diagnostic"] = "terse"
    enabled_tools: list[str]
    prompt_patch: str = Field(default="", max_length=6000)
    max_steps: int = Field(default=6, ge=1, le=12)

def build_tool_schemas(config):
    descriptions = choose_profile(config.tool_description_profile)
    return [{"type": "function", "function": {
        "name": name,
        "description": config.tool_description_overrides.get(
            name, descriptions[name]),
        "parameters": PARAMETERS[name],
    }} for name in config.enabled_tools]

# Схема для LLM и исполняемый код — две части интерфейса.
observation = ToolExecutor(database).execute(name, arguments_json)`,
 notes: [['Config → ветвление кода', 'workflow добавляет этап планирования; review_mode включает отдельный проверяющий вызов.'], ['Config → запрос модели', 'enabled_tools задаёт схемы; description объясняет применение. Реализация инструмента находится отдельно.']],
 sources: [['HarnessConfig · все поля', 'config', 25], ['build_tool_schemas', 'tools', 79], ['Ветвления agent.run', 'agent', 81]],
 bottom: 'Pydantic проверяет типы, диапазоны и известные tools; мета-поиск дополнительно ограничивает изменяемые поля.'
},
proposal: {
 title: 'Мутация · гипотеза → patch → train → диагностика',
 code: `# LLM получает родителей, историю, train-метрики и ошибки.
proposal = {
    "parent_number": 2,
    "hypothesis": "Нужно сохранять последние требования",
    "patch": {"context_strategy": "requirements_ledger"},
}
parent = states[proposal["parent_number"]].config
merged = parent.model_dump()
merged.update(proposal["patch"])
candidate = HarnessConfig.model_validate(merged)
# Проверить 1–4 разрешённых поля, реальное изменение,
# активность механизма и отсутствие дубликата.

result = run_benchmark(config=candidate, cases=train_cases)
state = save_candidate(candidate, result, parent_number)
feedback = {
    "metrics": result.summary,
    "failures": grader_issues_and_tool_events(result),
}
# Feedback войдёт в следующий запрос мета-модели.`,
 notes: [['Мета-агент предлагает; код применяет', 'Объект patch сливается с выбранным родителем. Некорректные, повторные и неактивные изменения отклоняются.'], ['Диагноз опирается на измерения', 'В следующем запросе есть метрики, проверки grader, артефакты и события инструментов. Нет eval-ответов.']],
 sources: [['_model_proposals · применение', 'optimizer', 469], ['_evaluate · train-запуск', 'optimizer', 181], ['_result_feedback · диагностика', 'optimizer', 307]],
 bottom: 'Объект proposal здесь иллюстрирует контракт; это не цитата измеренного кандидата.'
},
search: {
 title: 'Поиск · несколько родителей → следующий раунд → eval',
 code: `beam = [evaluate(baseline, train_cases)]
for round_number in range(1, search.model_rounds + 1):
    proposals = model_proposals(beam, train_history)
    evaluated = []
    for proposal in proposals:
        if not budget_available(): break
        evaluated.append(evaluate(proposal.config, train_cases))
    beam = select_beam(beam + evaluated, width=3)
    save_trace(beam, evaluated)

# Pareto: report ↑, SQL ↑, trace ↑, LLM calls ↓.
# Сначала недоминируемые, затем objective. Если frontier
# меньше width — дополняем лучшими оставшимися.
best = max(all_states, key=lambda s: s.objective)
save(best.config, "best_config.yaml")  # зафиксировать выбор

# Отдельная команда CLI: после optimize(), вне цикла.
baseline_eval = run_benchmark(baseline, cases=eval_cases)
candidate_eval = run_benchmark(best.config, cases=eval_cases)
compare(baseline_eval, candidate_eval)`,
 notes: [['В beam остаются разные компромиссы', 'Родители и новые кандидаты конкурируют вместе. Улучшение одной метрики не гарантирует сохранение кандидата.'], ['Eval после фиксации', 'Текущий optimizer использует только train. CLI eval сравнивает baseline и сохранённый best_config отдельно.']],
 sources: [['optimize · раунды', 'optimizer', 619], ['_select_beam · Pareto + objective', 'optimizer', 579], ['command_eval · отдельный запуск', 'cli', 176]],
 bottom: 'objective = score − 0.006 × max(LLM calls − 1, 0). Повторная подстройка по eval делает его dev-набором; нужен новый test.'
}
};
const code = document.getElementById('implementation-code');
if (!code) return;
const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(key) {
 const v = views[key];
 document.querySelectorAll('[data-code-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.codeView === key)));
 document.getElementById('implementation-title').textContent = v.title;
 code.textContent = v.code;
 document.getElementById('implementation-notes').innerHTML = v.notes.map(([title, text]) => `<article><b>${escape(title)}</b><p>${escape(text)}</p></article>`).join('') + `<div class="implementation-links">${v.sources.map(([label, file, line]) => `<a href="source.html#${file}-L${line}" target="_blank" rel="noopener">${escape(label)} ↗</a>`).join('')}</div>`;
 document.getElementById('implementation-bottom').textContent = v.bottom;
}
document.querySelectorAll('[data-code-view]').forEach(b => b.addEventListener('click', () => render(b.dataset.codeView)));
render('agent');
})();
