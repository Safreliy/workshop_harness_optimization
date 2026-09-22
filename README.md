# PostgreSQL Meta-Harness Lab

Воспроизводимый воркшоп о том, как улучшить агентную систему, не меняя модель. Один и тот же `MODEL_NAME` сначала работает в простом ReAct-harness, затем модель-оптимизатор получает результаты и наблюдаемые траектории, сама предлагает новые конфигурации harness и проверяет гипотезы на детерминированных graders.

В демонстрации используются два раздела данных:

- `train` — задачи, результаты и провальные траектории которых доступны мета-оптимизатору;
- `eval` — повторно запускаемая оценка выбранных конфигураций.

`eval` здесь не является закрытым holdout. Результаты можно изучать и использовать в следующей итерации разработки. Для строгого вывода о переносе в реальном проекте потребуется отдельный, заранее закрытый test.

## Схема

```text
                         ┌─ proposal A ─ graders ─┐
train ─ baseline trace ──┼─ proposal B ─ graders ─┼─ Pareto/beam ─ next round
                         └─ proposal C ─ graders ─┘
                                      │
                                      └─ exact config diffs + failed traces

eval ─ baseline / selected candidates ─ deterministic comparison
```

Мутации не перечислены в `search.yaml`. На каждом раунде модель сама выбирает:

- родительскую конфигурацию из текущего beam;
- механизм изменения и проверяемую гипотезу;
- patch из 1–4 полей типизированной конфигурации.

Один портфель должен содержать разные механизмы, хотя бы одну архитектурную мутацию и не более одной мутации `prompt_patch`. Это ограничение разнообразия поиска, а не заранее записанные решения.

## Задачи и проверка

В наборе 10 train-задач (6 `report`, 4 `sql`) и 7 eval-задач (4 `report`, 3 `sql`).

- `report`: длинные диалоги с аналитиком, поздние исправления, владельцы метрик, часовые пояса, уровень детализации, отсутствующие поля и формулы. Ответ — строгий JSON; grader атомарно проверяет требования без LLM-судьи.
- `sql`: PostgreSQL-диагностика с реальными schema, indexes и `EXPLAIN`. Grader проверяет read-only безопасность, исполнение, точное равенство результатов исходного и оптимизированного SQL, изменение planner cost после предложенных индексов и релевантность индекса.

Предложенные индексы создаются в транзакции и откатываются. Между кейсами база не загрязняется.

Каждый кейс получает две независимые оценки:

```text
combined = 0.75 × output_score + 0.25 × trajectory_score
```

`trajectory_score` проверяет наблюдаемое поведение: порядок schema → EXPLAIN → index trial, лишние и повторные tools, ошибки tools, forced finalization, число LLM-вызовов и бессмысленный доступ к БД в report-задачах. Скрытая chain-of-thought не сохраняется.

## Быстрый старт

Требования: Python 3.11+, Docker Desktop с Compose. В `.env` должны быть `API_KEY`, `BASE_URL`, `MODEL_NAME`; endpoint совместим с OpenAI Chat Completions и function calling.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
harness-lab db-up
harness-lab doctor
```

Проверить baseline:

```powershell
harness-lab benchmark --config configs/baseline.yaml --split train
```

Запустить три раунда по три модельных предложения:

```powershell
harness-lab train --search-config configs/search.yaml --output artifacts/training/workshop
```

Для короткой проверки можно ограничить число конфигураций и кейсов:

```powershell
harness-lab train --limit 4 --budget 4 --output artifacts/training/smoke
```

Сравнить baseline и выбранный harness на eval:

```powershell
harness-lab eval --candidate artifacts/training/workshop/best_config.yaml
```

## Интерактивное демо

Сначала соберите replay-файл из сохранённых eval-прогонов. Он нужен, чтобы опубликованная статическая версия показывала точные ответы и траектории без доступа к ключу модели:

```powershell
harness-lab demo-export
Copy-Item presentation\demo-data.json,presentation\demo.html,presentation\demo.css,presentation\demo.js presentation\dist\
```

Для живого показа запустите локальный сервер после `harness-lab db-up` и `harness-lab doctor`:

```powershell
harness-lab demo
```

Откройте `http://127.0.0.1:8765/demo.html`. Страница автоматически обнаружит локальный backend: кнопка запустит выбранный harness заново на той же модели и PostgreSQL. Без backend она честно переключается в режим воспроизведения сохранённого eval, а не имитирует новый вызов.

В режиме «Свободный чат» можно писать обычным текстом, вставлять SQL прямо в сообщение и продолжать разговор уточнениями — отдельных полей и обязательного формата ролей нет. История хранится в браузере отдельно для стартового и оптимизированного агентов; её можно очистить одной кнопкой. Запрос можно остановить, а через 90 секунд интерфейс завершит ожидание сам. Поскольку у свободного ввода нет эталона, интерфейс не показывает выдуманную оценку правильности: доступны ответ, число вызовов модели, время и фактическая последовательность tool calls.

## Что может менять модель-оптимизатор

| Область | Поля конфигурации | Что меняется |
|---|---|---|
| Инструкции | `prompt_profile`, `prompt_patch` | роль, правила и рецепты |
| Инструменты | `enabled_tools`, `tool_description_profile`, overrides | доступный набор и affordances |
| Маршрут | `routing` | разделение report и SQL |
| Процесс | `workflow`, `review_mode` | ReAct, plan→execute, отдельная проверка |
| Контекст | `context_strategy`, `context_partition`, `max_context_chars` | ledger, изоляция стадий, лимит контекста |
| Бюджет | `max_steps` | максимальное число tool-шагов |

Модель не пишет исполняемый Python. Любой patch проходит Pydantic-валидацию: только известные tools, перечислимые режимы, границы бюджета и длины текста. `temperature` задачи остаётся фиксированной.

## Реальный цикл мета-оптимизации

1. Baseline выполняет train, graders формируют output- и trajectory-сигналы.
2. Модель получает несколько родителей, их конфигурации, метрики, реальные diff’ы и историю принятых и провальных гипотез.
3. Она возвращает портфель новых гипотез с выбранным родителем и patch.
4. Все валидные конфигурации реально запускаются на benchmark.
5. Недоминаемые варианты отбираются по отчётам, SQL, траектории и числу вызовов; beam сохраняет несколько направлений.
6. Следующий раунд строится уже от этого beam.

В `optimization_trace.json` для каждого варианта сохраняются `parent_number`, `operator`, `hypothesis`, `changed_fields`, значения до/после, метрики и статус попадания в beam.

Целевая функция для упорядочивания кандидатов:

```text
mean(train_score) - call_penalty × max(avg_llm_calls - 1, 0)
```

Pareto-отбор дополнительно не позволяет одной средней скрыть компромисс между report, SQL, качеством траектории и стоимостью.

## Ограничения демонстрации

- `EXPLAIN Total Cost` — сигнал планировщика, а не wall-clock latency. Для production нужны повторные `EXPLAIN (ANALYZE, BUFFERS)`, warm/cold-cache протокол и доверительные интервалы.
- Synthetic data показывает механизм, но не заменяет анонимизированный workload и статистики реальной БД.
- Один прогон не оценивает дисперсию модели. Для сильного вывода повторите лидеров 3–5 раз.
- Eval открыт и повторяем. Не называйте его holdout или доказательством обобщения.
- Для production-решения добавьте отдельный закрытый test и откройте его только после фиксации протокола.

## Структура

- `benchmarks/` — train/eval JSONL;
- `db/init/` — schema, synthetic data и намеренно неполные индексы;
- `src/harness_lab/agent.py` — исполняемый harness;
- `src/harness_lab/graders.py` — локальная верификация;
- `src/harness_lab/optimizer.py` — модельный портфельный meta-optimizer;
- `configs/` — baseline и границы поиска;
- `artifacts/` — полные run traces и результаты.
- `src/harness_lab/demo.py` — live API и экспорт безопасного replay-файла;
- `presentation/demo.*` — чат, сравнение метрик и визуализация траектории.
