"""AI usage metering model: UsageLedger.

One row per real OpenAI call (server-side truth): who/where consumed it, the model,
the actual token counts read from the provider's ``response.usage`` (never client
supplied), and — only when a price table is configured — the imputed cost. The cost is
NULLABLE and is never fabricated: no price table means no cost.

In ``platform`` billing (cloud) the ledger backs quotas and transparency; it never
carries or reveals the managed API key.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.models.base import BaseModel


class UsageLedger(BaseModel):
    """A metered AI consumption event (one per OpenAI call). Server-side truth only."""

    __tablename__ = "usage_ledger"
    __table_args__ = (
        Index("ix_usage_household_created", "household_id", "created_at"),
        Index("ix_usage_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"))
    household_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("household.id")
    )
    optimization_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("optimization_run.id")
    )
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Imputed cost — NULL when no price table is configured (never fabricated).
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="EUR")
    extra: Mapped[dict | None] = mapped_column(JSONB)
