/* Local-server connection profiles. Secrets travel only in POST bodies, never browser storage. */
(() => {
  const listeners = new Set();
  let externalBusy = false;
  window.WorkshopConnections = {
    get busy() { return externalBusy || [...listeners].some(item => item.busy); },
    setBusy(value) { externalBusy = Boolean(value); listeners.forEach(item => item.updateBusy()); },
  };
  function mount(host, index) {
    const prefix = `connection-${index}`;
    host.classList.add('connection-settings');
    host.innerHTML = `<details class="connection-panel"><summary><span>Подключение модели</span><strong data-active>Загрузка…</strong></summary>
      <p class="connection-help">Для новых live-запросов. Сохранённые результаты не меняются. Профили и секреты хранятся в памяти локального сервера до его перезапуска. Можно сохранить без модели, получить список, выбрать ID и сохранить снова.</p>
      <form autocomplete="off"><fieldset><div class="connection-profile"><label for="${prefix}-profile">Профиль<select id="${prefix}-profile" name="profile"></select></label><button type="button" data-new>Новый профиль</button></div>
      <div class="connection-grid"><label for="${prefix}-name">Название<input id="${prefix}-name" name="name" maxlength="100" required placeholder="Например, локальная модель"></label>
      <label for="${prefix}-provider">Тип подключения<select id="${prefix}-provider" name="provider"><option value="openai_compatible">OpenAI-compatible</option><option value="openrouter">OpenRouter</option><option value="env">Из .env сервера</option></select></label>
      <label for="${prefix}-url">Base URL<input id="${prefix}-url" name="base_url" type="url" placeholder="https://api.example/v1" spellcheck="false"></label>
      <label for="${prefix}-key">API key<input id="${prefix}-key" name="api_key" type="password" autocomplete="new-password" spellcheck="false" placeholder="Введите ключ"></label>
      <label class="connection-model" for="${prefix}-model">Модель<input id="${prefix}-model" name="model" list="${prefix}-models" placeholder="ID модели" spellcheck="false"><datalist id="${prefix}-models"></datalist><select data-model-list aria-label="Полученные модели" hidden></select></label>
      <button class="connection-models" type="button" data-models>Проверить подключение / получить модели</button></div>
      <details class="connection-advanced"><summary>Прокси и TLS</summary><label class="connection-check"><input name="proxy_enabled" type="checkbox"> Использовать прокси для этого профиля</label>
      <label for="${prefix}-proxy">Proxy URL<input id="${prefix}-proxy" name="proxy_url" type="password" autocomplete="new-password" spellcheck="false" placeholder="socks5://user:pass@proxy.example:1080"></label>
      <p class="connection-help">Пустое поле оставляет сохранённый прокси. Выключение удаляет его из профиля. Настройки модели аудитора OpenRouter остаются отдельными; выбранный прокси применяется и к нему.</p>
      <label for="${prefix}-ca">CA-сертификат на сервере<input id="${prefix}-ca" name="model_ca_file" placeholder="Необязательно: путь к .pem" spellcheck="false"></label>
      <label class="connection-check"><input name="model_ca_allow_legacy" type="checkbox"> Совместимость с устаревшим CA (ослабить строгую X.509-проверку)</label></details>
      <div class="connection-actions"><button type="submit" data-save>Сохранить и использовать</button><span data-dirty></span></div></fieldset></form>
      <p class="connection-status" data-status role="status" aria-live="polite"></p></details>`;
    const form = host.querySelector('form'), field = name => form.elements.namedItem(name);
    const status = host.querySelector('[data-status]'), picker = field('profile');
    const state = { profiles: [], selected: null, busy: false, available: false, dirty: false, original: null, updateBusy };
    listeners.add(state);
    function updateBusy() {
      form.querySelector('fieldset').disabled = !state.available || state.busy || externalBusy;
      host.querySelector('[data-models]').disabled = !state.available || state.busy || externalBusy || state.dirty || !picker.value;
      host.querySelector('[data-new]').disabled = !state.available || state.busy || externalBusy;
    }
    function dirty(value = true) { state.dirty = value; host.querySelector('[data-dirty]').textContent = value ? 'Изменения ещё не применены' : ''; updateBusy(); }
    function message(text, error = false) { status.textContent = text; status.classList.toggle('connection-error', error); }
    function notify() {
      const profile = state.profiles.find(p => p.id === state.selected) || null;
      host.querySelector('[data-active]').textContent = profile ? `${profile.name} · ${profile.model || 'модель не выбрана'}` : 'Нет выбранного профиля';
      window.dispatchEvent(new CustomEvent('workshopconnectionchange', { detail: { selected: state.selected, profile } }));
    }
    function populate(profile) {
      state.original = profile || null;
      field('name').value = profile?.name || '';
      field('provider').value = profile?.provider || 'openai_compatible';
      field('base_url').value = profile?.base_url || '';
      field('model').value = profile?.model || '';
      field('api_key').value = ''; field('proxy_url').value = '';
      field('api_key').placeholder = profile?.key_configured ? 'Сохранён; пустое поле оставляет ключ' : 'Введите ключ';
      field('proxy_url').placeholder = profile?.proxy_configured ? 'Сохранён; пустое поле оставляет прокси' : 'socks5://user:pass@proxy.example:1080';
      field('proxy_enabled').checked = Boolean(profile?.proxy_enabled);
      field('model_ca_file').value = profile?.model_ca_file || '';
      field('model_ca_allow_legacy').checked = Boolean(profile?.model_ca_allow_legacy);
      host.querySelector('datalist').replaceChildren(); host.querySelector('[data-model-list]').hidden = true;
      providerFields(); dirty(!profile);
    }
    function providerFields() {
      const env = field('provider').value === 'env';
      field('base_url').readOnly = env; field('api_key').disabled = env;
      field('proxy_url').disabled = !field('proxy_enabled').checked;
      if (env) field('api_key').placeholder = 'Используется ключ из .env сервера';
    }
    function redact(text) {
      let result = String(text || 'Ошибка подключения');
      for (const name of ['api_key', 'proxy_url']) { const secret = field(name).value; if (secret) result = result.split(secret).join('[скрыто]'); }
      return result.replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1[скрыто]@').slice(0, 320);
    }
    async function request(body) {
      const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 60000);
      try {
        const response = await fetch('/api/connections', { method: body ? 'POST' : 'GET', cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}), signal: controller.signal });
        let payload; try { payload = await response.json(); } catch { throw new Error('Нужен локальный сервер с /api/connections.'); }
        if (!response.ok) throw new Error(redact(payload.message || payload.error || payload.detail || `HTTP ${response.status}`));
        return payload;
      } finally { clearTimeout(timer); }
    }
    async function refresh(emit = true) {
      const payload = await request();
      state.profiles = Array.isArray(payload.profiles) ? payload.profiles : [];
      state.selected = typeof payload.selected === 'object' ? payload.selected?.id : payload.selected;
      picker.replaceChildren(); const fresh = document.createElement('option'); fresh.value = ''; fresh.textContent = 'Новый профиль'; picker.append(fresh);
      state.profiles.forEach(p => { const o = document.createElement('option'); o.value = p.id; o.textContent = p.name; picker.append(o); });
      picker.value = state.selected || '';
      state.available = true; populate(state.profiles.find(p => p.id === state.selected));
      if (emit) notify();
    }
    async function operation(action) {
      if (state.busy || externalBusy) return;
      state.busy = true; updateBusy(); window.dispatchEvent(new CustomEvent('workshopconnectionbusy', { detail: true }));
      try { await action(); }
      catch (error) { message(error.name === 'AbortError' ? 'Запрос не завершился за 60 секунд.' : redact(error.message), true); }
      finally { state.busy = false; updateBusy(); window.dispatchEvent(new CustomEvent('workshopconnectionbusy', { detail: false })); }
    }
    host.querySelector('[data-new]').onclick = () => { picker.value = ''; populate(null); message('Заполните подключение и сохраните профиль.'); field('name').focus(); };
    picker.onchange = () => { if (!picker.value) { populate(null); message('Заполните новый профиль.'); return; } return operation(async () => { await request({ action: 'select', profile_id: picker.value }); await refresh(); message('Профиль выбран для следующих live-запросов.'); }); };
    form.addEventListener('input', () => dirty());
    field('provider').onchange = () => {
      const provider = field('provider').value;
      picker.value = ''; state.original = null;
      field('api_key').value = ''; field('api_key').placeholder = 'Введите ключ нового подключения';
      field('model_ca_file').value = ''; field('model_ca_allow_legacy').checked = false;
      if (provider === 'openrouter') { field('name').value = 'OpenRouter'; field('base_url').value = 'https://openrouter.ai/api/v1'; field('model').value = ''; }
      else if (provider === 'env') {
        const env = state.profiles.find(p => p.provider === 'env');
        field('name').value = 'Из .env'; field('base_url').value = env?.base_url || ''; field('model').value = env?.model || '';
        field('model_ca_file').value = env?.model_ca_file || ''; field('model_ca_allow_legacy').checked = Boolean(env?.model_ca_allow_legacy);
      } else { field('name').value = 'Новое подключение'; field('base_url').value = ''; field('model').value = ''; }
      providerFields(); dirty(); message('Шаблон создаёт новый профиль. Заполните поля и сохраните.');
    };
    field('proxy_enabled').onchange = providerFields;
    field('base_url').oninput = () => {
      if (state.original && field('base_url').value !== state.original.base_url) field('api_key').placeholder = 'Новый адрес: введите ключ заново';
    };
    form.onsubmit = event => {
      event.preventDefault();
      const provider = field('provider').value, base_url = field('base_url').value.trim();
      if (provider !== 'env') {
        try { const url = new URL(base_url); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error(); }
        catch { message('Укажите HTTP(S) Base URL без логина и пароля внутри адреса.', true); return; }
        if (state.original && base_url !== state.original.base_url && !field('api_key').value) { message('Адрес изменён: введите ключ заново. Сохранённый ключ не переносится на другой endpoint.', true); return; }
      }
      operation(async () => {
        message('Сохраняем подключение…');
        const body = { action: 'save', profile_id: picker.value || undefined, name: field('name').value.trim(), provider, base_url, model: field('model').value.trim(), api_key: field('api_key').value, proxy_enabled: field('proxy_enabled').checked, proxy_url: field('proxy_url').value, model_ca_file: field('model_ca_file').value.trim(), model_ca_allow_legacy: field('model_ca_allow_legacy').checked };
        await request(body); await refresh(); message('Сохранено и выбрано. Получите список моделей или запускайте live-запрос.');
      });
    };
    host.querySelector('[data-models]').onclick = () => operation(async () => {
      message('Получаем модели выбранного подключения…');
      const result = await request({ action: 'models' });
      const models = [...new Set((result.models || []).filter(m => typeof m === 'string'))].sort();
      const list = host.querySelector('datalist'); list.replaceChildren();
      const menu = host.querySelector('[data-model-list]'); menu.replaceChildren(); const prompt = document.createElement('option'); prompt.value = ''; prompt.textContent = 'Выберите полученную модель'; menu.append(prompt); models.forEach(model => { const o = document.createElement('option'); o.value = model; list.append(o); const choice = document.createElement('option'); choice.value = model; choice.textContent = model; menu.append(choice); }); menu.hidden = !models.length; menu.onchange = () => { if (menu.value) { field('model').value = menu.value; dirty(); } };
      message(models.length ? `Получено моделей: ${models.length}. Выберите ID в поле «Модель» и сохраните.` : 'Сервер вернул пустой список. Можно ввести ID модели вручную.');
    });
    updateBusy();
    operation(async () => { await refresh(); message('Готово. Для списка моделей сначала сохраните изменения.'); }).then(() => {
      if (!state.available) { host.querySelector('[data-active]').textContent = 'Нужен локальный сервер'; message('Подключения доступны через локальный сервер. Сохранённые демо продолжают работать.'); }
    });
  }
  function start() { document.querySelectorAll('[data-connection-settings]').forEach(mount); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
