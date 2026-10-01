const STORAGE_KEY = 'harness-lab-chat-v2';
const ARENA_STORAGE_KEY = 'harness-lab-arena-v1';
const REQUEST_TIMEOUT_MS = 90_000;
const AUDIT_TIMEOUT_MS = 45_000;
const markdown = window.markdownit?.({ html: false, linkify: true, breaks: true });

const state = {
  data: null,
  selectedTask: 0,
  agent: 'baseline',
  runtime: 'replay',
  running: false,
  shown: false,
  customMode: false,
  arenaMode: false,
  arena: loadArena(),
  liveResults: new Map(),
  histories: loadHistories(),
  lastChatResults: { baseline: null, optimized: null },
  currentAudits: { baseline: null, optimized: null },
  auditor: { enabled: false, model: '', jev_model: '' },
  chatPhase: '',
  stopRequested: false,
  chatError: '',
  controller: null,
  elapsedTimer: null,
  startedAt: 0,
};

const $ = (selector) => document.querySelector(selector);
const taskList = $('#task-list');
const runtimeBadge = $('#runtime-badge');
const runButton = $('#run-button');
const swapButton = $('#swap-button');
const answerWrap = $('#answer-wrap');
const answer = $('#assistant-answer');
const answerToggle = $('#answer-toggle');
const placeholder = $('#assistant-placeholder');
const traceList = $('#trace-list');
const traceEmpty = $('#trace-empty');
const resultCard = $('#result-card');
const customTaskButton = $('#custom-task-button');
const customComposer = $('#custom-composer');
const chatHistory = $('#chat-history');
const chatInput = $('#chat-input');
const chatForm = $('#chat-form');
const chatState = $('#chat-state');
const clearChatButton = $('#clear-chat');
const sendChatButton = $('#send-chat');
const arenaButton = $('#arena-button');
const arenaInput = $('#arena-input');

const number = (value, digits = 3) => Number(value || 0).toFixed(digits).replace('.', ',');
const signed = (value) => `${Number(value) >= 0 ? '+' : ''}${number(value)}`;
const activeTask = () => state.data.tasks[state.selectedTask];
const resultKey = () => `${activeTask().id}:${state.agent}`;
const activeResult = () => state.liveResults.get(resultKey()) || activeTask().agents[state.agent];
const activeHistory = () => state.histories[state.agent];

function loadArena() {
  const fresh = () => ({ histories: { baseline: [], optimized: [] }, criteria: [], evaluations: {}, results: {}, statuses: {}, error: '', controllers: new Set(), stopRequested: false });
  const arena = fresh();
  try {
    const saved = JSON.parse(localStorage.getItem(ARENA_STORAGE_KEY) || '{}');
    for (const agent of ['baseline', 'optimized']) {
      arena.histories[agent] = Array.isArray(saved.histories?.[agent])
        ? saved.histories[agent].filter((item) => item && ['user', 'assistant'].includes(item.role) && typeof item.content === 'string').slice(-24)
          .map(({ role, content, audit }) => ({ role, content, ...(audit && role === 'assistant' ? { audit } : {}) }))
        : [];
    }
    if (Array.isArray(saved.criteria)) arena.criteria = saved.criteria;
    if (saved.evaluations && typeof saved.evaluations === 'object') arena.evaluations = saved.evaluations;
  } catch (_) { /* unavailable or old local history */ }
  return arena;
}

function persistArena() {
  try {
    localStorage.setItem(ARENA_STORAGE_KEY, JSON.stringify({
      histories: state.arena.histories, criteria: state.arena.criteria, evaluations: state.arena.evaluations,
    }));
  } catch (_) { /* private browser modes may disable localStorage */ }
}

function trimArenaHistories() {
  for (const agent of ['baseline', 'optimized']) {
    const history = state.arena.histories[agent];
    const userIndexes = history.flatMap((message, index) => message.role === 'user' ? [index] : []);
    if (userIndexes.length > 8) state.arena.histories[agent] = history.slice(userIndexes.at(-8));
  }
}

function loadHistories() {
  const empty = { baseline: [], optimized: [] };
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    for (const agent of ['baseline', 'optimized']) {
      if (!Array.isArray(saved[agent])) continue;
      empty[agent] = saved[agent]
        .filter((item) => item && ['user', 'assistant'].includes(item.role) && typeof item.content === 'string')
        .slice(-40)
        .map(({ role, content, audit }) => ({ role, content, ...(role === 'assistant' && audit ? { audit } : {}) }));
    }
  } catch (_) { /* start with an empty local history */ }
  return empty;
}

function persistHistories() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state.histories)); }
  catch (_) { /* private browser modes may disable localStorage */ }
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 4800);
}

function setAgent(agent, { preserveShown = false } = {}) {
  if (state.running) return;
  state.agent = agent;
  state.chatError = '';
  if (!preserveShown) state.shown = false;
  document.querySelectorAll('.agent-button').forEach((button) => {
    const selected = button.dataset.agent === agent;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-selected', String(selected));
  });
  $('#assistant-name').textContent = agent === 'baseline' ? 'СТАРТОВЫЙ REACT' : 'ОПТИМИЗИРОВАННЫЙ HARNESS';
  $('#assistant-message').classList.toggle('optimized', agent === 'optimized');
  swapButton.textContent = agent === 'baseline'
    ? 'Сравнить с оптимизированным →'
    : '← Сравнить со стартовым';
  if (state.arenaMode) renderArena();
  else if (state.customMode) renderFreeChat();
  else renderResult();
}

function renderTaskList() {
  taskList.replaceChildren();
  state.data.tasks.forEach((task, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    const selected = !state.customMode && !state.arenaMode && index === state.selectedTask;
    button.className = `task-button${selected ? ' active' : ''}`;
    button.setAttribute('aria-pressed', String(selected));
    const order = document.createElement('b');
    order.textContent = String(index + 1).padStart(2, '0');
    const title = document.createElement('span');
    title.textContent = task.title;
    const label = document.createElement('small');
    label.textContent = task.label;
    const delta = document.createElement('em');
    delta.textContent = signed(task.delta);
    button.append(order, title, label, delta);
    button.addEventListener('click', () => {
      if (state.running) return;
      state.selectedTask = index;
      state.customMode = false;
      state.arenaMode = false;
      document.body.classList.remove('arena-mode');
      $('#arena-screen').hidden = true;
      state.shown = false;
      renderTaskList();
      renderCase();
      renderResult();
    });
    taskList.append(button);
  });
  customTaskButton.classList.toggle('active', state.customMode && !state.arenaMode);
  arenaButton.classList.toggle('active', state.arenaMode);
}

function renderInput(task) {
  const root = $('#input-body');
  root.replaceChildren();
  if (task.kind === 'report') {
    task.input.dialogue.forEach((turn) => {
      const row = document.createElement('div');
      row.className = 'dialogue-line';
      const role = document.createElement('b');
      role.textContent = turn.role;
      const copy = document.createElement('span');
      copy.textContent = turn.content;
      row.append(role, copy);
      root.append(row);
    });
    return;
  }
  const question = document.createElement('p');
  question.textContent = task.input.question;
  const sql = document.createElement('pre');
  sql.className = 'sql-input';
  sql.textContent = task.input.sql;
  root.append(question, sql);
}

function renderCase() {
  if (state.arenaMode) return;
  if (state.customMode) {
    renderFreeChat();
    return;
  }
  const task = activeTask();
  customComposer.hidden = true;
  $('#user-message').hidden = false;
  $('#run-row').hidden = false;
  $('#assistant-message').hidden = false;
  $('#comparison-strip').hidden = false;
  $('.case-delta').hidden = false;
  $('#case-kicker').textContent = `${task.kind === 'report' ? 'ОТЧЁТ ПО ДИАЛОГУ' : 'ДИАГНОСТИКА SQL'} · ${task.difficulty}`;
  $('#case-title').textContent = task.title;
  $('#case-delta').textContent = signed(task.delta);
  $('#case-why').textContent = task.why;
  renderInput(task);
  const list = $('#expectation-list');
  list.replaceChildren(...task.expectations.map((item) => {
    const li = document.createElement('li');
    li.textContent = item;
    return li;
  }));
  const before = task.agents.baseline;
  const after = task.agents.optimized;
  $('#compare-score').textContent = `${number(before.score)} → ${number(after.score)}`;
  $('#compare-trace').textContent = `${number(before.trajectory_score)} → ${number(after.trajectory_score)}`;
  $('#compare-calls').textContent = `${before.llm_calls} → ${after.llm_calls}`;
  $('#compare-score-bar').style.setProperty('--width', `${Math.max(3, after.score * 100)}%`);
  $('#compare-trace-bar').style.setProperty('--width', `${Math.max(3, after.trajectory_score * 100)}%`);
  const callRatio = before.llm_calls ? (after.llm_calls / before.llm_calls) * 100 : 0;
  $('#compare-calls-bar').style.setProperty('--width', `${Math.max(3, Math.min(100, callRatio))}%`);
}

function prettyOutput(raw) {
  const text = String(raw || '').trim();
  const first = text.indexOf('{');
  const last = text.lastIndexOf('}');
  if (first >= 0 && last > first) {
    try { return JSON.stringify(JSON.parse(text.slice(first, last + 1)), null, 2); }
    catch (_) { /* preserve the exact model output */ }
  }
  return text || '[агент не вернул ответ]';
}

function graderSummary(task, result) {
  const grade = result.grade_details || {};
  if (task.kind === 'report') {
    const checks = grade.checks || [];
    const passed = checks.filter((item) => item.ok).length;
    const failed = checks.filter((item) => !item.ok).map((item) => item.target);
    const suffix = failed.length ? ` Не прошли: ${[...new Set(failed)].join(', ')}.` : ' Все условия выполнены.';
    return `${passed} из ${checks.length} требований${suffix}`;
  }
  if (grade.structure_score === 0 || result.output_score === 0) {
    return 'Нет проверяемого итогового JSON: семантика и стоимость решения не могут быть подтверждены.';
  }
  const safety = grade.safe ? 'безопасен' : 'небезопасен';
  const equivalence = grade.equivalent ? 'результат совпадает' : 'результат изменился';
  const ratio = grade.cost_ratio ? `стоимость ниже в ${number(grade.cost_ratio)}×` : 'улучшение плана не доказано';
  return `SQL ${safety}; ${equivalence}; ${ratio}.`;
}

function traceNode(event, index) {
  const item = document.createElement('li');
  item.className = 'trace-item';
  item.style.setProperty('--i', index);
  const meta = document.createElement('span');
  const title = document.createElement('strong');
  const detail = document.createElement('p');
  if (event.stage === 'tool') {
    meta.textContent = `шаг ${event.step || index + 1} · tool`;
    title.textContent = event.name || 'tool';
    detail.textContent = event.observation || 'инструмент выполнен';
    if (String(detail.textContent).startsWith('ошибка:')) item.classList.add('error');
  } else if (event.stage === 'forced_final') {
    item.classList.add('forced');
    meta.textContent = 'бюджет исчерпан';
    title.textContent = 'forced_final';
    detail.textContent = 'Harness потребовал немедленно вернуть итоговый ответ.';
  } else if (event.stage === 'final_candidate') {
    item.classList.add('final');
    meta.textContent = `шаг ${event.step || index + 1}`;
    title.textContent = 'final_answer';
    detail.textContent = 'Агент завершил задачу без принудительной финализации.';
  } else {
    item.classList.add('final');
    meta.textContent = 'этап harness';
    title.textContent = event.stage || 'event';
    detail.textContent = event.detail || 'Отдельный этап обработки контекста или проверки.';
  }
  item.append(meta, title, detail);
  return item;
}

function renderTrace(result) {
  const events = result?.events || [];
  traceList.replaceChildren(...events.map(traceNode));
  traceEmpty.hidden = events.length > 0;
  $('#trace-count').textContent = `${events.length} ${events.length === 1 ? 'действие' : 'действий'}`;
}

function resetMetrics(note = 'Результат появится после запуска.') {
  resultCard.classList.remove('pass', 'fail', 'uncertain');
  $('#result-verdict').textContent = '—';
  $('#output-score').textContent = '—';
  $('#trajectory-score').textContent = '—';
  $('#llm-calls').textContent = '—';
  $('#grader-note').textContent = note;
  traceList.replaceChildren();
  traceEmpty.hidden = false;
  $('#trace-count').textContent = '0 действий';
}

function resetBenchmarkResult() {
  answerWrap.hidden = true;
  placeholder.hidden = false;
  placeholder.classList.remove('running');
  placeholder.textContent = state.runtime === 'live'
    ? 'Нажмите «Запустить агента»: запрос уйдёт в локальную модель и PostgreSQL.'
    : 'Нажмите «Воспроизвести прогон»: покажем сохранённый ответ и реальные tool calls.';
  resetMetrics();
  $('#assistant-status').textContent = 'готов';
}

function renderResult() {
  if (!state.data || state.customMode || state.arenaMode) return;
  $('#output-label').textContent = 'ответ';
  $('#trajectory-label').textContent = 'траектория';
  runButton.disabled = state.running;
  swapButton.disabled = state.running;
  runButton.querySelector('b').textContent = state.runtime === 'live' ? 'Запустить агента' : 'Воспроизвести прогон';
  runButton.classList.toggle('running', state.running);
  if (state.running) {
    answerWrap.hidden = true;
    placeholder.hidden = false;
    placeholder.classList.add('running');
    placeholder.textContent = 'Модель исследует задачу. Запрос можно остановить; максимальное ожидание — 90 секунд.';
    $('#assistant-status').textContent = 'выполняется';
    return;
  }
  if (!state.shown) {
    resetBenchmarkResult();
    return;
  }
  const result = activeResult();
  placeholder.classList.remove('running');
  placeholder.hidden = true;
  answerWrap.hidden = false;
  answer.textContent = prettyOutput(result.output);
  answer.classList.remove('expanded');
  answerToggle.textContent = 'Показать ответ целиком';
  $('#assistant-status').textContent = result.error ? 'ошибка запуска' : 'завершён';
  resultCard.classList.remove('pass', 'fail', 'uncertain');
  resultCard.classList.add(result.passed ? 'pass' : 'fail');
  $('#result-verdict').textContent = result.passed ? 'ПРОШЁЛ' : 'НЕ ПРОШЁЛ';
  $('#output-score').textContent = number(result.output_score);
  $('#trajectory-score').textContent = number(result.trajectory_score);
  $('#llm-calls').textContent = String(result.llm_calls);
  const issues = (result.issues || []).map((item) => item.label).join('; ');
  const summary = graderSummary(activeTask(), result);
  $('#grader-note').textContent = `${summary}${issues ? ` Траектория: ${issues}.` : ''}`;
  renderTrace(result);
}

function chatBubble(role, content, extraClass = '', audit = null, agent = state.agent) {
  const node = document.createElement('div');
  node.className = `chat-bubble ${role}${extraClass ? ` ${extraClass}` : ''}`;
  const label = document.createElement('small');
  label.textContent = role === 'user' ? 'Вы' : agent === 'baseline' ? 'Стартовый ReAct' : 'Оптимизированный harness';
  const body = document.createElement(!extraClass ? 'div' : 'p');
  if (!extraClass && markdown && window.DOMPurify) {
    body.className = 'chat-markdown';
    body.innerHTML = window.DOMPurify.sanitize(markdown.render(content));
    body.querySelectorAll('a[href]').forEach((link) => {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    });
  } else {
    body.textContent = content;
  }
  node.append(label, body);
  if (audit?.evaluation) {
    const tag = document.createElement('small');
    tag.className = 'chat-audit-tag';
    tag.textContent = `Аудитор: ${audit.evaluation.passed}/${audit.evaluation.total} подтверждено`;
    node.append(tag);
  }
  return node;
}

function renderChatHistory() {
  chatHistory.replaceChildren();
  const history = activeHistory();
  if (!history.length && !state.running && !state.chatError) {
    const empty = document.createElement('div');
    empty.className = 'chat-empty';
    const title = document.createElement('strong');
    title.textContent = 'Обычный диалог без формата';
    const copy = document.createElement('span');
    copy.textContent = 'Напишите вопрос, вставьте SQL прямо в сообщение или продолжите ответ уточнением.';
    empty.append(title, copy);
    chatHistory.append(empty);
  } else {
    history.forEach((message) => chatHistory.append(chatBubble(message.role, message.content, '', message.audit)));
    if (state.running && history.at(-1)?.role !== 'assistant') chatHistory.append(chatBubble('assistant', state.chatPhase === 'plan' ? 'Аудитор формирует критерии…' : 'Готовлю ответ…', 'pending'));
    if (state.chatError) chatHistory.append(chatBubble('assistant', state.chatError, 'error'));
  }
  requestAnimationFrame(() => { chatHistory.scrollTop = chatHistory.scrollHeight; });
}

function renderChatMetrics() {
  const result = state.lastChatResults[state.agent];
  const audit = state.currentAudits[state.agent] || [...activeHistory()].reverse().find((item) => item.audit)?.audit;
  $('#output-label').textContent = 'аудитор';
  $('#trajectory-label').textContent = 'инструменты';
  if (!result) {
    if (audit?.evaluation) {
      resetMetrics(`${audit.evaluation.summary} Оценка сохранена в истории браузера; траектория доступна только в текущей сессии.`);
      resultCard.classList.add(audit.evaluation.failed ? 'fail' : audit.evaluation.uncertain ? 'uncertain' : 'pass');
      $('#result-verdict').textContent = audit.evaluation.failed ? 'ЕСТЬ ПРОВАЛЫ' : audit.evaluation.uncertain ? 'НЕОДНОЗНАЧНО' : 'ПОДТВЕРЖДЕНО';
      $('#output-score').textContent = `${audit.evaluation.passed}/${audit.evaluation.total}`;
      return;
    }
    resetMetrics(state.auditor.enabled
      ? 'Для каждого запроса внешний аудитор сначала формирует проверяемые критерии, затем независимо оценивает ответ.'
      : 'Внешний аудит выключен: добавьте OPENROUTER_API_KEY в .env и перезапустите сервер. Чат работает без него.');
    return;
  }
  resultCard.classList.remove('pass', 'fail', 'uncertain');
  if (audit?.evaluation) resultCard.classList.add(audit.evaluation.failed ? 'fail' : audit.evaluation.uncertain ? 'uncertain' : 'pass');
  else if (result.error) resultCard.classList.add('fail');
  $('#result-verdict').textContent = audit?.evaluation
    ? audit.evaluation.failed ? 'ЕСТЬ ПРОВАЛЫ' : audit.evaluation.uncertain ? 'НЕОДНОЗНАЧНО' : 'ПОДТВЕРЖДЕНО'
    : state.chatPhase === 'check' ? 'АУДИТОР ПРОВЕРЯЕТ' : result.error ? 'ОШИБКА' : 'ОТВЕТ ПОЛУЧЕН';
  $('#output-score').textContent = audit?.evaluation ? `${audit.evaluation.passed}/${audit.evaluation.total}` : '—';
  const toolCalls = (result.events || []).filter((event) => event.stage === 'tool').length;
  $('#trajectory-score').textContent = String(toolCalls);
  $('#llm-calls').textContent = String(result.llm_calls ?? '—');
  const latency = Number(result.latency_seconds || 0).toFixed(1).replace('.', ',');
  $('#grader-note').textContent = audit?.evaluation
    ? `${audit.evaluation.summary} Внешняя модель: ${state.auditor.model}; Jev: ${state.auditor.jev_model}.${audit.evaluation.errors?.length ? ` Сбой проверки: ${audit.evaluation.errors.join('; ')}.` : ''} Это модельная оценка, не эталон. Ответ: ${latency} с.`
    : audit?.error ? `Ответ получен, но внешний аудит не завершён: ${audit.error}`
      : state.chatPhase === 'check' ? 'Проверяем ответ по сформированным до запуска критериям…'
        : `Показана фактическая траектория; ответ: ${latency} с.`;
  renderTrace(result);
}

function renderAuditCriteria() {
  const list = $('#expectation-list');
  const audit = state.currentAudits[state.agent] || [...activeHistory()].reverse().find((item) => item.audit)?.audit;
  list.replaceChildren();
  if (!audit?.criteria?.length) {
    const li = document.createElement('li');
    li.textContent = audit?.error ? `Аудит недоступен: ${audit.error}` : state.auditor.enabled
      ? 'Критерии появятся после отправки запроса — до запуска агента.'
      : 'Внешний аудитор недоступен без OPENROUTER_API_KEY.';
    list.append(li);
    return;
  }
  for (const criterion of audit.criteria) {
    const verdict = audit.evaluation?.criteria?.find((item) => item.id === criterion.id);
    const li = document.createElement('li');
    if (verdict) li.className = verdict.verdict;
    const title = document.createElement('strong');
    title.textContent = `${criterion.id}. ${criterion.expectation}`;
    const detail = document.createElement('small');
    detail.textContent = `Проверка: ${criterion.evidence}`;
    li.append(title, detail);
    if (verdict) {
      const outcome = document.createElement('em');
      const modelVerdict = { pass: 'да', fail: 'нет', unclear: 'неясно', unavailable: 'нет ответа' }[verdict.model_verdict] || 'нет ответа';
      outcome.textContent = `${verdict.verdict === 'pass' ? 'Подтверждено' : verdict.verdict === 'fail' ? 'Не выполнено' : 'Неоднозначно'} · модель: ${modelVerdict} · Jev: ${verdict.jev_probability == null ? 'нет ответа' : `${Math.round(verdict.jev_probability * 100)}%`}`;
      li.append(outcome);
      const evidence = document.createElement('small');
      evidence.textContent = verdict.reason || (verdict.verdict === 'uncertain' ? 'Проверяющие не пришли к согласованному выводу.' : '');
      li.append(evidence);
    }
    list.append(li);
  }
}

function updateChatControls() {
  const live = state.runtime === 'live';
  chatInput.disabled = !live || state.running;
  clearChatButton.disabled = state.running || activeHistory().length === 0;
  sendChatButton.disabled = !live || (!state.running && !chatInput.value.trim());
  sendChatButton.textContent = state.running ? 'Остановить' : 'Отправить ↗';
  if (!live) {
    chatState.textContent = 'Живой чат доступен в локальной версии: публичный сайт не получает API-ключ.';
  } else if (!state.running) {
    chatState.textContent = 'Enter — отправить · Shift+Enter — новая строка';
  }
}

function renderFreeChat() {
  customComposer.hidden = false;
  $('#user-message').hidden = true;
  $('#run-row').hidden = true;
  $('#assistant-message').hidden = true;
  $('#comparison-strip').hidden = true;
  $('.case-delta').hidden = true;
  $('#case-kicker').textContent = 'СВОБОДНЫЙ ЧАТ · ЖИВОЙ ЗАПУСК';
  $('#case-title').textContent = state.agent === 'baseline' ? 'Диалог со стартовым ReAct' : 'Диалог с оптимизированным harness';
  $('#case-why').textContent = state.runtime === 'live'
    ? `Формата нет: пишите обычным текстом, вставляйте SQL в сообщение и задавайте уточняющие вопросы. История сохраняется отдельно для каждого агента в этом браузере.${state.auditor.enabled ? ' При включённом аудите запрос, ответ и наблюдения инструментов передаются OpenRouter.' : ''}`
    : 'Публичная версия показывает eval-прогоны, но не получает секретный ключ модели. Для живого чата откройте локальный demo-сервер.';
  renderAuditCriteria();
  renderChatHistory();
  renderChatMetrics();
  updateChatControls();
}

function timedFetch(url, options, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  state.controller = controller;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal })
    .finally(() => {
      clearTimeout(timeout);
      if (state.controller === controller) state.controller = null;
    });
}

async function runCurrent() {
  if (state.customMode || state.arenaMode) return;
  if (state.running) {
    state.controller?.abort();
    return;
  }
  state.running = true;
  state.shown = false;
  renderResult();
  try {
    if (state.runtime === 'live') {
      const response = await timedFetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ case_id: activeTask().id, agent: state.agent }),
      });
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.message || payload.error || 'run failed');
      state.liveResults.set(resultKey(), payload.result);
    } else {
      await new Promise((resolve) => setTimeout(resolve, matchMedia('(prefers-reduced-motion: reduce)').matches ? 20 : 720));
    }
    state.shown = true;
  } catch (error) {
    const aborted = error.name === 'AbortError';
    toast(aborted ? 'Запрос остановлен или превысил 90 секунд.' : `Запуск не завершён: ${error.message}. Показываю сохранённый eval-прогон.`);
    state.runtime = 'replay';
    runtimeBadge.classList.remove('live');
    runtimeBadge.querySelector('span').textContent = 'воспроизведение eval';
    state.shown = true;
  } finally {
    state.running = false;
    renderResult();
  }
}

function startElapsedTimer(label, limitSeconds) {
  clearInterval(state.elapsedTimer);
  state.startedAt = Date.now();
  const update = () => {
    if (!state.running) return;
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    chatState.textContent = `${label} · ${seconds} с · максимум ${limitSeconds} с`;
  };
  update();
  state.elapsedTimer = setInterval(update, 1000);
}

async function sendChat() {
  if (state.running) {
    state.stopRequested = true;
    state.controller?.abort();
    return;
  }
  const content = chatInput.value.trim();
  if (!content || state.runtime !== 'live') return;
  const agentAtStart = state.agent;
  state.chatError = '';
  state.currentAudits[agentAtStart] = { criteria: [] };
  state.lastChatResults[agentAtStart] = null;
  state.histories[agentAtStart].push({ role: 'user', content });
  state.histories[agentAtStart] = state.histories[agentAtStart].slice(-40);
  const requestHistory = state.histories[agentAtStart].map(({ role, content }) => ({ role, content }));
  persistHistories();
  chatInput.value = '';
  state.running = true;
  state.stopRequested = false;
  state.chatPhase = state.auditor.enabled ? 'plan' : 'answer';
  startElapsedTimer(state.auditor.enabled ? 'Аудитор формирует критерии' : 'Ждём ответ', state.auditor.enabled ? 45 : 90);
  renderFreeChat();
  try {
    let auditId = null;
    if (state.auditor.enabled) {
      try {
        const planResponse = await timedFetch('/api/audit/plan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent: agentAtStart, messages: requestHistory }),
        }, AUDIT_TIMEOUT_MS);
        const plan = await planResponse.json();
        if (!planResponse.ok || plan.error) throw new Error(plan.message || plan.error || 'audit plan failed');
        auditId = plan.audit_id;
        state.currentAudits[agentAtStart] = { criteria: plan.criteria };
        renderFreeChat();
      } catch (error) {
        if (state.stopRequested) throw error;
        state.currentAudits[agentAtStart] = {
          criteria: [],
          error: error.name === 'AbortError' ? 'формирование критериев превысило 45 секунд' : error.message,
        };
        renderFreeChat();
      }
    }
    if (state.stopRequested) throw new DOMException('Запрос остановлен', 'AbortError');
    state.chatPhase = 'answer';
    startElapsedTimer('Ждём ответ агента', 90);
    renderFreeChat();
    const response = await timedFetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: agentAtStart, messages: requestHistory, ...(auditId ? { audit_id: auditId } : {}) }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.message || payload.error || 'chat failed');
    if (state.stopRequested) throw new DOMException('Запрос остановлен', 'AbortError');
    const reply = { role: 'assistant', content: payload.result.output };
    state.histories[agentAtStart].push(reply);
    state.histories[agentAtStart] = state.histories[agentAtStart].slice(-40);
    state.lastChatResults[agentAtStart] = payload.result;
    persistHistories();
    renderFreeChat();
    if (auditId) {
      state.chatPhase = 'check';
      startElapsedTimer('Аудитор проверяет ответ', 45);
      renderFreeChat();
      try {
        const checkResponse = await timedFetch('/api/audit/check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ audit_id: auditId, agent: agentAtStart }),
        }, AUDIT_TIMEOUT_MS);
        const checked = await checkResponse.json();
        if (!checkResponse.ok || checked.error) throw new Error(checked.message || checked.error || 'audit check failed');
        state.currentAudits[agentAtStart].evaluation = checked.evaluation;
        reply.audit = state.currentAudits[agentAtStart];
        persistHistories();
      } catch (error) {
        state.currentAudits[agentAtStart].error = error.name === 'AbortError'
          ? 'проверка остановлена или превысила 45 секунд'
          : error.message;
      }
    }
  } catch (error) {
    const aborted = error.name === 'AbortError';
    state.chatError = aborted
      ? 'Запрос остановлен. Если вы его не останавливали, модель не ответила за 90 секунд.'
      : `Не удалось получить ответ: ${error.message}`;
  } finally {
    clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
    state.running = false;
    state.chatPhase = '';
    renderFreeChat();
    if (!state.chatError) chatInput.focus();
  }
}

function renderArenaCriterion(criterion) {
  const row = document.createElement('div');
  row.className = 'arena-criterion';
  const description = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = `${criterion.id}. ${criterion.expectation}`;
  const evidence = document.createElement('small');
  evidence.textContent = `Признак: ${criterion.evidence}`;
  description.append(title, evidence);
  row.append(description);
  for (const agent of ['baseline', 'optimized']) {
    const cell = document.createElement('div');
    const verdict = state.arena.evaluations[agent]?.criteria?.find((item) => item.id === criterion.id);
    cell.className = `arena-verdict${verdict ? ` ${verdict.verdict}` : ''}`;
    const label = document.createElement('b');
    label.textContent = verdict
      ? { pass: 'Подтверждено', fail: 'Не выполнено', uncertain: 'Неоднозначно' }[verdict.verdict] || 'Неоднозначно'
      : /^(Ошибка|Остановлен)/.test(state.arena.statuses[agent] || '') ? 'Нет оценки' : 'Ожидаем';
    const signals = document.createElement('span');
    signals.textContent = verdict
      ? `модель: ${{ pass: 'да', fail: 'нет', unclear: 'неясно', unavailable: 'нет ответа' }[verdict.model_verdict] || 'нет ответа'} · Jev: ${verdict.jev_probability == null ? 'нет ответа' : `${Math.round(verdict.jev_probability * 100)}%`}`
      : agent === 'baseline' ? 'Стартовый' : 'Оптимизированный';
    cell.append(label, signals);
    if (verdict?.reason) {
      const reason = document.createElement('span');
      reason.textContent = verdict.reason;
      cell.append(reason);
    }
    row.append(cell);
  }
  return row;
}

function renderArena() {
  if (!state.arenaMode) return;
  $('#arena-screen').hidden = false;
  const arena = state.arena;
  const rubric = $('#arena-criteria');
  rubric.replaceChildren();
  if (arena.criteria.length) {
    const header = document.createElement('div');
    header.className = 'arena-criterion arena-criterion-head';
    for (const label of ['Критерий', 'Стартовый ReAct', 'Оптимизированный']) {
      const cell = document.createElement('strong');
      cell.textContent = label;
      header.append(cell);
    }
    rubric.replaceChildren(header, ...arena.criteria.map(renderArenaCriterion));
    $('#arena-rubric-state').textContent = 'Один набор · сформирован до ответов';
  } else {
    const note = document.createElement('p');
    note.textContent = arena.error
      ? `Аудит недоступен: ${arena.error}. Ответы сравниваются без оценки.`
      : state.auditor.enabled
        ? state.running ? 'Аудитор формирует единые критерии до запуска обоих агентов…' : 'Критерии появятся после отправки общего запроса.'
        : 'Внешний аудитор выключен. Добавьте OPENROUTER_API_KEY; ответы сравниваются без оценки.';
    rubric.append(note);
    $('#arena-rubric-state').textContent = state.auditor.enabled ? 'Пока нет критериев' : 'Без внешней оценки';
  }
  for (const agent of ['baseline', 'optimized']) {
    const history = $(`#arena-${agent}-history`);
    history.replaceChildren();
    if (!arena.histories[agent].length) {
      const empty = document.createElement('div');
      empty.className = 'chat-empty';
      empty.textContent = 'Здесь появится ответ на общий запрос.';
      history.append(empty);
    } else {
      arena.histories[agent].forEach((message) => history.append(chatBubble(message.role, message.content, '', message.audit, agent)));
    }
    const status = arena.statuses[agent] || (arena.histories[agent].at(-1)?.role === 'assistant' ? 'Ответ получен' : 'Готов');
    $(`#arena-${agent}-status`).textContent = status;
    const result = arena.results[agent];
    const evaluation = arena.evaluations[agent];
    const metrics = $(`#arena-${agent}-metrics`);
    metrics.replaceChildren();
    const labels = [
      ['Аудит', evaluation ? `${evaluation.passed}/${evaluation.total} подтверждено` : '—'],
      ['Tools', result ? String(result.tool_calls ?? 0) : '—'],
      ['LLM', result ? String(result.llm_calls ?? 0) : '—'],
      ['Время', result ? `${Number(result.latency_seconds || 0).toFixed(1).replace('.', ',')} с` : '—'],
    ];
    for (const [name, value] of labels) {
      const span = document.createElement('span');
      const strong = document.createElement('b');
      strong.textContent = value;
      span.append(`${name}: `, strong);
      metrics.append(span);
    }
    const trace = $(`#arena-${agent}-trace`);
    trace.replaceChildren();
    if (result) {
      (result.events || []).forEach((event) => trace.append(traceNode(event, trace.childElementCount)));
    } else {
      const item = document.createElement('li');
      item.textContent = 'Траектория появится после запуска и доступна до перезагрузки страницы.';
      trace.append(item);
    }
    requestAnimationFrame(() => { history.scrollTop = history.scrollHeight; });
  }
  arenaInput.disabled = state.running || state.runtime !== 'live';
  $('#arena-clear').disabled = state.running || !arena.histories.baseline.length;
  $('#arena-send').disabled = state.runtime !== 'live' || (!state.running && !arenaInput.value.trim());
  $('#arena-send').textContent = state.running ? 'Остановить' : 'Сравнить ↗';
  $('#arena-state').textContent = state.runtime !== 'live'
    ? 'Для запуска откройте локальный demo-сервер.'
    : state.running ? 'Выполняется; можно остановить ожидание.' : 'Enter — отправить · Shift+Enter — новая строка';
}

async function arenaFetch(url, body, timeoutMs) {
  const controller = new AbortController();
  state.arena.controllers.add(controller);
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
      const error = new Error(payload.message || payload.error || 'request failed');
      error.result = payload.result;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timer);
    state.arena.controllers.delete(controller);
  }
}

async function runArenaCandidate(agent, messages, auditId) {
  const arena = state.arena;
  arena.statuses[agent] = 'Агент отвечает…';
  renderArena();
  try {
    const payload = await arenaFetch('/api/chat', { agent, messages, ...(auditId ? { audit_id: auditId } : {}) }, REQUEST_TIMEOUT_MS);
    if (arena.stopRequested) return;
    arena.results[agent] = payload.result;
    const reply = { role: 'assistant', content: payload.result.output };
    arena.histories[agent].push(reply);
    arena.statuses[agent] = auditId ? 'Аудитор проверяет…' : 'Ответ получен';
    persistArena();
    renderArena();
    if (auditId) {
      try {
        const checked = await arenaFetch('/api/audit/check', { audit_id: auditId, agent }, AUDIT_TIMEOUT_MS);
        if (arena.stopRequested) return;
        arena.evaluations[agent] = checked.evaluation;
        reply.audit = { evaluation: checked.evaluation };
        arena.statuses[agent] = 'Оценка готова';
        persistArena();
      } catch (error) {
        arena.statuses[agent] = error.name === 'AbortError' ? 'Ответ · аудит остановлен' : `Ответ · аудит недоступен: ${error.message}`;
      }
    }
  } catch (error) {
    if (error.result) arena.results[agent] = error.result;
    arena.statuses[agent] = error.name === 'AbortError' ? 'Остановлен или тайм-аут' : `Ошибка: ${error.message}`;
  } finally {
    renderArena();
  }
}

async function sendArena() {
  const arena = state.arena;
  if (state.running) {
    arena.stopRequested = true;
    arena.controllers.forEach((controller) => controller.abort());
    return;
  }
  const content = arenaInput.value.trim();
  if (!content || state.runtime !== 'live') return;
  arena.stopRequested = false;
  arena.criteria = [];
  arena.evaluations = {};
  arena.results = {};
  arena.statuses = {};
  arena.error = '';
  for (const agent of ['baseline', 'optimized']) arena.histories[agent].push({ role: 'user', content });
  trimArenaHistories();
  const histories = Object.fromEntries(['baseline', 'optimized'].map((agent) => [
    agent, arena.histories[agent].map(({ role, content: text }) => ({ role, content: text })),
  ]));
  persistArena();
  arenaInput.value = '';
  state.running = true;
  renderArena();
  try {
    let auditIds = {};
    if (state.auditor.enabled) {
      try {
        const plan = await arenaFetch('/api/arena/plan', { histories }, AUDIT_TIMEOUT_MS);
        auditIds = plan.audit_ids;
        arena.criteria = plan.criteria;
        persistArena();
        renderArena();
      } catch (error) {
        if (arena.stopRequested) return;
        arena.error = error.name === 'AbortError' ? 'формирование критериев превысило 45 секунд' : error.message;
        renderArena();
      }
    }
    if (arena.stopRequested) return;
    arena.statuses.optimized = 'Ждёт своей очереди…';
    renderArena();
    // This gateway reliably serves one internal model request at a time.
    // Sequential runs preserve identical precommitted criteria without overload.
    for (const agent of ['baseline', 'optimized']) {
      if (arena.stopRequested) break;
      await runArenaCandidate(agent, histories[agent], auditIds[agent]);
    }
  } finally {
    state.running = false;
    persistArena();
    renderArena();
    if (!arena.stopRequested) arenaInput.focus();
  }
}

document.querySelectorAll('.agent-button').forEach((button) => {
  button.addEventListener('click', () => setAgent(button.dataset.agent));
});

customTaskButton.addEventListener('click', () => {
  if (state.running) return;
  state.customMode = true;
  state.arenaMode = false;
  document.body.classList.remove('arena-mode');
  $('#arena-screen').hidden = true;
  state.shown = false;
  state.chatError = '';
  renderTaskList();
  renderFreeChat();
  if (state.runtime === 'live') chatInput.focus();
});

arenaButton.addEventListener('click', () => {
  if (state.running) return;
  state.customMode = false;
  state.arenaMode = true;
  document.body.classList.add('arena-mode');
  renderTaskList();
  renderArena();
  if (state.runtime === 'live') arenaInput.focus();
});

$('#arena-form').addEventListener('submit', (event) => {
  event.preventDefault();
  sendArena();
});

arenaInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendArena();
  }
});
arenaInput.addEventListener('input', renderArena);
$('#arena-clear').addEventListener('click', () => {
  if (state.running || !state.arena.histories.baseline.length) return;
  if (!window.confirm('Очистить обе истории Арены и общие критерии?')) return;
  state.arena.histories = { baseline: [], optimized: [] };
  state.arena.criteria = [];
  state.arena.evaluations = {};
  state.arena.results = {};
  state.arena.statuses = {};
  state.arena.error = '';
  persistArena();
  renderArena();
  arenaInput.focus();
});

runButton.addEventListener('click', runCurrent);
swapButton.addEventListener('click', async () => {
  setAgent(state.agent === 'baseline' ? 'optimized' : 'baseline');
  await runCurrent();
});

answerToggle.addEventListener('click', () => {
  const expanded = answer.classList.toggle('expanded');
  answerToggle.textContent = expanded ? 'Свернуть ответ' : 'Показать ответ целиком';
});

chatForm.addEventListener('submit', (event) => {
  event.preventDefault();
  sendChat();
});

chatInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendChat();
  }
});

chatInput.addEventListener('input', () => {
  state.chatError = '';
  updateChatControls();
});

clearChatButton.addEventListener('click', () => {
  if (state.running || !activeHistory().length) return;
  if (!window.confirm(`Очистить историю ${state.agent === 'baseline' ? 'стартового' : 'оптимизированного'} агента?`)) return;
  state.histories[state.agent] = [];
  state.lastChatResults[state.agent] = null;
  state.currentAudits[state.agent] = null;
  state.chatError = '';
  persistHistories();
  renderFreeChat();
  chatInput.focus();
});

async function detectRuntime() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1500);
  try {
    const response = await fetch('/api/status', { cache: 'no-store', signal: controller.signal });
    const payload = await response.json();
    if (response.ok && payload.mode === 'live') {
      state.runtime = 'live';
      state.auditor = payload.auditor || state.auditor;
      runtimeBadge.classList.add('live');
      runtimeBadge.querySelector('span').textContent = `живой запуск · ${payload.model}`;
      $('#run-note').textContent = 'Ответ будет рассчитан заново этой же моделью';
      return;
    }
  } catch (_) { /* hosted static replay */ }
  finally { clearTimeout(timer); }
  state.runtime = 'replay';
  runtimeBadge.querySelector('span').textContent = 'воспроизведение eval';
  $('#run-note').textContent = 'Сохранённый фактический прогон, без постановочной анимации ответа';
}

async function boot() {
  try {
    const [data] = await Promise.all([
      fetch('demo-data.json', { cache: 'no-store' }).then((response) => {
        if (!response.ok) throw new Error('demo-data.json недоступен');
        return response.json();
      }),
      detectRuntime(),
    ]);
    state.data = data;
    renderTaskList();
    renderCase();
    setAgent('baseline');
  } catch (error) {
    taskList.textContent = 'Не удалось загрузить набор задач.';
    toast(error.message);
  }
}

boot();
