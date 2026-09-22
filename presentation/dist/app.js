const slides = [...document.querySelectorAll('.slide')];
const rail = document.querySelector('.rail');
const current = document.querySelector('#current-slide');
const total = document.querySelector('#total-slides');
const progress = document.querySelector('#progress-bar');
const chapter = document.querySelector('.chapter-label');
let activeIndex = 0;

const two = (n) => String(n).padStart(2, '0');
total.textContent = two(slides.length);

slides.forEach((slide, index) => {
  const dot = document.createElement('a');
  dot.href = `#${slide.id}`;
  dot.setAttribute('aria-label', `${index + 1}: ${slide.dataset.title}`);
  rail.append(dot);
});

const dots = [...rail.querySelectorAll('a')];
const activate = (index) => {
  activeIndex = index;
  slides.forEach((slide, i) => slide.classList.toggle('active', i === index));
  dots.forEach((dot, i) => dot.classList.toggle('active', i === index));
  current.textContent = two(index + 1);
  progress.style.width = `${((index + 1) / slides.length) * 100}%`;
  chapter.textContent = `${slides[index].dataset.chapter} · ${slides[index].dataset.title}`;
};

const observer = new IntersectionObserver((entries) => {
  const visible = entries.filter((e) => e.isIntersecting).sort((a,b) => b.intersectionRatio - a.intersectionRatio)[0];
  if (visible) activate(slides.indexOf(visible.target));
}, { threshold: [.45, .65] });
slides.forEach((slide) => observer.observe(slide));
activate(0);

const go = (index) => slides[Math.max(0, Math.min(slides.length - 1, index))].scrollIntoView();
document.addEventListener('keydown', (event) => {
  if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); go(activeIndex + 1); }
  if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); go(activeIndex - 1); }
  if (event.key === 'Home') { event.preventDefault(); go(0); }
  if (event.key === 'End') { event.preventDefault(); go(slides.length - 1); }
  if (event.key.toLowerCase() === 'f') document.documentElement.requestFullscreen?.();
});

const help = document.querySelector('.help');
document.querySelector('.help-button').addEventListener('click', () => { help.hidden = !help.hidden; });
document.querySelector('.help-close').addEventListener('click', () => { help.hidden = true; });

const wireTabs = (buttons, panels, buttonKey, panelKey) => {
  buttons.forEach((button) => button.addEventListener('click', () => {
    const value = button.dataset[buttonKey];
    buttons.forEach((item) => {
      const selected = item === button;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-selected', String(selected));
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset[panelKey] !== value; });
  }));
};

wireTabs(
  [...document.querySelectorAll('.mode-button')],
  [...document.querySelectorAll('[data-mode-panel]')],
  'mode',
  'modePanel'
);
wireTabs(
  [...document.querySelectorAll('.score-tab')],
  [...document.querySelectorAll('[data-score-panel]')],
  'score',
  'scorePanel'
);

const flowItems = [...document.querySelectorAll('.experiment-flow [data-step]')];
let flowTimers = [];
document.querySelector('.play-flow').addEventListener('click', (event) => {
  flowTimers.forEach(clearTimeout);
  flowTimers = [];
  flowItems.forEach((item) => item.classList.remove('lit'));
  event.currentTarget.querySelector('span').textContent = '↻';
  const instant = matchMedia('(prefers-reduced-motion: reduce)').matches;
  flowItems.forEach((item, index) => {
    flowTimers.push(setTimeout(() => {
      item.classList.add('lit');
      if (index === flowItems.length - 1) event.currentTarget.querySelector('span').textContent = '▶';
    }, instant ? 0 : index * 220));
  });
});

const axes = {
  round1: ['РАУНД 01 · РОДИТЕЛЬ 0', 'Модель предложила три разных механизма', 'Workflow ухудшил цену. Context ledger почти не помог. Добавление пробного индекса и диагностических описаний подняло SQL с 0,156 до 0,758.'],
  round2: ['РАУНД 02 · РОДИТЕЛЬ 2', 'Модель развила сильную tool-ветку', 'Reviewer поднял SQL до 0,955, но стоил 6,5 вызова. Routing плюс увеличение step budget дали 0,898 при 3,7 вызова и стали новым лучшим родителем.'],
  round3: ['РАУНД 03 · РОДИТЕЛЬ 5', 'Две разные попытки закрыть остаточные провалы', 'Ledger улучшил отчёты, но общий score снизился. Короткая инструкция против повторных выборок подняла SQL и trajectory: вариант 7 стал лучшим.'],
  best: ['ВАРИАНТ 07 · РОДИТЕЛЬ 5', 'Итог — пять изменений, а не один prompt', 'От baseline унаследована ReAct-основа. Модель добавила trial_index_plan, диагностические описания, routing, max_steps=8 и только затем короткий prompt patch.']
};
const axisButtons = [...document.querySelectorAll('.coordinate')];
axisButtons.forEach((button) => button.addEventListener('click', () => {
  axisButtons.forEach((item) => item.classList.toggle('active', item === button));
  const [kicker, title, copy] = axes[button.dataset.axis];
  document.querySelector('#axis-kicker').textContent = kicker;
  document.querySelector('#axis-title').textContent = title;
  document.querySelector('#axis-copy').textContent = copy;
}));

const baselineViews = {
  all: { label: 'общая оценка', score: '0,475', value: '47.51%', caption: '0 из 10 задач прошли ответ и траекторию · 5,4 вызова в среднем' },
  report: { label: 'оценка отчётов', score: '0,688', value: '68.80%', caption: 'Ответ: 0,852 · траектория: 0,195 · полный проход: 0 из 6' },
  sql: { label: 'оценка SQL', score: '0,156', value: '15.58%', caption: 'Ответ: 0,000 · траектория: 0,623 · полный проход: 0 из 4' }
};
const metricButtons = [...document.querySelectorAll('.metric-button')];
metricButtons.forEach((button) => button.addEventListener('click', () => {
  metricButtons.forEach((item) => item.classList.toggle('active', item === button));
  const view = baselineViews[button.dataset.metric];
  document.querySelector('#baseline-label').textContent = view.label;
  document.querySelector('#baseline-score').textContent = view.score;
  document.querySelector('#baseline-meter').style.setProperty('--value', view.value);
  document.querySelector('#baseline-caption').textContent = view.caption;
}));

const trials = {
  t0: { number: 'ВАРИАНТ 00 · BASELINE', status: 'вне финального beam', statusClass: 'rejected', title: 'Точка отсчёта', score: '0,475', calls: '5,4', objective: '0,449', copy: 'Ответ: 0,511. Траектория: 0,366. Ни одна из 10 задач не прошла обе группы проверок.' },
  t1: { number: 'ВАРИАНТ 01 · ОТ 00 · WORKFLOW', status: 'отклонён', statusClass: 'rejected', title: 'Планирование добавило цену без пользы', score: '0,465', calls: '6,6', objective: '0,431', copy: 'Модель заменила ReAct на plan→execute. Вызовов стало больше, SQL и итоговая цель снизились. Провал сохранён для следующих раундов.' },
  t2: { number: 'ВАРИАНТ 02 · ОТ 00 · TOOLS', status: 'стал родителем', statusClass: 'accepted', title: 'Добавлен пробный индекс и понятные описания', score: '0,679', calls: '5,8', objective: '0,651', copy: 'Модель включила trial_index_plan и diagnostic descriptions. SQL вырос с 0,156 до 0,758; именно эту ветку она развила во втором раунде.' },
  t3: { number: 'ВАРИАНТ 03 · ОТ 00 · CONTEXT', status: 'выбыл позднее', statusClass: 'rejected', title: 'Ledger отдельно почти не помог', score: '0,467', calls: '6,3', objective: '0,436', copy: 'Контекстная мутация слегка улучшила отчёты, но снизила качество траектории и была дороже baseline.' },
  t4: { number: 'ВАРИАНТ 04 · ОТ 02 · REVIEW', status: 'выбыл позднее', statusClass: 'rejected', title: 'Reviewer силён в SQL, но дорог', score: '0,769', calls: '6,5', objective: '0,736', copy: 'Domain reviewer поднял SQL до 0,955, но отчёты остались 0,645, а стоимость выросла. Вариант не вошёл в финальный beam.' },
  t5: { number: 'ВАРИАНТ 05 · ОТ 02 · ROUTING + BUDGET', status: 'в финальном beam', statusClass: 'accepted', title: 'Главный структурный скачок', score: '0,898', calls: '3,7', objective: '0,882', copy: 'Routing убрал БД-tools из отчётов, max_steps=8 дал SQL закончить проверку. Это лучший кандидат до любой prompt-мутации.' },
  t6: { number: 'ВАРИАНТ 06 · ОТ 05 · CONTEXT', status: 'в финальном beam', statusClass: 'accepted', title: 'Отдельный компромисс для отчётов', score: '0,833', calls: '4,1', objective: '0,815', copy: 'Requirements ledger поднял отчёты до 0,885, но SQL в этом повторе просел. Ветка сохранена как недоминируемая по задачам.' },
  t7: { number: 'ВАРИАНТ 07 · ОТ 05 · PROMPT', status: 'лучший', statusClass: 'accepted', title: 'Короткий patch поверх работающей структуры', score: '0,913', calls: '3,2', objective: '0,900', copy: 'Инструкция требует планировать проверку до sample queries. Она улучшила структурный вариант 5, но не является источником основного прироста от baseline.' }
};
const trialMetrics = {
  t0: { score: .4751, calls: 5.4, objective: .4487, report: .6880, sql: .1558 },
  t1: { score: .4648, calls: 6.6, objective: .4312, report: .6939, sql: .1212 },
  t2: { score: .6794, calls: 5.8, objective: .6506, report: .6272, sql: .7577 },
  t3: { score: .4673, calls: 6.3, objective: .4355, report: .6949, sql: .1260 },
  t4: { score: .7691, calls: 6.5, objective: .7361, report: .6454, sql: .9546 },
  t5: { score: .8977, calls: 3.7, objective: .8815, report: .8700, sql: .9392 },
  t6: { score: .8333, calls: 4.1, objective: .8147, report: .8845, sql: .7566 },
  t7: { score: .9128, calls: 3.2, objective: .8996, report: .8669, sql: .9818 }
};
const plotViews = {
  cost: {
    xMetric: 'calls', yMetric: 'score', xDirection: 'min', yDirection: 'max',
    xRange: [3, 7], yRange: [.43, .94], xLabel: 'вызовы модели →', yLabel: 'общая оценка ↑', direction: 'лучше ↖',
    context: 'По качеству и числу вызовов вариант 7 доминирует все остальные: 0,913 при 3,2 вызова. Его родитель 5 уже достиг 0,898 без prompt patch.',
    offsets: { t0: [-8, 8], t1: [8, 0], t3: [0, -8] }
  },
  tasks: {
    xMetric: 'report', yMetric: 'sql', xDirection: 'max', yDirection: 'max',
    xRange: [.60, .91], yRange: [.10, 1], xLabel: 'оценка отчётов →', yLabel: 'оценка SQL ↑', direction: 'лучше ↗',
    context: 'Парето-фронт по двум задачам: 5, 6 и 7. Вариант 6 лучше в отчётах; 7 — в SQL; 5 сохраняет баланс без финального prompt patch.',
    offsets: { t0: [-8, 8], t1: [8, 0], t3: [0, -8], t5: [0, 8], t7: [0, -6] }
  }
};
const pointColors = { t0: '#86a8ff', t1: '#ff7f6e', t2: '#c8ff4d', t3: '#ff7f6e', t4: '#c69cff', t5: '#c8ff4d', t6: '#86a8ff', t7: '#c69cff' };
const trialOrder = ['t0', 't1', 't2', 't3', 't4', 't5', 't6', 't7'];
let selectedTrial = 0;
let activePlot = 'cost';
const trialPoints = [...document.querySelectorAll('.trial-point')];
const trialLegend = [...document.querySelectorAll('[data-trial-legend]')];
const metricText = (value, digits = 3) => value.toFixed(digits).replace('.', ',');
const showTrial = (key) => {
  const trial = trials[key];
  const metrics = trialMetrics[key];
  selectedTrial = trialOrder.indexOf(key);
  trialPoints.forEach((point) => point.classList.toggle('active', point.dataset.trial === key));
  trialLegend.forEach((item) => item.classList.toggle('active', item.dataset.trialLegend === key));
  document.querySelector('#trial-number').textContent = trial.number;
  const status = document.querySelector('#trial-status');
  status.textContent = trial.status;
  status.className = trial.statusClass;
  document.querySelector('#trial-title').textContent = trial.title;
  const labels = activePlot === 'tasks' ? ['отчёты', 'SQL', 'общая'] : ['оценка', 'вызовы', 'цель'];
  const values = activePlot === 'tasks'
    ? [metricText(metrics.report), metricText(metrics.sql), metricText(metrics.score)]
    : [metricText(metrics.score), metricText(metrics.calls, 1), metricText(metrics.objective)];
  document.querySelector('#trial-metric-a-label').textContent = labels[0];
  document.querySelector('#trial-metric-b-label').textContent = labels[1];
  document.querySelector('#trial-metric-c-label').textContent = labels[2];
  document.querySelector('#trial-score').textContent = values[0];
  document.querySelector('#trial-calls').textContent = values[1];
  document.querySelector('#trial-objective').textContent = values[2];
  document.querySelector('#trial-copy').textContent = trial.copy;
};
const scalePlotValue = (value, [min, max], invert = false) => {
  const ratio = Math.max(0, Math.min(1, (value - min) / (max - min)));
  return invert ? 92 - ratio * 84 : 8 + ratio * 84;
};
const paretoFront = (view) => trialOrder.filter((key) => {
  const current = trialMetrics[key];
  return !trialOrder.some((otherKey) => {
    if (otherKey === key) return false;
    const other = trialMetrics[otherKey];
    const xAtLeast = view.xDirection === 'max' ? other[view.xMetric] >= current[view.xMetric] : other[view.xMetric] <= current[view.xMetric];
    const yAtLeast = view.yDirection === 'max' ? other[view.yMetric] >= current[view.yMetric] : other[view.yMetric] <= current[view.yMetric];
    const xBetter = view.xDirection === 'max' ? other[view.xMetric] > current[view.xMetric] : other[view.xMetric] < current[view.xMetric];
    const yBetter = view.yDirection === 'max' ? other[view.yMetric] > current[view.yMetric] : other[view.yMetric] < current[view.yMetric];
    return xAtLeast && yAtLeast && (xBetter || yBetter);
  });
});
const plotButtons = [...document.querySelectorAll('.pareto-mode')];
const applyPlotView = (mode) => {
  activePlot = mode;
  const view = plotViews[mode];
  const positions = {};
  const front = paretoFront(view);
  trialPoints.forEach((point) => {
    const key = point.dataset.trial;
    const metrics = trialMetrics[key];
    const x = scalePlotValue(metrics[view.xMetric], view.xRange);
    const y = scalePlotValue(metrics[view.yMetric], view.yRange, true);
    const [offsetX = 0, offsetY = 0] = view.offsets[key] || [];
    positions[key] = [x, y];
    point.style.setProperty('--x', `${x}%`);
    point.style.setProperty('--y', `${y}%`);
    point.style.setProperty('--ox', `${offsetX}px`);
    point.style.setProperty('--oy', `${offsetY}px`);
    point.style.setProperty('--point', pointColors[key]);
    point.classList.toggle('on-front', front.includes(key));
  });
  trialLegend.forEach((item) => item.classList.toggle('on-front', front.includes(item.dataset.trialLegend)));
  const orderedFront = [...front].sort((a, b) => trialMetrics[a][view.xMetric] - trialMetrics[b][view.xMetric]);
  document.querySelector('#pareto-line').setAttribute('points', orderedFront.map((key) => positions[key].map((value) => value.toFixed(2)).join(',')).join(' '));
  document.querySelector('#pareto-x-label').textContent = view.xLabel;
  document.querySelector('#pareto-y-label').textContent = view.yLabel;
  document.querySelector('#pareto-direction').textContent = view.direction;
  document.querySelector('#pareto-context').textContent = view.context;
  plotButtons.forEach((button) => {
    const selected = button.dataset.plot === mode;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-selected', String(selected));
  });
  showTrial(trialOrder[selectedTrial]);
};
plotButtons.forEach((button) => button.addEventListener('click', () => applyPlotView(button.dataset.plot)));
trialPoints.forEach((point) => point.addEventListener('click', () => showTrial(point.dataset.trial)));
trialLegend.forEach((item) => item.addEventListener('click', () => showTrial(item.dataset.trialLegend)));
document.querySelectorAll('[data-step-trial]').forEach((button) => button.addEventListener('click', () => {
  selectedTrial = (selectedTrial + Number(button.dataset.stepTrial) + trialOrder.length) % trialOrder.length;
  showTrial(trialOrder[selectedTrial]);
}));
applyPlotView('cost');

const reuse = {
  cases: ['СЛОЙ 01 · ЗАМЕНИТЬ', 'Соберите пары «задача + наблюдаемый провал»', 'Возьмите 10–30 рабочих кейсов с поздними правками, ошибками инструментов, неверными решениями или лишними шагами.', 'Support → policy exceptions · Coding → failing tests · RAG → citation misses'],
  registry: ['СЛОЙ 02 · РАСШИРИТЬ', 'Опишите affordances как конфигурацию', 'Вынесите prompt profiles, разрешённые tools, routing, context strategy и бюджеты из кода в валидируемый registry.', 'Новый tool попадает в поиск только через allowlist и схему аргументов'],
  graders: ['СЛОЙ 03 · ПЕРЕПИСАТЬ ПОД ДОМЕН', 'Опишите проверяемые инварианты', 'Разделите корректность результата и дисциплину траектории. Максимум проверок должен работать локально и детерминированно.', 'Coding → tests + diff safety · RAG → claim/citation entailment + retrieval trace'],
  search: ['СЛОЙ 04 · СОХРАНИТЬ', 'Модель генерирует портфель гипотез', 'Дайте proposer-у несколько родителей, метрики и провальные traces. Ограничьте только безопасную schema, разнообразие портфеля и бюджет.', 'Каждый trial хранит parent, operator, hypothesis, before/after diff и метрики'],
  eval: ['СЛОЙ 05 · ЗАМЕНИТЬ ДАННЫЕ', 'Eval — обратная связь, не печать качества', 'Сравнивайте baseline и кандидатов по тем же проверкам. Eval можно открывать повторно; для доказательства переноса позже нужен отдельный закрытый test.', 'Support → свежие диалоги · Coding → новые репозитории · RAG → новые коллекции']
};
const reuseButtons = [...document.querySelectorAll('.reuse-node')];
reuseButtons.forEach((button) => button.addEventListener('click', () => {
  reuseButtons.forEach((item) => item.classList.toggle('active', item === button));
  const [kicker, title, copy, example] = reuse[button.dataset.reuse];
  document.querySelector('#reuse-kicker').textContent = kicker;
  document.querySelector('#reuse-title').textContent = title;
  document.querySelector('#reuse-copy').textContent = copy;
  document.querySelector('#reuse-example').innerHTML = `<span>пример</span>${example}`;
}));

let touchStartY = 0;
document.addEventListener('touchstart', (event) => { touchStartY = event.changedTouches[0].clientY; }, { passive: true });
document.addEventListener('touchend', (event) => {
  const delta = touchStartY - event.changedTouches[0].clientY;
  if (Math.abs(delta) > 70) go(activeIndex + (delta > 0 ? 1 : -1));
}, { passive: true });
