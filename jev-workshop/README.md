# Jev в реальных задачах

1. Откройте **[jev-workshop.html](jev-workshop.html)** в браузере: самодостаточная презентация, внутри скачиваются оба ноутбука и код.
2. Начните с **[intro_compare.ipynb](intro_compare.ipynb)**: классификация обращений через GPT и Jev, качество, время и фактическая стоимость API.
3. Для основной практики скачайте **[practice/practice.zip](practice/practice.zip)**, распакуйте и откройте `practice.ipynb` в Jupyter или VS Code.

В ноутбуках установка SDK находится в первой ячейке. Нужен ключ OpenRouter; ввод скрытый. Вводный ноутбук также читает `OPENROUTER_API_KEY` из локального `.env`. Ключи не сохраняются в результатах. API-запросы платные. Меняйте тексты и критерии прямо в ячейках.

## Основная практика

- Один запуск агента в учебном сервисе → полный журнал → краткая версия → проверка утверждений Jev.
- Семантическая классификация и Score; точные вычисления проверяет код.
- Jev → порог → подробный judge; видны реальные запросы и ответы.
- Генерация критериев под свой запрос и их непосредственная отправка в Jev.
- Подбор порога на размеченных примерах, проверка на test и challenge, сравнение с локальными моделями.

`practice/data/support_agent_run.json` — исходная переписка модели с учебными инструментами. `practice/data/benchmark.json` — сохранённые данные и предсказания для опыта с порогом. Подключение локальных Qwen/BERT и официального endpoint vLLM/SGLang описано в [practice/local_setup.md](practice/local_setup.md).

## Исходники и обучение

`practice/runtime.py` — клиенты Jev/judge и контур проверки; `support_agent.py` — агентный цикл; `trace_views.py` — получение выжимки. Обучение своего mmBERT: [experiment/README.md](experiment/README.md). Веса в репозиторий не включены; для необязательного локального inference сначала обучите checkpoint.

Для просмотра HTML из исходников:

```sh
python -m http.server 8770 --directory jev-workshop
```

Откройте http://127.0.0.1:8770/v2.html . Самодостаточному HTML сервер не нужен.

## Пересборка

```sh
python jev-workshop/practice/build_notebook.py
python jev-workshop/build_practice_data.py
python jev-workshop/build_standalone.py
```

Первый скрипт создаёт чистый notebook без результатов выполнения. Для упаковки уже выполненного notebook без его перезаписи: из `jev-workshop/practice` выполните `python -c "import build_notebook; build_notebook.package()"`, затем пересоберите standalone HTML. Исходные таблицы эксперимента сохранены для воспроизводимости; старые пилоты и служебные материалы не нужны для запуска.
