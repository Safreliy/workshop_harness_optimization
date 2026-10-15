# Маленькая модель: официальный decision API

Для vLLM и SGLang используется тот же официальный `TypeSafeClient.system_one`, что и для Jev: меняются адрес и модель. `Choice` — классификация, `Noul` — вероятность ответа «да», `Score` — оценка по заданной шкале. `local_models.py` оставлен для CPU-демонстрации и обученных encoder-моделей.

## Что именно сравниваем

Qwen3-0.6B — обычная обученная генеративная модель. Вместо длинного ответа читаются logits допустимых меток и нормируются softmax по этим меткам. CPU-демонстрация ниже использует `A/B`: `p_yes = softmax(logit_A, logit_B)[0]`; встроенный `Noul` endpoint использует `yes/no`. В vLLM это выполняет встроенный decision endpoint: один read на вопрос. Шаблон и метки строит сервер. **Скор не является вероятностью правильности и не становится откалиброванным от выбора режима сервера.** `label_mass` показывает, сколько исходной вероятности приходится на предложенные метки; условные `0.99` могут сопровождаться очень малой массой меток.

NLI (Natural Language Inference) — задача проверки, следует ли утверждение из предоставленных сведений: подтверждено, опровергнуто или данных недостаточно. Это не Natural Language Interface.

Jev, готовый encoder для такой проверки, наш обученный encoder и Qwen с чтением logits имеют разное обучение. Режим инференса отвечает за способ выдачи, а знания и калибровка приходят из checkpoint и данных. Классический encoder для честного сравнения должен уже иметь обученную NLI-голову; случайная голова `AutoModelForSequenceClassification` над bare BERT не служит baseline.

## Windows / CPU: без сервера

В отдельной среде или ячейке ноутбука установите `torch` и `transformers>=4.51` только если их ещё нет. В первый раз загрузятся веса модели (~1.2 GB). [Model card Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B).

```python
from local_models import TransformersOneTokenScorer

qwen = TransformersOneTokenScorer(device="cpu")  # или device="cuda"
state = {"evidence": "validator: currency_filter failed", "claim": "Все проверки прошли"}
qwen.score(state, "Подтверждается ли claim данными evidence?")
```

Модель ничего не генерирует: `model(..., logits_to_keep=1)` и softmax по двум токенам. Проверяется, что метки — отдельные токены именно после chat template; `enable_thinking=False` закрывает блок рассуждения. Длинный вход вызывает ошибку, а не скрытое усечение.

## Linux / WSL2 / GPU: официальный vLLM

В актуальном официальном репозитории есть встроенный `/v1/systemone`, включая `Qwen/Qwen3-0.6B`. Отдельный сервер-посредник, токенизатор на клиенте и собственная логика чтения logprobs для этого не нужны. Поддерживаемая модель запускается обычным `vllm serve`; специального decision flag нет.

Нужен Linux/WSL2 и совместимая GPU. Для нового endpoint используйте официальный nightly: доступность в `v0.31.0` здесь не подтверждена. Установка в отдельной среде по [официальной инструкции](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/#install-the-latest-code):

```bash
uv venv --python 3.12 --seed
source .venv/bin/activate
uv pip install -U vllm --torch-backend=auto --extra-index-url https://wheels.vllm.ai/nightly
vllm serve Qwen/Qwen3-0.6B --host 127.0.0.1 --port 8782 --max-model-len 4096 --gpu-memory-utilization 0.35 --logprobs-mode raw_logprobs --enable-prefix-caching
```

В ноутбуке SDK уже установлен:

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

local = TypeSafeClient(
    base_url="http://127.0.0.1:8782/v1", api_key="unused",
    model="Qwen/Qwen3-0.6B", timeout=60,
)
result = local.system_one(
    "Опять списали деньги. Третий раз пишу, никто не помогает!",
    {
        "route": Choice(instructions="Кому передать обращение?", criteria={
            "billing": "платежи и возвраты", "technical": "сбои программы", "other": "другое",
        }),
        "urgent": Noul(instructions="Нужен ли приоритетный разбор повторной проблемы?"),
        "frustration": Score(instructions="Насколько клиент раздражён?", criteria=[
            "спокоен", "недоволен", "очень раздражён",
        ]),
    },
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
for name, answer in result.answers.items():
    print(name, answer)
```

`Score` возвращает среднее значение индекса уровня: для трёх уровней диапазон 0–2. При необходимости делите на 2 для шкалы 0–1. Это оценка по заданным уровням, а не вероятность правильности.

Сервер строит prompt и читает оценки меток первого токена ответа: `A…Z` для Choice, `yes/no` для Noul, `0…9` для Score. Softmax по предложенным меткам даёт распределение; `diagnostics.label_mass` показывает их суммарную вероятность по полному словарю. Эти значения не обучают и не калибруют Qwen. Проверяйте порог на своих размеченных примерах. Для воспроизводимого эксперимента сохраните версии vLLM, checkpoint и параметры запуска; nightly меняется.

Источники: [официальная реализация и описание endpoint](https://github.com/vllm-project/vllm/blob/6ecb39eaa7c55b26afd39b76a87b44f33053bdff/docs/models/structured_decisions_models.md), [пример PR #57250](https://github.com/vllm-project/vllm/pull/57250). PR #57250 был ранним путём для DiffusionGemma с фиксированными canvas-слотами; актуальный встроенный endpoint также поддерживает next-token readout Qwen.

Другой официальный интерфейс vLLM — `/v1/decisions` через `OpenAI().decisions.create`; для него документация требует OpenAI SDK ≥ 3.26.0. Здесь достаточно уже установленного TypeSafe SDK.

## SGLang: тот же SDK

[SGLang decision models](https://docs.sglang.io/docs/supported-models/decision_models) также предоставляет совместимый `/v1/systemone`. Запустите сервер из версии/nightly, содержащей endpoint, и замените только URL:

```bash
python -m sglang.launch_server --model-path Qwen/Qwen3-0.6B --host 127.0.0.1 --port 8783
```

```python
local = TypeSafeClient(base_url="http://127.0.0.1:8783/v1", api_key="unused",
                       model="Qwen/Qwen3-0.6B", timeout=60)
result = local.system_one("Клиент ждёт ответ три дня и не может войти в аккаунт", {
    "urgent": Noul(instructions="Нужно ли передать обращение на приоритетный разбор?")
})
print(result.answers)
```

vLLM и SGLang могут по-разному строить prompt и определять `confidence`. При смене backend измерьте качество и подберите порог заново. Совместимость интерфейса не означает совпадение оценок.

## Голова классификации — другой режим

`--convert classify` и pooling `/classify` предназначены для sequence classification. Их не нужно добавлять к описанному Qwen decision-серверу. Применяйте этот путь к checkpoint с обученной головой или с документированным преобразованием, например reranker. Создание случайной головы над обычной генеративной моделью не даёт обученного классификатора. [vLLM classification](https://docs.vllm.ai/en/stable/models/pooling_models/classify/).

## Encoder: готовый NLI и свой обученный checkpoint

Готовый [multilingual MiniLMv2 NLI](https://huggingface.co/MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli) — 6-слойный encoder с головой, обученной на MNLI/XNLI, включая русский. Это encoder семейства XLM-R/MiniLM, а не исходный BERT. Он получает пару: запрос + свидетельства / проверяемое утверждение. Ему не передают произвольную инструкцию или динамические критерии.

```python
from local_models import PretrainedNLIScorer, TrainedNLIScorer
state = {"request": "Проверьте возврат", "evidence": "Возврат отклонён", "claim": "Деньги возвращены"}
classic = PretrainedNLIScorer(device="cpu")
classic.classify(state)
ours = TrainedNLIScorer("checkpoints/lora", device="cpu")
ours.classify(state)
```

Для своего checkpoint нужны `experiment_config.json`, `temperature.json`, tokenizer и обученные веса головы (`frozen`) или адаптера (`lora`). LoRA требует `peft`. Базовые веса mmBERT загружаются отдельно по зафиксированной revision из config. Сериализация входа повторяет обучение; длинный вход явно отклоняется. Температура из checkpoint подобрана на синтетическом calibration split; она не гарантирует калибровку на новых доменах.

## Фактическая проверка в этом комплекте

`data/local_smoke.json` и `data/pretrained_nli_smoke.json` хранят реальные входы, версии и результаты CPU forward pass на одинаковых 12 синтетических примерах проверки утверждений. Qwen получил 4/12, готовый MiniLM — 3/12. У Qwen выражено предпочтение `supported`; MiniLM также ошибается уверенно, например путает результат для запрошенного объекта с соседним объектом в журнале. Режим чтения logits не обучает модель понимать прикладные журналы.

Это небольшая иллюстрация, не полный benchmark и не рейтинг моделей. Время CPU этих новых прогонов нельзя прямо сравнивать с сохранёнными GPU/API-прогонами. vLLM/SGLang команды здесь являются рецептом запуска: работа GPU-сервера в этой среде не проверена. Учебный GPU-хост не ответил на SSH, локальная машина — Windows без CUDA. Свой mmBERT loader реализован, но локальный live forward пока не проверен: базовые веса не кэшированы. Порог не подобран и точность не откалибрована; используйте dev, затем фиксированный test.
