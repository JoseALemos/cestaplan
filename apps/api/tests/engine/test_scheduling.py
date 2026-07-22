"""Meal scheduling produces exactly sum(requested_count) slots (§2.10)."""

from __future__ import annotations

from datetime import date

from cestaplan_engine.scheduling import MealScheduler

from .builders import RANGE, requirement


def test_exact_meal_count_2_4_1_3_equals_10():
    reqs = [
        requirement("breakfast", 2),
        requirement("lunch", 4),
        requirement("snack", 1),
        requirement("dinner", 3),
    ]
    slots = MealScheduler().schedule(reqs, RANGE)
    assert len(slots) == 10
    counts = {}
    for s in slots:
        counts[s.meal_type] = counts.get(s.meal_type, 0) + 1
    assert counts == {"breakfast": 2, "lunch": 4, "snack": 1, "dinner": 3}


def test_slots_have_unique_indices_and_sorted():
    reqs = [requirement("breakfast", 2), requirement("dinner", 2)]
    slots = MealScheduler().schedule(reqs, RANGE)
    assert [s.index for s in slots] == list(range(len(slots)))
    dates = [s.date for s in slots]
    assert dates == sorted(dates)


def test_zero_requested_yields_no_slots():
    slots = MealScheduler().schedule([requirement("lunch", 0)], RANGE)
    assert slots == []


def test_selected_dates_respected():
    reqs = [requirement("lunch", 2, selected_dates=[date(2026, 7, 22), date(2026, 7, 24)])]
    slots = MealScheduler().schedule(reqs, RANGE)
    assert {s.date for s in slots} == {date(2026, 7, 22), date(2026, 7, 24)}


def test_preferred_days_respected():
    # 2026-07-20 is a Monday; ask for Wednesdays only.
    reqs = [requirement("dinner", 1, preferred_days=["wednesday"])]
    slots = MealScheduler().schedule(reqs, RANGE)
    assert slots[0].date.weekday() == 2
