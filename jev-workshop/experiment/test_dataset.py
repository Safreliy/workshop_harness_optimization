from __future__ import annotations

from collections import defaultdict

from dataset import LABELS, create_splits, serialize_input


def test_action_families_do_not_cross_splits():
    splits = create_splits()
    seen = set()
    for rows in splits.values():
        actions = {row["family_id"].split("-")[0] for row in rows}
        assert not (actions & seen)
        seen |= actions
        assert all(row["label"] in LABELS for row in rows)


def test_same_claim_changes_label_with_evidence():
    grouped = defaultdict(list)
    for rows in create_splits().values():
        for row in rows:
            grouped[(row["family_id"], row["claim"])].append(row)
    assert grouped
    for group in grouped.values():
        assert len(group) == 3
        assert len({row["evidence"] for row in group}) == 3
        assert len({row["label"] for row in group}) in (1, 3)
        assert all(row["request"] in serialize_input(row)[0] for row in group)


def test_independent_claim_is_never_implied_or_refuted_by_action_result():
    for rows in create_splits().values():
        for row in rows:
            if row["id"].endswith("-2"):
                assert row["label"] == "insufficient_evidence"
