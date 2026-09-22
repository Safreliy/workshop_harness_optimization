const STORAGE_KEY = 'harness-lab-chat-v2';
const REQUEST_TIMEOUT_MS = 90_000;

const state = {
  data: null,
  selectedTask: 0,
  agent: 'baseline',
  runtime: 'replay',
  running: false,
  shown: false,
  customMode: false,
  liveResults: new Map(),
  histories: loadHistories(),
  lastChatResults: { baseline: null, optimized: null },
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

const number = (value, digits = 3) => Number(value || 0).toFixed(digits).replace('.', ',');
const signed = (value) => `${Number(value) >= 0 ? '+' : ''}${number(value)}`;
const activeTask = () => state.data.tasks[state.selectedTask];
const resultKey = () => `${activeTask().id}:${state.agent}`;
const activeResult = () => state.liveResults.get(resultKey()) || activeTask().agents[state.agent];
const activeHistory = () => state.histories[state.agent];

function loadHistories() {
  const empty = { baseline: [], optimized: [] };
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    for (const agent of ['baseline', 'optimized']) {
      if (!Array.isArray(saved[agent])) continue;
      empty[agent] = saved[agent]
        .filter((item) => item && ['user', 'assistant'].includes(item.role) && typeof item.content === 'string')
        .slice(-40)
        .map(({ role, content }) => ({ role, content }));
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
  if (state.customMode) renderFreeChat();
  else renderResult();
}

function renderTaskList() {
  taskList.replaceChildren();
  state.data.tasks.forEach((task, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    const selected = !state.customMode && index === state.selectedTask;
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
      state.shown = false;
      renderTaskList();
      renderCase();
      renderResult();
    });
    taskList.append(button);
  });
  customTaskButton.classList.toggle('active', state.customMode);
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
  resultCard.classList.remove('pass', 'fail');
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
  if (!state.data || state.customMode) return;
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
  resultCard.classList.remove('pass', 'fail');
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

function chatBubble(role, content, extraClass = '') {
  const node = document.createElement('div');
  node.className = `chat-bubble ${role}${extraClass ? ` ${extraClass}` : ''}`;
  const label = document.createElement('small');
  label.textContent = role === 'user' ? 'Вы' : state.agent === 'baseline' ? 'Стартовый ReAct' : 'Оптимизированный harness';
  const body = document.createElement('p');
  body.textContent = content;
  node.append(label, body);
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
    history.forEach((message) => chatHistory.append(chatBubble(message.role, message.content)));
    if (state.running) chatHistory.append(chatBubble('assistant', 'Готовлю ответ…', 'pending'));
    if (state.chatError) chatHistory.append(chatBubble('assistant', state.chatError, 'error'));
  }
  requestAnimationFrame(() => { chatHistory.scrollTop = chatHistory.scrollHeight; });
}

function renderChatMetrics() {
  const result = state.lastChatResults[state.agent];
  if (!result) {
    resetMetrics('Свободный чат не имеет эталонного ответа. После ответа покажем стоимость и фактическую траекторию.');
    return;
  }
  resultCard.classList.remove('pass', 'fail');
  resultCard.classList.add(result.error ? 'fail' : 'pass');
  $('#result-verdict').textContent = result.error ? 'ОШИБКА' : 'ОТВЕТ ПОЛУЧЕН';
  $('#output-score').textContent = '—';
  const toolCalls = (result.events || []).filter((event) => event.stage === 'tool').length;
  $('#trajectory-score').textContent = `${toolCalls} tools`;
  $('#llm-calls').textContent = String(result.llm_calls ?? '—');
  const latency = Number(result.latency_seconds || 0).toFixed(1).replace('.', ',');
  $('#grader-note').textContent = `Автооценки корректности здесь нет: это свободный запрос. Показана наблюдаемая траектория; время ответа — ${latency} с.`;
  renderTrace(result);
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
    ? 'Формата нет: пишите обычным текстом, вставляйте SQL в сообщение и задавайте уточняющие вопросы. История сохраняется отдельно для каждого агента в этом браузере.'
    : 'Публичная версия показывает eval-прогоны, но не получает секретный ключ модели. Для живого чата откройте локальный demo-сервер.';
  const expectations = [
    'свободный текст вместо обязательной формы',
    'история передаётся модели в каждом следующем сообщении',
    'контекст стартового и оптимизированного агентов хранится отдельно',
    'запрос можно остановить; таймаут — 90 секунд',
  ];
  $('#expectation-list').replaceChildren(...expectations.map((text) => {
    const item = document.createElement('li');
    item.textContent = text;
    return item;
  }));
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
  if (state.customMode) return;
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

function startElapsedTimer() {
  clearInterval(state.elapsedTimer);
  state.startedAt = Date.now();
  const update = () => {
    if (!state.running) return;
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    chatState.textContent = `Ждём ответ · ${seconds} с · максимум 90 с`;
  };
  update();
  state.elapsedTimer = setInterval(update, 1000);
}

async function sendChat() {
  if (state.running) {
    state.controller?.abort();
    return;
  }
  const content = chatInput.value.trim();
  if (!content || state.runtime !== 'live') return;
  const agentAtStart = state.agent;
  state.chatError = '';
  state.histories[agentAtStart].push({ role: 'user', content });
  state.histories[agentAtStart] = state.histories[agentAtStart].slice(-40);
  persistHistories();
  chatInput.value = '';
  state.running = true;
  startElapsedTimer();
  renderFreeChat();
  try {
    const response = await timedFetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: agentAtStart, messages: state.histories[agentAtStart] }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.message || payload.error || 'chat failed');
    state.histories[agentAtStart].push({ role: 'assistant', content: payload.result.output });
    state.histories[agentAtStart] = state.histories[agentAtStart].slice(-40);
    state.lastChatResults[agentAtStart] = payload.result;
    persistHistories();
  } catch (error) {
    const aborted = error.name === 'AbortError';
    state.chatError = aborted
      ? 'Запрос остановлен. Если вы его не останавливали, модель не ответила за 90 секунд.'
      : `Не удалось получить ответ: ${error.message}`;
  } finally {
    clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
    state.running = false;
    renderFreeChat();
    if (!state.chatError) chatInput.focus();
  }
}

document.querySelectorAll('.agent-button').forEach((button) => {
  button.addEventListener('click', () => setAgent(button.dataset.agent));
});

customTaskButton.addEventListener('click', () => {
  if (state.running) return;
  state.customMode = true;
  state.shown = false;
  state.chatError = '';
  renderTaskList();
  renderFreeChat();
  if (state.runtime === 'live') chatInput.focus();
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
