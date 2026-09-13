"""Fail-fast de configuración de producción (Settings.validate_runtime_security).

Un despliegue cloud NUNCA debe arrancar con el secreto de sesión por defecto ni con cookies
sin ``Secure``. En dev/self_hosted (y por tanto en CI, que corre en self_hosted) no valida.
"""

from __future__ import annotations

import pytest

from cestaplan_api.config import DEV_SESSION_SECRET, Settings

_GOOD_SECRET = "una-clave-de-sesion-larga-y-aleatoria-para-produccion-0123456789"


def test_cloud_with_default_secret_raises() -> None:
    settings = Settings(
        deployment_mode="cloud", session_secret=DEV_SESSION_SECRET, cookie_secure=True
    )
    with pytest.raises(RuntimeError) as exc:
        settings.validate_runtime_security()
    assert "SESSION_SECRET" in str(exc.value)


def test_cloud_with_insecure_cookie_raises() -> None:
    settings = Settings(
        deployment_mode="cloud", session_secret=_GOOD_SECRET, cookie_secure=False
    )
    with pytest.raises(RuntimeError) as exc:
        settings.validate_runtime_security()
    assert "COOKIE_SECURE" in str(exc.value)


def test_cloud_with_secure_config_passes() -> None:
    settings = Settings(
        deployment_mode="cloud", session_secret=_GOOD_SECRET, cookie_secure=True
    )
    # No debe lanzar.
    settings.validate_runtime_security()


def test_self_hosted_with_dev_defaults_never_raises() -> None:
    # Dev/CI: los valores por defecto inseguros están permitidos fuera de cloud.
    settings = Settings(
        deployment_mode="self_hosted",
        session_secret=DEV_SESSION_SECRET,
        cookie_secure=False,
    )
    settings.validate_runtime_security()
