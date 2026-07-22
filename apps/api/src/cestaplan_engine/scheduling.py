"""Meal scheduling (OPTIMIZATION.md §2.10).

Distributes each requirement's ``requested_count`` meals across the date range,
honoring ``selected_dates`` / ``preferred_days`` / ``auto_distribute``. Produces
EXACTLY ``sum(requested_count)`` slots — no more, no fewer. Users are not obliged
to fill every meal of every day; gaps are allowed by simply not requesting them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from cestaplan_engine.contracts import (
    WEEKDAY_INDEX,
    MealRequirementDTO,
)

_MEAL_ORDER = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3}


@dataclass(frozen=True)
class MealSlot:
    """An empty day/meal slot to be filled with a recipe."""

    index: int
    date: date
    meal_type: str
    servings: int
    maximum_preparation_minutes: int | None
    requires_tupper: bool
    reheating_available: bool


def _dates_in_range(start: date, end: date) -> list[date]:
    if end < start:
        return [start]
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def _pick_dates(req: MealRequirementDTO, pool: list[date]) -> list[date]:
    count = req.requested_count
    if count == 0:
        return []

    if req.selected_dates:
        chosen = sorted(req.selected_dates)
        return [chosen[i % len(chosen)] for i in range(count)]

    if req.preferred_days:
        wanted = {WEEKDAY_INDEX[d] for d in req.preferred_days}
        preferred = [d for d in pool if d.weekday() in wanted]
        source = preferred or pool
        return [source[i % len(source)] for i in range(count)]

    if req.auto_distribute and pool:
        # Even spread: index i -> floor(i * len / count).
        n = len(pool)
        return [pool[(i * n) // count] for i in range(count)]

    # Sequential fill from the start, cycling if fewer days than meals.
    return [pool[i % len(pool)] for i in range(count)]


class MealScheduler:
    """Builds the flat list of meal slots to be planned."""

    def schedule(
        self,
        requirements: list[MealRequirementDTO],
        date_range: tuple[date, date],
    ) -> list[MealSlot]:
        start, end = date_range
        pool = _dates_in_range(start, end)
        slots: list[MealSlot] = []

        for req in requirements:
            for slot_date in _pick_dates(req, pool):
                slots.append(
                    MealSlot(
                        index=0,  # assigned after sorting
                        date=slot_date,
                        meal_type=req.meal_type,
                        servings=req.default_servings,
                        maximum_preparation_minutes=req.maximum_preparation_minutes,
                        requires_tupper=req.requires_tupper,
                        reheating_available=req.reheating_available,
                    )
                )

        slots.sort(key=lambda s: (s.date, _MEAL_ORDER.get(s.meal_type, 99)))
        return [
            MealSlot(
                index=i,
                date=s.date,
                meal_type=s.meal_type,
                servings=s.servings,
                maximum_preparation_minutes=s.maximum_preparation_minutes,
                requires_tupper=s.requires_tupper,
                reheating_available=s.reheating_available,
            )
            for i, s in enumerate(slots)
        ]
