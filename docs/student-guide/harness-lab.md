# CLI `harness-lab`

Запуск PostgreSQL-агента, оценка результатов и поиск конфигураций harness.

Конспект участника, проверен по коду. Это CLI для SQL/report. В ETL-домашке свой интерфейс `ACTION` внутри ноутбука: его `train` запускает сравнение, а не автоматический поиск. [К путеводителю](README.md).

## Подготовка

Выполняйте команды из корня репозитория:

```bash
python -m pip install -e ".[notebooks]"
```

Для вызовов модели задайте в `.env` параметры `BASE_URL`, `MODEL_NAME`, `API_KEY`. Модель должна поддерживать tool calling. Подключение к PostgreSQL задаёт `PG_DSN`; по умолчанию:

```text
postgresql://workshop:workshop@localhost:55432/workshop
```

## Синтаксис

```text
harness-lab [--env FILE] COMMAND [OPTIONS]
```

`--env FILE` выбирает dotenv-файл; по умолчанию — `.env`. Уже заданные переменные окружения имеют приоритет.

```bash
harness-lab --help
harness-lab train --help
harness-lab --env .env.local doctor
```

## `doctor`

```bash
harness-lab doctor
```

Проверяет настройки модели, доступность `/models`, версию benchmark-данных в PostgreSQL и загрузку train/eval-кейсов.

Показывает, присутствует ли модель в `/models`. Значение `advertised=False` само по себе не считается ошибкой. Генерация ответа и tool calling этой командой не проверяются.

## `db-up`

```bash
harness-lab db-up
```

Выполняет `docker compose up -d --wait`: запускает сервисы и ждёт готовности. PostgreSQL инициализируется при первом запуске с пустым volume.

## `db-reset`

```bash
harness-lab db-reset
```

Выполняет `docker compose down --volumes --remove-orphans`, затем `up -d --wait`.

**Удаляет данные volumes проекта Compose** и создаёт benchmark-базу заново.

## `benchmark`

Оценивает одну конфигурацию на выбранных задачах.

```bash
harness-lab benchmark --config configs/baseline.yaml --split train
```

| Параметр | Назначение | По умолчанию |
|---|---|---|
| `--config FILE` | YAML-конфигурация агента | `configs/baseline.yaml` |
| `--split train\|eval\|all` | Набор задач | `train` |
| `--kinds LIST` | `report`, `sql` или `report,sql` | Оба типа |
| `--limit N` | Первые N выбранных задач; используйте N > 0 | Все задачи |
| `--artifact-root DIR` | Каталог запусков | `artifacts/runs` |

Выводит оценки по задачам и общую сводку: качество результата, траекторию, итоговый score, pass rate, вызовы и токены.

Каждый запуск создаёт отдельный подкаталог с `config.yaml`, `records.jsonl` и `summary.json`.

## `train`

Предлагает и оценивает конфигурации harness на train. Выбирает вариант с максимальным objective; веса модели не изменяет.

```bash
harness-lab train --search-config configs/search.yaml --output artifacts/training/my-search
```

| Параметр | Назначение | По умолчанию |
|---|---|---|
| `--search-config FILE` | YAML-параметры поиска | `configs/search.yaml` |
| `--kinds LIST` | Типы задач: `report`, `sql`, `report,sql` | Оба типа |
| `--limit N` | Первые N train-задач; используйте N > 0 | Все задачи |
| `--budget N` | Лимит trials, включая baseline и восстановленные trials | По параметрам поиска |
| `--output DIR` | Каталог поиска | `artifacts/training/<UTC-время>` |
| `--resume` | Восстановить поиск из каталога `--output` | Выключено |

Основные файлы в каталоге поиска:

- `optimization_trace.json` — журнал оценённых конфигураций;
- `best_config.yaml` — выбранная конфигурация после завершения;
- `best_summary.json` — сводка выбора;
- `runs/` — результаты отдельных оценок.

Для продолжения укажите тот же каталог и совместимые условия эксперимента:

```bash
harness-lab train --search-config configs/search.yaml --output artifacts/training/my-search --resume
```

Если журнал уже существует, запуск без `--resume` завершится ошибкой. Возобновление использует сохранённые trials; прерванный API-запрос не продолжается.

## `eval`

Заново запускает baseline и candidate на одинаковых eval-задачах и показывает разницу score.

```bash
harness-lab eval --candidate artifacts/training/my-search/best_config.yaml
```

| Параметр | Назначение | По умолчанию |
|---|---|---|
| `--baseline FILE` | Исходная конфигурация | `configs/baseline.yaml` |
| `--candidate FILE` | Проверяемая конфигурация | Обязателен |
| `--kinds LIST` | Типы задач: `report`, `sql`, `report,sql` | Оба типа |
| `--limit N` | Первые N eval-задач; используйте N > 0 | Все задачи |
| `--artifact-root DIR` | Каталог результатов | `artifacts/eval` |

Сохраняет оба запуска и `<artifact-root>/latest_comparison.json`. Файл сравнения перезаписывается при следующем успешном eval в том же каталоге.

Eval — повторно используемый набор для разработки. Для независимой итоговой оценки нужен отдельный закрытый test; CLI-команды `test` нет.

## `demo-export`

Собирает JSON для статического просмотра сохранённых eval-запусков.

```bash
harness-lab demo-export --comparison artifacts/eval/latest_comparison.json --candidate artifacts/training/my-search/best_config.yaml
```

| Параметр | Назначение | По умолчанию |
|---|---|---|
| `--benchmark-dir DIR` | Каталог задач | `benchmarks` |
| `--comparison FILE` | Сравнение с путями к запускам | `artifacts/eval/meta_v4/latest_comparison.json` |
| `--baseline FILE` | Исходная конфигурация | `configs/baseline.yaml` |
| `--candidate FILE` | Выбранная конфигурация | `artifacts/training/meta_v4/best_config.yaml` |
| `--output FILE` | Выходной JSON | `presentation/demo-data.json` |

Требует сохранённых артефактов. Новые ответы модели не генерирует.

## `demo`

Запускает HTTP-сервер интерактивного сравнения с возможностью новых запусков агента.

```bash
harness-lab demo --candidate artifacts/training/my-search/best_config.yaml
```

| Параметр | Назначение | По умолчанию |
|---|---|---|
| `--host HOST` | Адрес сервера | `127.0.0.1` |
| `--port PORT` | Порт | `8765` |
| `--benchmark-dir DIR` | Каталог задач | `benchmarks` |
| `--baseline FILE` | Исходная конфигурация | `configs/baseline.yaml` |
| `--candidate FILE` | Выбранная конфигурация | `artifacts/training/meta_v4/best_config.yaml` |
| `--static-dir DIR` | Статические файлы | `presentation/dist` |
| `--artifact-root DIR` | Результаты live-запусков | `artifacts/demo` |

Презентация `workshop-v2` запускается отдельной командой:

```bash
python -m harness_lab.workshop_server --port 8867
```

Адрес: http://127.0.0.1:8867/workshop-v2/

## Порядок эксперимента

После установки и настройки `.env`:

```bash
harness-lab db-up
harness-lab doctor
harness-lab benchmark --config configs/baseline.yaml --split train
harness-lab train --search-config configs/search.yaml --output artifacts/training/my-search
harness-lab eval --candidate artifacts/training/my-search/best_config.yaml
```

`benchmark`, `train`, `eval` и live-запуски demo обращаются к модели и могут быть платными.

## Коды выхода

- `0` — команда выполнена; низкое качество ответов само по себе не означает ошибку CLI.
- `1` — провал проверок doctor или исключение при выполнении.
- `2` — ошибка аргументов CLI или отсутствие выбранных задач в `benchmark`.
- `130` — прерывание через Ctrl+C.

`db-up` и `db-reset` возвращают код завершения Docker Compose.

Источник параметров и поведения: [src/harness_lab/cli.py](../../src/harness_lab/cli.py).
