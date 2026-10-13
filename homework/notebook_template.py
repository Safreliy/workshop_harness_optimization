"""Maintainer source for the compact homework notebook."""
from textwrap import dedent


def cell(kind, text):
    item = {"cell_type": kind, "metadata": {}, "source": dedent(text).strip().splitlines(keepends=True)}
    if kind == "code":
        item.update(execution_count=None, outputs=[])
    return item


def make_notebook():
    cells = [
        cell("markdown", """
        # ДЗ: улучшить ETL-агента

        **Задача.** Из выгрузок заказов, платежей и возвратов построить сверенную таблицу: одна строка на последний заказ. Устранить повторные доставки; сначала выбрать последнюю ревизию события, затем проверить статус и валюту заказа. Суммы привести к целым копейкам.

        **Результат агента:** исполненный SQL и таблица `order_id, customer_id, currency, order_amount_cents, paid_cents, refunded_cents, net_cents, status`. Объяснение «всё готово» не заменяет таблицу.

        **Зачем:** научиться отличать ошибку модели от слабой проверки и выбирать изменение по измеренному эффекту. Начинаем с готового агента; меняем инструкции, инструменты или цикл. LangGraph/LangChain тоже допустимы.

        **Ваш путь:** посмотреть цикл → изменить `configs/candidate.json` → проверить гипотезу локально → запустить контроль и кандидат на одинаковых train/dev → зафиксировать выбор → проверить на новом test.

        **Сдать:** гипотезу, конфиг/diff, пойманный контрпример, результаты и регрессии. Отрицательный результат тоже принимается. Если контроль уже проходит 100% случаев, проверяйте снижение вызовов/токенов при сохранении качества на повторных запусках обоих вариантов; небольшая разница одного прогона ещё не доказывает эффект, нулевой результат допустим.

        Run All по умолчанию работает **без API** и ничего не перезаписывает. Для новых ответов модели включите `RUN_LIVE` в ячейке запуска. [Полное задание](index.html) · [Дополнительные диагностики, 44 ячейки](_support/etl_diagnostics.ipynb)
        """),
        cell("code", """
        import json, sys, subprocess
        from contextlib import closing
        from pathlib import Path
        from datetime import datetime
        HOMEWORK = Path.cwd()
        assert (HOMEWORK / "_support/notebook_support.py").is_file(), "Откройте ноутбук из распакованной папки homework"
        HOMEWORK = HOMEWORK.resolve()
        sys.path.insert(0, str(HOMEWORK / "_support"))
        from notebook_support import table, load_evidence, diagnostic_db, select
        from experiment import prepare, save_config, connect, Experiment
        INSTALL_DEPS = False  # True один раз для живого агента; установка в текущее ядро.
        prepare(HOMEWORK, install=INSTALL_DEPS)
        evidence = load_evidence()
        CONTROL = HOMEWORK / "configs/control.json"
        CANDIDATE = HOMEWORK / "configs/candidate.json"
        print("Конфиг для редактирования:", CANDIDATE)
        """),
        cell("markdown", """
        ## 1 · Где агент и что меняем

        Верхнеуровневая схема настоящего `run_agent`; обработка ошибок и дополнительные режимы опущены:

        ```python
        messages = [system(config.prompt_patch), public_task(task)]
        tools = env.tool_schemas.filtered(config.tools)
        for step in range(config.max_steps):
            turn = client.complete(messages, tools=tools)
            messages.append(turn.as_message())
            if not turn.tool_calls:
                break
            for call in turn.tool_calls:
                observation = env.call(call.name, json.loads(call.arguments))
                messages.append(tool_result(call.id, observation))
        # Оценка отдельно, эталон не входит в messages:
        grade = env.grade(private_reference)
        ```

        Модель выбирает действие; Python исполняет инструмент и возвращает наблюдение. `max_steps` ограничивает число модельных ходов, в одном ходе могут быть несколько tools. После лимита настоящий код ещё запрашивает финальный отчёт; он не создаёт отсутствующий pipeline.

        **Код в вашем архиве:** [runtime/harness_lab/etl_agent.py](runtime/harness_lab/etl_agent.py) → `run_agent` / `ETLHarnessConfig`; [etl_env.py](runtime/harness_lab/etl_env.py) → инструменты / grader; [etl_optimizer.py](runtime/harness_lab/etl_optimizer.py) → мета-поиск. После изменения кода перезапустите ядро и начните новый `EXPERIMENT`.

        У пары общий код и разные конфиги. Для сравнения двух циклов сохраните исходный и новый режим в коде и переключайте их параметром конфига.

        `react` — свободный выбор инструментов; `profile_first` — сбор профилей и план перед ReAct. `review="public_checks"` добавляет программную проверку и возможность исправления. Это параметры ETL; `tool_description_profile` и `requirements_ledger` относятся к SQL/report-агенту.
        """),
        cell("code", """
        # Для первого сохранения: выполните setup с INSTALL_DEPS=True.
        # Читаем candidate.json. Run All не сбрасывает ваши изменения.
        control = json.loads(CONTROL.read_text(encoding="utf-8"))
        candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
        MY_HYPOTHESIS = ""  # Причина провала и ожидаемый эффект изменения.
        CHANGES = {}        # Например: {"prompt_patch": candidate["prompt_patch"] + "\\nВаше правило"}
        SAVE_CANDIDATE = False  # True только когда готовы сохранить изменения в candidate.json.
        proposed = {**candidate, **CHANGES}
        if SAVE_CANDIDATE:
            save_config(CANDIDATE, proposed)
            candidate = proposed
        table([{"Поле": key, "Контроль": control.get(key), "Ваш кандидат": proposed.get(key)}
               for key in proposed if proposed.get(key) != control.get(key)] or
              [{"Поле": "Изменений пока нет", "Контроль": control["name"], "Ваш кандидат": candidate["name"]}])
        print("Запуск читает сохранённый candidate.json; CHANGES без SAVE_CANDIDATE не применяются.")
        """),
        cell("markdown", """
        ## 2 · Проверка гипотезы без модели

        В реальном запуске модель получила 6930 вместо своего ручного ожидания 6153 и подозревала SQLite. Ниже исполняется её SQL по тем же публичным данным. Посмотрите строки, вошедшие в сумму: что именно добавило 777?

        **Упражнение.** В первой ячейке изменяйте `QUERY`, чтобы увидеть строки, вошедшие в сумму заказа `00002`. Во второй заполните `FILTER`: дополнительное SQL-условие перед группировкой должно исключить неподходящие события. Запустите обе ячейки повторно. Критерий: сумма оплаты заказа `00002` равна **6153**, а сообщение показывает «Локальная проверка пройдена». Пустой `FILTER` специально оставляет исходную ошибку; Run All покажет её без исключения.

        Это локальная проверка конкретного запроса. Для общего pipeline событие должно сравниваться с валютой каждого заказа; после упражнения перенесите правило в свой кандидат и измерьте новые ответы модели.

        <details><summary>Подсказка</summary><p>Сравните колонку <code>cur</code> у двух событий с валютой заказа. Достаточно ли проверки статуса и суммы?</p></details>
        <details><summary>Проверить свой ответ</summary><p>Для этого локального USD-заказа: <code>FILTER = "AND cur='USD'"</code>. Платёж GBP на 777 должен исчезнуть из суммы. Такой литерал не подходит для общего pipeline: там нужна валюта соответствующего последнего заказа.</p></details>

        [Полная траектория](_support/trajectory.html?case=diagnosis#event-31). Это диагностика SQL, а не новый запуск LLM.
        """),
        cell("code", """
        failure = evidence["failure"]
        saved_sql = next(e["arguments"]["sql"] for e in failure["events"] if e["index"] == 31)
        cte = saved_sql.rsplit("SELECT oid, SUM(amt)", 1)[0]
        QUERY = cte + ("SELECT eid, oid, amt, cur, st FROM pay_dedup\\n"
                     "WHERE st='settled' AND amt IS NOT NULL AND oid='00002'")
        with closing(diagnostic_db(failure["source_rows"])) as db:
            contributors = select(db, QUERY)
        table(contributors)
        print("Сумма строк:", sum(row["amt"] for row in contributors))
        """),
        cell("code", """
        # Ваше упражнение: впишите дополнительное условие перед GROUP BY.
        FILTER = ""  # Пустое условие воспроизводит исходную ошибку.
        LOCAL_FIX = saved_sql.replace("GROUP BY oid", FILTER + " GROUP BY oid")
        with closing(diagnostic_db(failure["source_rows"])) as db:
            local_rows = select(db, LOCAL_FIX)
        table(local_rows)
        paid = next(row["paid"] for row in local_rows if row["oid"] == "00002")
        print("Оплата 00002:", paid, "· ожидается 6153")
        print("Локальная проверка пройдена" if paid == 6153 else "Пока не исправлено: заполните FILTER и повторите ячейку.")
        print("Исправление SQL здесь не доказывает, что prompt-патч исправит будущие ответы агента.")
        """),
        cell("code", """
        # Другой реальный запуск: публичный валидатор зелёный, но денежные суммы неверны.
        # Можно редактировать _support/example_pipeline.sql и повторять эту ячейку.
        sample = evidence["false_success"]
        PIPELINE_SQL = (HOMEWORK / "_support/example_pipeline.sql").read_text(encoding="utf-8")
        def replay(source_rows):
            with closing(diagnostic_db(source_rows)) as db:
                return select(db, PIPELINE_SQL)
        def canonical(rows):
            return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
        actual = replay(sample["source_rows"])
        expected = sample["expected_rows"]  # Только у локальной оценки, не в агентном контексте.
        mismatches = [row for row in actual if json.dumps(row, sort_keys=True, ensure_ascii=False) not in canonical(expected)]
        permutation = {name: list(reversed(rows)) for name, rows in sample["source_rows"].items()}
        table([{"Проверка": "Публичный валидатор, сохранённый результат", "Пройдена": sample["public_passed"]},
               {"Проверка": "Точная таблица, исполнено сейчас", "Пройдена": canonical(actual) == canonical(expected)},
               {"Проверка": "Повторное исполнение", "Пройдена": canonical(actual) == canonical(replay(sample["source_rows"]))},
               {"Проверка": "Перестановка источников", "Пройдена": canonical(actual) == canonical(replay(permutation))}])
        print("Неверных строк:", len(mismatches)); table(mismatches[:2])
        # Эта сверка сравнивает всю учебную таблицу. Новые запуски используют env.grade().
        """),
        cell("markdown", """
        ## 3 · От локальной правки к эксперименту

        Локальный SELECT объясняет ошибку. Чтобы проверить harness, нужны **новые** ответы агента. Архив ниже показывает, что инструкция исполнять pipeline раньше помогла, а более сложная ручная обвязка оказалась слабее. Это ориентир; редактирование вашего JSON не меняет архивные числа.
        """),
        cell("code", """
        bench = json.loads((HOMEWORK / "_support/evidence/etl-benchmark.json").read_text(encoding="utf-8"))
        table([{"Harness": item["id"], "Успех": f'{item["passed"]}/{item["executions"]}',
                "Шаги": item["config"]["max_steps"], "Workflow": item["config"]["workflow"],
                "LLM-вызовы/запуск": round(item["metrics"]["mean_llm_calls"], 1)} for item in bench["configs"]])
        print("Архив:", bench["unique_tasks"], "задачи ×", bench["repeats_per_task"], "повтора; модель:", bench["model"])
        """),
        cell("markdown", """
        ## 4 · Новые запуски: контроль и ваш кандидат

        Всё находится в распакованном архиве. Для живого агента один раз выполните первую ячейку с `INSTALL_DEPS=True`, затем верните `False`. Нужна обычная модель с **tool calling**; Jev — отдельный оценщик.

        Введите URL и имя модели ниже. `CONNECT=True` запросит ключ скрытым вводом; ключ остаётся в памяти. Затем в следующей ячейке включите `RUN_LIVE=True` и последовательно меняйте `ACTION`, повторяя **только эту ячейку**:

        | ACTION | Что выполняется |
        |---|---|
        | `smoke` | 3 train-случая × 2 варианта; проверить подключение и артефакты |
        | `train` | 24 случая × 2; изучить ошибки и расходы |
        | `dev` | 12 случаев × 2; сравнить варианты для выбора |
        | `select` | Зафиксировать выбор и оба конфига; без API |
        | `test` | 12 новых случаев × контроль и выбранный вариант |

        Полная серия — **96 платных эпизодов** сверх 6 smoke; это не число API-вызовов. `SELECT="auto"` выбирает по dev: успех → score → меньше вызовов → меньше токенов; при равенстве контроль. Можно явно выбрать `control` или `candidate` до test. Данные фиксированы: **24 train / 12 dev / 12 test**. Изменили конфиг, код или модель — задайте новое имя `EXPERIMENT` и повторите оба варианта. Уже выполненная стадия не перезаписывается.
        """),
        cell("code", """
        from getpass import getpass
        CONNECT = False
        BASE_URL = "https://openrouter.ai/api/v1"  # Или URL вашего OpenAI-like API.
        MODEL = ""  # Идентификатор модели с tools.
        USE_PROXY = False
        CA_FILE = ""  # Необязательно: путь к вашему .pem внутри распакованной папки.
        client = None
        if CONNECT:
            key = getpass("API key: ")
            proxy = getpass("Proxy URL: ") if USE_PROXY else ""
            try:
                client = connect(base_url=BASE_URL, model=MODEL, api_key=key, proxy=proxy,
                                 ca_file=str(HOMEWORK / CA_FILE) if CA_FILE else "")
            finally:
                key = proxy = ""  # Подключение хранит их только в памяти.
            print("Модель:", client.model_name, "· подключение настроено; запросов пока не было.")
        else:
            print("Для новых запусков укажите MODEL и включите CONNECT.")
        """),
        cell("code", """
        RUN_LIVE = False  # True разрешает платные запросы для выбранной стадии.
        ACTION = "smoke"  # Затем: train → dev → select → test.
        EXPERIMENT = "attempt-1"  # Новая гипотеза или повтор: новое имя.
        SELECT = "auto"  # Только для select: auto / control / candidate.
        DATA_SEED = 2026101301
        experiment = Experiment(HOMEWORK, EXPERIMENT, client, DATA_SEED)
        if RUN_LIVE:
            experiment.execute(ACTION, selected=SELECT)
        else:
            print("Новые запросы выключены. Показаны только результаты этого EXPERIMENT, если они есть.")
        table(experiment.metrics())
        table(experiment.outcomes(ACTION))  # Парные исходы и регрессии.
        print("SQL, CSV, журнал и сравнения:", experiment.root)
        """),
        cell("markdown", """
        ## 5 · Что сдавать

        1. Причина: вход, вызов инструмента и наблюдение, которое её подтвердило.
        2. Изменение: `configs/control.json`, `configs/candidate.json` и diff кода, если меняли цикл.
        3. Проверка: контрпример ловит ошибку, корректный результат проходит.
        4. Эксперимент: одинаковые данные/модель, успехи, регрессии, вызовы, токены; папка `results/ваш-EXPERIMENT`.
        5. Вывод: почему изменение помогло/навредило и что проверять дальше.

        **Train** — диагностика и предложения; **dev/eval** — выбор; **test** — проверка после фиксации. Веса LLM здесь не обучаются. Если API недоступен, сдайте воспроизводимую диагностику и конфиг; эффект на агенте отметьте как неизмеренный.

        Дополнительные 44 ячейки с отрицательными опытами, регрессией и переносом на свою задачу: [etl_diagnostics.ipynb](_support/etl_diagnostics.ipynb).
        """),
    ]
    return {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.11"}}, "nbformat": 4, "nbformat_minor": 5}
