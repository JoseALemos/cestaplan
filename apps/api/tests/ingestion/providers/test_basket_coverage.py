"""Real basket coverage (spec §X) — pure logic.

A basket is 'coste calculado' only when everything is priced, packaged, mapped, non-expired
and estimate-free-or-approved. Otherwise known vs estimated cost are split, counts and a
min-max range are reported, and the label is never 'precio exacto'.
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_api.services.basket_coverage import BasketLine, evaluate_basket_coverage
from cestaplan_api.services.price_resolution import FreshnessState, PriceResolution


def _res(
    price: str, *, freshness=FreshnessState.FRESH, price_type="regular", scope="exact_store"
) -> PriceResolution:
    return PriceResolution(
        selected_price=Decimal(price),
        price_type=price_type,
        price_scope=scope,
        freshness=freshness,
        confidence_score=Decimal("1.0"),
    )


def _line(name: str, res: PriceResolution | None, **over) -> BasketLine:
    kw = {"has_package_data": True, "ingredient_mapped": True, "quantity": Decimal("1")}
    kw.update(over)
    return BasketLine(canonical_name=name, resolution=res, **kw)  # type: ignore[arg-type]


def test_complete_basket_is_coste_calculado() -> None:
    report = evaluate_basket_coverage([_line("a", _res("1.00")), _line("b", _res("2.00"))])
    assert report.complete is True
    assert report.cost_label == "coste calculado"
    assert report.line_coverage == 1.0
    assert report.cost_known == Decimal("3.00")
    assert report.cost_min == report.cost_max == Decimal("3.00")


def test_unresolved_line_is_not_complete() -> None:
    report = evaluate_basket_coverage([_line("a", _res("1.00")), _line("b", None)])
    assert report.complete is False
    assert report.unresolved_lines == 1
    assert report.cost_label == "coste conocido"
    assert report.cost_known == Decimal("1.00")


def test_estimate_splits_cost_and_labels_estimado() -> None:
    report = evaluate_basket_coverage(
        [
            _line("a", _res("1.00")),
            _line(
                "b", _res("0.50", price_type="estimated", scope="national"), estimate_approved=True
            ),
        ]
    )
    assert report.cost_known == Decimal("1.00")
    assert report.cost_estimated == Decimal("0.50")
    assert report.cost_min == Decimal("1.00")
    assert report.cost_max == Decimal("1.50")
    assert report.cost_label == "coste estimado"
    assert report.complete is False  # an estimate was used


def test_unapproved_estimate_warns() -> None:
    report = evaluate_basket_coverage(
        [_line("b", _res("0.50", price_type="estimated"), estimate_approved=False)]
    )
    assert any("without approval" in w for w in report.warnings)


def test_missing_package_data_blocks_complete() -> None:
    report = evaluate_basket_coverage([_line("a", _res("1.00"), has_package_data=False)])
    assert report.complete is False
    assert report.package_data_coverage == 0.0


def test_old_and_different_scope_counts() -> None:
    report = evaluate_basket_coverage(
        [
            _line("a", _res("1.00", freshness=FreshnessState.STALE, scope="national")),
            _line("b", _res("2.00")),
        ]
    )
    assert report.old_price_lines == 1
    assert report.different_scope_lines == 1
    assert report.exact_scope_coverage == 0.5


def test_label_never_says_precio_exacto() -> None:
    for report in (
        evaluate_basket_coverage([_line("a", _res("1.00"))]),
        evaluate_basket_coverage([_line("a", None)]),
    ):
        assert "exacto" not in report.cost_label
        assert report.cost_label in {
            "coste calculado",
            "coste conocido",
            "coste estimado",
            "coste aproximado",
        }
