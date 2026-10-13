"""Illustrative search regression, executed locally; these are not LLM outputs."""

def search_docs(query, docs, mode="correct"):
    """Return matching active documents; deliberately broken modes teach testing."""
    if mode not in {"missing_archive_filter", "hides_everything", "correct"}:
        raise ValueError(f"Unknown mode: {mode}")
    if mode == "hides_everything":
        return []
    return [doc["id"] for doc in docs
            if query.casefold() in doc["title"].casefold()
            and (mode == "missing_archive_filter" or not doc["archived"])]

ACTIVE_DOCS = [
    {"id": "A1", "title": "VPN: настройка доступа", "archived": False},
    {"id": "A2", "title": "Отпуска", "archived": False},
]
REGRESSION_DOCS = ACTIVE_DOCS + [
    {"id": "OLD", "title": "VPN: устаревшая инструкция", "archived": True},
]

def check(mode):
    happy = search_docs("VPN", ACTIVE_DOCS, mode)
    mixed = search_docs("VPN", REGRESSION_DOCS, mode)
    return {
        "вариант": mode,
        "обычный тест: только активные документы": happy == ["A1"],
        "регрессия: архивный документ исключён": "OLD" not in mixed,
        "регрессия: актуальный результат сохранён": mixed == ["A1"],
    }

def run_demo():
    return [check(mode) for mode in ("missing_archive_filter", "hides_everything", "correct")]

if __name__ == "__main__":
    import json
    rows = run_demo()
    assert [list(row.values())[1:] for row in rows] == [
        [True, False, False], [False, True, False], [True, True, True]]
    print("Учебная регрессия поиска. Локальный Python, без модели.")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
