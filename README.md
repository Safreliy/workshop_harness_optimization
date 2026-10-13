# Агент и оптимизация harness

Три входа в материалы:

- **[Презентация](workshop-v2/index.html)** — разбор задач SQL/отчётов, ошибок агента, оценки, ручек harness и мета-поиска. Откройте HTML в браузере; сохранённая [арена](workshop-v2/demo.html) и полные траектории работают без API.
- **[База в ноутбуке](notebooks/01_agent_and_harness.ipynb)** — агентный цикл, инструменты, конфиг и интерфейс мета-оптимизатора. Учебный проход использует SQLite и программную policy; дополнительных сервисов и ключа не требует.
- **[Самостоятельная работа](homework/index.html)** — улучшение настоящего ETL-агента. Откройте [etl_homework.ipynb](homework/etl_homework.ipynb) в Jupyter/VS Code: инструкция, зависимости, подключение модели и сравнение `smoke → train → dev → select → test` находятся в 14 ячейках. Первый проход работает без API. Код для правок — `homework/runtime/harness_lab/`. Python 3.11+.

[ДЗ одним HTML](homework/homework.html) можно отправить отдельно: внутри встроен полный архив с ноутбуком, средой, данными и проверками. Репозиторий для этого не нужен.

## Запустить основной эксперимент SQL/report

Для живой арены и собственного поиска нужны Python 3.11+, Docker с Compose, совместимая с OpenAI модель с tool calling. Из корня репозитория:

```bash
python -m pip install -e ".[notebooks]"
```

Скопируйте `.env.example` в `.env` любым удобным способом. Укажите `BASE_URL`, `MODEL_NAME`, `API_KEY`; proxy и CA необязательны. Для OpenRouter: `BASE_URL=https://openrouter.ai/api/v1`, в `MODEL_NAME` — идентификатор выбранной модели.

```bash
harness-lab db-up
harness-lab doctor
python -m harness_lab.workshop_server --port 8867
```

Откройте `http://127.0.0.1:8867/workshop-v2/`. Модель вызывается только при запуске живого эксперимента в арене. Статические сохранённые результаты можно смотреть без сервера.

В отдельном терминале:

```bash
harness-lab benchmark --config configs/baseline.yaml --split train
harness-lab train --search-config configs/search.yaml --output artifacts/training/my-search
harness-lab eval --candidate artifacts/training/my-search/best_config.yaml
```

Поиск использует train; eval сравнивает выбранную конфигурацию. Для самостоятельного независимого вывода нужен отдельный закрытый test. Измеренная конфигурация занятия сохранена в `configs/optimized.yaml`.

Основной код: `src/harness_lab/agent.py` — агент; `config.py` — ручки; `optimizer.py` — поиск; `graders.py` и `trajectory.py` — оценка; `runner.py` — запуск и агрегация. На слайде «Реализация» есть код и ссылки на исходники.

Для ETL-домашки Docker и эти команды не нужны: весь цикл запускается в её ноутбуке. Ключи остаются локально; `.env` и новые результаты не входят в git.
