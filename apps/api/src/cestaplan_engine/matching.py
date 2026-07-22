"""Map canonical ingredients to store products (OPTIMIZATION.md §2.6).

Honors substitution groups: if the exact ingredient has no product, another
member of its substitution group may be used. Never invents a product — an
unmatched ingredient yields a price-less line that lowers coverage.
"""

from __future__ import annotations

from cestaplan_engine.contracts import CatalogProductDTO, RecipeIngredientDTO


class ProductMatcher:
    """Resolves a canonical ingredient name to a catalog product."""

    def __init__(
        self,
        catalog: list[CatalogProductDTO],
        substitution_groups: dict[str, list[str]] | None = None,
    ) -> None:
        # canonical_name -> product (first wins; catalog is store-scoped upstream).
        self._by_name: dict[str, CatalogProductDTO] = {}
        for product in catalog:
            self._by_name.setdefault(product.canonical_name, product)
        # substitution_group -> ordered candidate canonical names.
        self._groups = substitution_groups or {}

    def match(
        self, canonical_name: str, substitution_group: str | None = None
    ) -> CatalogProductDTO | None:
        """Return the product for ``canonical_name`` or a substitute in its group."""
        direct = self._by_name.get(canonical_name)
        if direct is not None:
            return direct
        if substitution_group is not None:
            for alt in self._groups.get(substitution_group, []):
                product = self._by_name.get(alt)
                if product is not None:
                    return product
        return None

    def match_ingredient(
        self, ingredient: RecipeIngredientDTO
    ) -> CatalogProductDTO | None:
        return self.match(ingredient.canonical_name, ingredient.substitution_group)
