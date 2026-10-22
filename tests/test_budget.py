"""礼物预算：默认一分钱都花不出去。"""

from __future__ import annotations

from live_deck.fish.budget import GiftBudget


def test_zero_budget_blocks_everything(tmp_path):
    budget = GiftBudget(tmp_path / "ledger.json", 0)
    assert not budget.can_spend(1)
    assert not budget.can_spend(100)


def test_daily_limit(tmp_path):
    ledger = tmp_path / "ledger.json"
    budget = GiftBudget(ledger, 300)
    assert budget.can_spend(100)
    budget.record(100)
    assert budget.spent_today() == 100
    assert budget.can_spend(200)
    budget.record(200)
    assert not budget.can_spend(1)


def test_negative_amounts_are_ignored(tmp_path):
    budget = GiftBudget(tmp_path / "ledger.json", 300)
    budget.record(-100)
    assert budget.spent_today() == 0
    assert not budget.can_spend(-5)


def test_stale_ledger_resets(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"date": "2000-01-01", "spent": 999}', encoding="utf-8")
    budget = GiftBudget(ledger, 300)
    assert budget.spent_today() == 0
    assert budget.can_spend(100)
