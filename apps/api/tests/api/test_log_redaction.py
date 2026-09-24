"""SEC3: el token de invitación se redacta antes de loguear la ruta."""

from __future__ import annotations

from cestaplan_api.main import _safe_path


def test_invitation_token_is_redacted() -> None:
    assert _safe_path("/api/v1/invitations/ABC123SECRET") == "/api/v1/invitations/:token"
    assert (
        _safe_path("/api/v1/invitations/ABC123SECRET/accept")
        == "/api/v1/invitations/:token/accept"
    )


def test_other_paths_unchanged() -> None:
    assert _safe_path("/api/v1/plans/42") == "/api/v1/plans/42"
    assert _safe_path("/health") == "/health"
