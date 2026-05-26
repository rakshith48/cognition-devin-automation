"""Shared fixtures.

Each test gets its own SQLite file under tmp_path. We patch app.db.connection.DB_PATH
so module-level reads of the constant pick up the per-test value.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """Per-test SQLite file. Importing app.db AFTER this fixture runs picks
    up the patched path. Tests should `from app import db` inside the test
    function, not at module top, or use this fixture's side-effect ordering.
    """
    p = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(p))

    # Reset module-level constant in case app.db.connection is already imported.
    from app.db import connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", p)

    # Initialize the schema fresh.
    from app import db
    db.init_db()
    return p
