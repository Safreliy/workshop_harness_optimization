# Обучение собственного классификатора

По `request`, `evidence` и `claim` модель выбирает `supported`, `contradicted` или `insufficient_evidence`. Наблюдения должны подтверждать/опровергать то же событие и объект; отсутствие наблюдений само по себе не доказывает ошибку.

База: `jhu-clsp/mmBERT-base`, revision зафиксирован в `train.py`. Режим `frozen` обучает только голову, `lora` — голову и адаптеры encoder. Данные синтетические, детерминированные: 720 train / 90 dev / 90 calibration / 180 test; split сделан по типам операций. Train меняет веса, dev выбирает эпоху, calibration подбирает температуру, test измеряет итог. Затем notebook отдельно подбирает порог автопринятия на calibration.

Из корня репозитория:

```sh
python -m pip install -r jev-workshop/experiment/requirements.txt
cd jev-workshop/experiment
python train.py --mode frozen --epochs 5 --batch-size 8
python train.py --mode lora --epochs 5 --batch-size 4
```

Для CUDA установите подходящую сборку PyTorch по официальной инструкции pytorch.org. Исходный опыт выполнен на RTX 5070 Ti 16 GiB. Запуск загружает базовые веса из Hugging Face; веса и checkpoint не включены в репозиторий.

Метрики, предсказания и обученный checkpoint записываются в `artifacts/frozen` или `artifacts/lora`. `metrics.json` содержит версии библиотек, SHA256 данных, выбранную эпоху и температуру. Сохранённые измерения исходного опыта находятся в этих же каталогах; при новом обучении используйте отдельную копию каталога эксперимента, чтобы сохранить исходное сравнение.

```sh
python infer.py --checkpoint artifacts/lora/checkpoint --serve --port 8771
```

Локальный `/predict` принимает JSON с `request`, `evidence`, `claim`; возвращает класс, распределение, длительность и признак усечения. Сервер слушает `127.0.0.1`. Можно также загрузить checkpoint через `TrainedEncoderScorer` из `practice/local_models.py`.

Сохранённый test: head accuracy 67,2%, LoRA 88,9%; challenge: 33,3% и 20%. Поэтому высокая точность на шаблонном test не означает перенос на реальные формулировки. Предсказания до и после temperature scaling сохранены отдельно. На занятии эти результаты читаются из компактного `practice/data/benchmark.json`, обучение заново не требуется.

`dataset.py` генерирует данные и сериализует пары текстов; `test_dataset.py` проверяет split и метки; `train.py` обучает, выбирает эпоху и температуру; `infer.py` выполняет inference.
