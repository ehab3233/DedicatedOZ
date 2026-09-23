"""Additive schema upgrades.

`create_all` creates missing tables but never touches existing ones, so an
install from an earlier version would be missing any column added since. This
adds them. It only ever adds nullable columns (or ones with a server default)
and never drops, renames or retypes anything -- those need a real migration,
and should get one (Alembic) before the first change that needs it.

Safe to run on every start: every statement is IF NOT EXISTS.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.models import Base


def upgrade_schema(engine: Engine) -> list[str]:
    """Create missing tables and add missing columns. Returns what changed."""
    Base.metadata.create_all(engine)

    changes: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(
                        f"{table.name}.{column.name} is NOT NULL without a server "
                        "default; it needs a real migration, not an additive one"
                    )
                ddl_type = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.server_default is not None:
                    default = f" DEFAULT {column.server_default.arg.text}"  # type: ignore[union-attr]
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS '
                        f'"{column.name}" {ddl_type}{default}'
                    )
                )
                changes.append(f"{table.name}.{column.name}")
    return changes
