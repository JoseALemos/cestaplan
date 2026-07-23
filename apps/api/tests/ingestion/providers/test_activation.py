"""Provider activation gate + kill switch (spec §O/§S).

A provider must not reach production just because its API works: every gate condition plus
the kill switch and the global enable flag are enforced. Rights approval can be waived only
by PROVIDER_REQUIRE_RIGHTS_APPROVAL=false.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.activation import (
    can_run_development,
    evaluate_production,
    guard_production_sync,
)
from cestaplan_api.ingestion.providers.exceptions import ProviderNotActivated
from cestaplan_api.models import ProviderActivation, User


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "price_providers_enabled": True,
        "price_provider_kill_switch": False,
        "provider_require_rights_approval": True,
    }
    base.update(over)
    return Settings(**base)


def _approver(db: Session) -> int:
    user = User(email=f"appr-{id(db)}@x.com", password_hash="x", display_name="Appr")
    db.add(user)
    db.flush()
    return user.id


def _fully_cleared(db: Session, code: str, approver_id: int) -> ProviderActivation:
    row = ProviderActivation(
        provider_code=code,
        transport_status="operational",
        mapper_status="verified",
        data_quality_status="accepted",
        data_rights_status="commercial_use_allowed",
        production_approved_at=datetime(2026, 7, 23, tzinfo=UTC),
        production_approved_by=approver_id,
    )
    db.add(row)
    db.flush()
    return row


def test_no_record_blocks(db_session: Session) -> None:
    decision = evaluate_production(db_session, "no-such-provider-zzz", _settings())
    assert decision.allowed is False
    assert "no_activation_record" in decision.reasons


def test_kill_switch_blocks_even_when_cleared(db_session: Session) -> None:
    approver = _approver(db_session)
    _fully_cleared(db_session, "apify-mercadona", approver)
    decision = evaluate_production(
        db_session, "apify-mercadona", _settings(price_provider_kill_switch=True)
    )
    assert decision.allowed is False
    assert "kill_switch_on" in decision.reasons


def test_fully_cleared_is_allowed(db_session: Session) -> None:
    approver = _approver(db_session)
    _fully_cleared(db_session, "parsebot-alcampo", approver)
    decision = evaluate_production(db_session, "parsebot-alcampo", _settings())
    assert decision.allowed is True
    assert decision.reasons == []


def test_each_missing_condition_blocks(db_session: Session) -> None:
    approver = _approver(db_session)
    row = _fully_cleared(db_session, "prov-x", approver)
    row.mapper_status = "blocked"
    db_session.flush()
    decision = evaluate_production(db_session, "prov-x", _settings())
    assert decision.allowed is False
    assert any("mapper_status=blocked" in r for r in decision.reasons)


def test_rights_can_be_waived(db_session: Session) -> None:
    approver = _approver(db_session)
    row = _fully_cleared(db_session, "prov-rights", approver)
    row.data_rights_status = "under_review"
    db_session.flush()
    # With approval required -> blocked on rights.
    assert evaluate_production(db_session, "prov-rights", _settings()).allowed is False
    # With the flag off -> rights no longer block (other conditions still hold).
    assert (
        evaluate_production(
            db_session, "prov-rights", _settings(provider_require_rights_approval=False)
        ).allowed
        is True
    )


def test_guard_raises_when_blocked(db_session: Session) -> None:
    with pytest.raises(ProviderNotActivated):
        guard_production_sync(db_session, "unknown-prov", _settings())


def test_development_only_allows_dev_but_not_prod(db_session: Session) -> None:
    row = ProviderActivation(
        provider_code="dev-prov",
        transport_status="operational",
        mapper_status="pending",
        development_only=True,
    )
    db_session.add(row)
    db_session.flush()
    # dev allowed...
    assert can_run_development(db_session, "dev-prov", _settings()) is True
    # ...but production still blocked, and the kill switch overrides dev too.
    assert evaluate_production(db_session, "dev-prov", _settings()).allowed is False
    assert (
        can_run_development(db_session, "dev-prov", _settings(price_provider_kill_switch=True))
        is False
    )
