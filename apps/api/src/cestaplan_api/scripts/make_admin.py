"""Bootstrap a platform admin (self-hosted).

Flags an existing user as an admin so they can manage data sources and run catalogue
imports. Email is matched case-insensitively (stored normalised to lowercase).

Run::

    python -m cestaplan_api.scripts.make_admin <email>
    uv run python -m cestaplan_api.scripts.make_admin admin@example.com
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import User


def make_admin(email: str) -> int:
    """Set ``is_admin=True`` for the user with ``email``. Returns a process exit code."""
    normalised = email.strip().lower()
    with SessionLocal() as session:
        user = session.execute(
            select(User).where(User.email == normalised)
        ).scalar_one_or_none()
        if user is None:
            print(f"No existe ningún usuario con email {normalised!r}.", file=sys.stderr)
            return 1
        if user.is_admin:
            print(f"{normalised} ya es administrador. Sin cambios.")
            return 0
        user.is_admin = True
        session.commit()
        print(f"{normalised} ahora es administrador.")
        return 0


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python -m cestaplan_api.scripts.make_admin <email>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(make_admin(sys.argv[1]))


if __name__ == "__main__":
    main()
