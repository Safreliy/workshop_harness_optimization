(() => {
  'use strict';
  const data = window.WORKSHOP_TRAJECTORIES;
  const $ = id => document.getElementById(id);
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pretty = value => JSON.stringify(value, null, 2);
  const block = value => `<pre>${esc(pretty(value))}</pre>`;
  let selected = new URLSearchParams(location.search).get('case') || 'diagnosis';
  let current;
  function external(event) { return event.stage === 'final_public_check'; }
  function table(output) {
    if (!output || !Array.isArray(output.rows) || !output.rows.length) return '';
    const columns = output.columns || Object.keys(output.rows[0]);
    return `<div class="table-scroll"><table><thead><tr>${columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${output.rows.map(row => `<tr>${columns.map(c => `<td>${row[c] === null ? '<i>null</i>' : esc(typeof row[c] === 'object' || typeof row[c] === 'string' ? JSON.stringify(row[c]) : row[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }
  function visibleText(event) {
    if (external(event)) return 'Вызван управляющим кодом после цикла; результат не добавляется в контекст модели.';
    if (event.stage === 'preflight') return 'Вызван управляющим кодом до ReAct; ответ передаётся модели как наблюдение.';
    if (event.stage === 'harness_review') return 'Проверка управляющего кода; ответ передаётся модели для исправления.';
    if (event.name) return 'Модель запросила инструмент; ответ передаётся ей в следующем вызове.';
    if ('content' in event) return 'Сохранённый ответ модели. Это вывод API, не отдельная запись скрытых рассуждений.';
    return 'Служебное событие управляющего цикла.';
  }
  function renderEvents() {
    const filter = $('filter').value;
    let n = 0;
    $('events').innerHTML = current.record.events.map((event, index) => {
      const kind = external(event) ? 'external' : event.name ? 'tool' : 'content' in event ? 'model' : 'control';
      if (filter === 'model' && kind !== 'model' || filter === 'tools' && !event.name || filter === 'external' && kind !== 'external') return '';
      n++;
      const active = current.id === 'diagnosis' && [29,30,31,33].includes(index) || current.id === 'validation' && [13,14,17].includes(index);
      const title = event.name || ({planning:'План модели',agent:'Ответ модели',forced_final:'Финальный ответ модели',truncated_response:'Ответ прерван лимитом'}[event.stage] || event.stage);
      let body = `<p class="visibility">${esc(visibleText(event))}</p>`;
      if ('content' in event) body += `<div class="model-text">${esc(event.content || '(Пустой текст; запрос инструмента ниже.)')}</div>`;
      if (event.arguments) body += `<div class="label">Аргументы инструмента</div>${event.arguments.sql ? `<pre>${esc(event.arguments.sql)}</pre>` : block(event.arguments)}`;
      if (event.tool_calls?.length) body += `<div class="label">Запрошенные вызовы</div>${block(event.tool_calls)}`;
      if ('output' in event) body += `<div class="label">Ответ инструмента · все сохранённые строки</div>${(event.output?.display_truncated || event.output?.row_count > event.output?.rows?.length) ? '<p class="visibility">Исходный инструмент показал срез таблицы. Ниже весь сохранённый ответ, но не все строки базы; полный размер указан в row_count.</p>' : ''}${table(event.output)}<details><summary>Полный ответ JSON</summary>${block(event.output)}</details>`;
      body += `<details><summary>Событие ${index} целиком · без преобразований</summary>${block(event)}</details>`;
      return `<details class="event ${kind} ${active ? 'highlight' : ''}" id="event-${index}" data-index="${index}"><summary>#${String(index).padStart(2,'0')} · ${esc(title)}<span>${esc(event.stage)}${event.step ? ' · шаг '+event.step : ''}${event.input_tokens !== undefined ? ' · input '+event.input_tokens+' / output '+event.output_tokens+' tokens' : ''}</span></summary>${body}</details>`;
    }).join('');
    $('count').textContent = `Показано ${n} из ${current.record.events.length} событий. Нумерация events[index] с нуля.`;
    revealAnchor();
  }
  function revealAnchor() {
    const match = location.hash.match(/^#event-(\d+)$/);
    if (!match) return;
    const el = $('event-'+match[1]);
    if (el) { el.open = true; requestAnimationFrame(() => el.scrollIntoView({block:'start'})); }
  }
  function render() {
    current = data.cases.find(c => c.id === selected) || data.cases[0];
    $('cases').innerHTML = `<label>Сохранённый запуск <select id="case-select">${data.cases.map(c => `<option value="${esc(c.id)}" ${c === current ? 'selected' : ''}>${esc(c.title)}</option>`).join('')}</select></label>`;
    $('case-select').onchange = event => {selected=event.target.value;history.replaceState(null,'','?case='+selected);render();};
    const r=current.record;
    $('intro').textContent = current.intro || ((current.id.startsWith('manual-') || current.id.startsWith('reproduction-')) ? `Ручной A/B: ${['control','baseline'].includes(r.arm) ? 'исходная инструкция' : 'добавлена политика проверки валюты и диагностических запросов'}. Задача ${r.task_id}, повтор ${r.repeat}. Итог exact grader: ${r.grade.passed ? 'все строки правильны' : 'проверка не пройдена'} (${r.grade.correct_rows}/${r.grade.expected_rows} строк).` : current.id === 'diagnosis' ? '6153 — собственное ожидание модели по данным. Её SQL получил 6930: условие валюты пропущено. Агент повторял диагностику и исчерпал бюджет, не исполнив pipeline.' : 'execute_pipeline сообщил, что структурные проверки пройдены. Модель затем сама заметила валютную ошибку и признала её в отчёте, но итоговую таблицу до конца бюджета не исправила.');
    $('metrics').innerHTML = [[r.model,'Модель в конфигурации'],[r.events.length,'Сохранённых событий'],[r.llm_calls,'Вызовов LLM'],[r.tool_calls,'Вызовов инструментов']].map(([v,l])=>`<div><b>${esc(v)}</b><small>${l}</small></div>`).join('');
    const returned = [...new Set((r.backend_responses || []).map(x => x.model).filter(Boolean))];
    const backendNote = returned.length ? `В ответах провайдера сохранено model: ${returned.map(esc).join(', ')}.` : 'Отдельный идентификатор backend из ответа провайдера в архиве не записан.';
    $('provenance').innerHTML = `<div class="metadata"><p><b>Запись:</b> <code>${esc(current.path)}</code><br><b>SHA-256:</b> <code>${current.sha256}</code><br><b>Размер:</b> ${current.byte_count} bytes · split=${esc(r.split)} · задача ${esc(r.task_id)}</p><p>Название модели сохранено клиентом. ${backendNote}</p><p><b>Контракт:</b> <code>${esc(current.task_path)}</code><br><b>SHA-256:</b> <code>${current.task_sha256}</code><br><b>Fingerprint задачи и источников в записи:</b> <code>${esc(r.task_sha256)}</code></p><p><b>Управляющий код:</b> <code>${esc(data.control_source)}</code><br><b>SHA-256 текущего файла:</b> <code>${data.control_sha256}</code>. Видимость событий поясняется по этому коду; это не сохранённый пакет запроса.</p></div><h3>Конфигурация запуска</h3>${block(r.config)}<p>Модели доступны публичное условие, схема источников и результаты инструментов. Private gold не передаётся: public_task использует список разрешённых полей. Поле grade добавляет внешний оценщик после завершения агента.</p>`;
    $('task').textContent = pretty(current.task);
    $('grade').textContent = pretty(r.grade);
    $('filter').value='all';
    renderEvents();
  }
  $('filter').onchange=renderEvents;
  $('expand').onclick=()=>document.querySelectorAll('#events > details').forEach(e=>e.open=true);
  $('collapse').onclick=()=>document.querySelectorAll('#events details').forEach(e=>e.open=false);
  $('download').onclick=()=>{const bytes=Uint8Array.from(atob(current.raw_base64),c=>c.charCodeAt(0));const u=URL.createObjectURL(new Blob([bytes],{type:'application/octet-stream'}));const a=document.createElement('a');a.href=u;a.download=current.filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),60000);};
  addEventListener('hashchange',revealAnchor);
  render();
})();
