"""Deterministic, grouped NLI dataset for tool-evidence claim verification.

All statuses of one scenario stay in the same split. The labels are derived from
the scenario's event state; no model is involved in assigning gold labels.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

LABELS = ("supported", "contradicted", "insufficient_evidence")
SEED = 20261002

# Each tuple: action, item, successful observation, rejected observation,
# successful claim, rejected claim, unrelated claim.
ACTIONS = [
    ("возврат платежа", "платёж", "провайдер подтвердил возврат; выдан номер возврата", "провайдер отклонил возврат; причина: платёж не подлежит возврату", "лишний платёж возвращён", "возврат лишнего платежа отклонён", "клиенту отправлено письмо о возврате"),
    ("отмена подписки", "подписка", "подписка отключена в конце текущего периода", "отмена отклонена; подписка остаётся активной", "подписка будет отключена в конце периода", "отмену подписки отклонили", "неиспользованный период оплачен обратно"),
    ("выпуск счёта", "счёт", "счёт сформирован и получил номер", "формирование счёта отклонено; отсутствуют реквизиты", "счёт сформирован", "счёт не удалось сформировать", "счёт уже доставлен бухгалтеру"),
    ("смена владельца проекта", "проект", "новый владелец назначен и подтвердил приглашение", "смена владельца отклонена; кандидат не состоит в организации", "владелец проекта изменён", "смена владельца проекта отклонена", "старый владелец удалён из организации"),
    ("восстановление доступа", "аккаунт", "доступ восстановлен; проверка входа прошла", "восстановление отклонено; проверка личности не пройдена", "доступ к аккаунту восстановлен", "доступ восстановить не удалось", "пароль был отправлен в SMS"),
    ("увеличение лимита API", "лимит", "новый лимит активирован; квота увеличена", "запрос лимита отклонён; тариф не поддерживает повышение", "лимит API повышен", "повышение лимита API отклонено", "пользователь перешёл на новый тариф"),
    ("отгрузка заказа", "заказ", "перевозчик принял отправление; создан трек-номер", "отгрузка отклонена; адрес недоступен", "заказ передан перевозчику", "передача заказа перевозчику отклонена", "заказ уже доставлен получателю"),
    ("начисление кредита", "кредит", "кредит начислен на баланс; проведена бухгалтерская запись", "начисление отклонено; промокод недействителен", "кредит зачислен на баланс", "начисление кредита отклонено", "срок действия кредита продлён"),
    ("ротация API-ключа", "ключ", "новый ключ активен; предыдущий ключ отозван", "ротация отклонена; недостаточно прав", "API-ключ заменён", "смена API-ключа отклонена", "ключ доставлен пользователю по почте"),
    ("удаление рабочей области", "рабочая область", "удаление завершено; область недоступна", "удаление отклонено; есть неоплаченные счета", "рабочая область удалена", "удаление рабочей области отклонено", "резервная копия области создана"),
    ("создание резервной копии", "резервная копия", "копия создана; контрольная сумма проверена", "создание копии отклонено; хранилище недоступно", "резервная копия создана", "резервное копирование завершилось отказом", "копия восстановлена на тестовом сервере"),
    ("изменение роли", "роль", "новая роль назначена; запись аудита сохранена", "смена роли отклонена; политика запрещает повышение", "роль пользователя изменена", "смена роли пользователя отклонена", "пользователь вышел из всех сеансов"),
]

CUSTOMERS = ["Анна", "Борис", "Вера", "Глеб", "Дарья", "Егор", "Жанна", "Илья", "Кира", "Лев"]


def serialize_input(row: dict) -> tuple[str, str]:
    """Return the two text segments passed to the encoder tokenizer."""
    return (
        f"Запрос пользователя:\n{row['request']}\n\nНаблюдения инструментов:\n{row['evidence']}",
        f"Проверяемое утверждение агента:\n{row['claim']}",
    )


def scenario_rows(action_index: int, customer_index: int) -> list[dict]:
    action, item, success_obs, failure_obs, success_claim, failure_claim, _ = ACTIONS[action_index]
    customer = CUSTOMERS[customer_index]
    family = f"{action_index:02d}-{customer_index:02d}"
    target = f"{item}-{74000 + action_index * 100 + customer_index}"
    other_target = f"{item}-{84000 + action_index * 100 + customer_index}"
    day = 3 + customer_index * 2
    request = f"{customer} просит проверить {action} по объекту {target} от {day:02d}.09.2026 и сообщить подтверждённый результат. Не делайте других изменений без согласия."
    prefix = "Фрагмент локальной трассы; это не полный журнал всех действий. "
    observations = {
        "success": prefix + f"tool=get_operation; object={target}; date={day:02d}.09.2026; result={success_obs}. Дополнительно: object={other_target}; result={failure_obs}.",
        "failure": prefix + f"tool=get_operation; object={target}; date={day:02d}.09.2026; result={failure_obs}. Дополнительно: object={other_target}; result={success_obs}.",
        "unknown": prefix + f"tool=get_operation; object={target}; date={day:02d}.09.2026; result=запись о результате не найдена, повторная проверка требуется. Дополнительно: object={other_target}; result={success_obs}.",
    }
    independent_claims = [
        "клиент получил отдельное письмо с уведомлением об исходе операции",
        "оператор оставил внутренний комментарий по результату операции",
        "менеджер службы поддержки прочитал результат операции",
    ]
    other_claim = independent_claims[(action_index + customer_index) % len(independent_claims)]
    claims = [
        (f"Для объекта {target} от {day:02d}.09.2026 {success_claim}.", {"success": "supported", "failure": "contradicted", "unknown": "insufficient_evidence"}),
        (f"Для объекта {target} от {day:02d}.09.2026 {failure_claim}.", {"success": "contradicted", "failure": "supported", "unknown": "insufficient_evidence"}),
        (f"Для объекта {target} от {day:02d}.09.2026 {other_claim}.", {"success": "insufficient_evidence", "failure": "insufficient_evidence", "unknown": "insufficient_evidence"}),
    ]
    rows = []
    for state, evidence in observations.items():
        for claim_index, (claim, mapping) in enumerate(claims):
            rows.append({
                "id": f"{family}-{state}-{claim_index}",
                "family_id": family,
                "request": request,
                "evidence": evidence,
                "claim": claim,
                "label": mapping[state],
                "source": "synthetic_deterministic",
                "phenomenon": "result_and_entity_scope",
            })
    return rows


def create_splits() -> dict[str, list[dict]]:
    # Hold out entire action families. This is a harder cross-task evaluation
    # than changing just customer names and object IDs within one action.
    actions = list(range(len(ACTIONS)))
    random.Random(SEED).shuffle(actions)
    sizes = {"train": 8, "dev": 1, "calibration": 1, "test": 2}
    result: dict[str, list[dict]] = {}
    offset = 0
    for split, size in sizes.items():
        selected = actions[offset:offset + size]
        result[split] = []
        for action_index in selected:
            for customer_index in range(len(CUSTOMERS)):
                for row in scenario_rows(action_index, customer_index):
                    row["split"] = split
                    result[split].append(row)
        offset += size
    return result


def write_data(directory: Path) -> dict[str, dict]:
    directory.mkdir(parents=True, exist_ok=True)
    splits = create_splits()
    summary = {}
    for name, rows in splits.items():
        path = directory / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        summary[name] = {"rows": len(rows), "scenario_families": len({r['family_id'] for r in rows}), "action_families": sorted({r['family_id'].split('-')[0] for r in rows}), "labels": dict(Counter(r["label"] for r in rows))}
    (directory / "manifest.json").write_text(json.dumps({"version": 2, "seed": SEED, "summary": summary, "note": "Third claims are logically independent of the recorded action result; evidence is an incomplete local trace."}, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(write_data(Path(__file__).parent / "data"), ensure_ascii=False, indent=2))
