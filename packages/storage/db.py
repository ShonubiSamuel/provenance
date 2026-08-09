"""Engine/session setup, SQLite pragmas (WAL), and FTS5 provisioning.

The FTS5 virtual table + sync triggers are SQLite-specific. When we add Postgres, this
module gets a sibling that provisions tsvector/pg_trgm instead; the repository layer
above does not change.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from packages.core.settings import get_settings
from packages.storage.orm import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _apply_sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    cur.close()


# FTS5 over the searchable columns of `matches`. External-content table keyed on
# matches.id so we don't duplicate storage, plus triggers to keep it in sync.
_FTS_SETUP = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS fts_matches USING fts5(
        path, filename, snippet,
        content='matches', content_rowid='id',
        tokenize='unicode61'
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS matches_ai AFTER INSERT ON matches BEGIN
        INSERT INTO fts_matches(rowid, path, filename, snippet)
        VALUES (new.id, new.path, new.filename, new.snippet);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS matches_ad AFTER DELETE ON matches BEGIN
        INSERT INTO fts_matches(fts_matches, rowid, path, filename, snippet)
        VALUES ('delete', old.id, old.path, old.filename, old.snippet);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS matches_au AFTER UPDATE ON matches BEGIN
        INSERT INTO fts_matches(fts_matches, rowid, path, filename, snippet)
        VALUES ('delete', old.id, old.path, old.filename, old.snippet);
        INSERT INTO fts_matches(rowid, path, filename, snippet)
        VALUES (new.id, new.path, new.filename, new.snippet);
    END;
    """,
]


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    settings = get_settings()
    url = settings.database_url

    if _is_sqlite(url):
        path_part = url.split("///", 1)[-1]
        if path_part and path_part != ":memory:":
            # Ensure the parent directory exists for file-based SQLite URLs.
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, future=True)
        else:
            # In-memory DB: every pooled connection would get its OWN empty database,
            # and FastAPI's TestClient runs handlers on separate threads. Pin a single
            # shared connection so tests see one coherent database.
            from sqlalchemy.pool import StaticPool

            _engine = create_engine(
                url,
                future=True,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        event.listen(_engine, "connect", _apply_sqlite_pragmas)
    else:
        _engine = create_engine(url, future=True, pool_pre_ping=True)

    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


# Columns added after a table first shipped. create_all only creates missing TABLES,
# so existing databases need these applied by ALTER. (table, column, DDL type/default).
_COLUMN_MIGRATIONS = [
    ("downloads", "total_is_estimate", "INTEGER NOT NULL DEFAULT 0"),
    ("searches", "reported_matches", "INTEGER NOT NULL DEFAULT 0"),
    ("searches", "sampled", "INTEGER NOT NULL DEFAULT 0"),
    ("searches", "note", "TEXT"),
]


def _ensure_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if cols and column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    """Create all tables and (for SQLite) the FTS5 index + triggers. Idempotent."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    if _is_sqlite(get_settings().database_url):
        _ensure_columns(engine)
        with engine.begin() as conn:
            for stmt in _FTS_SETUP:
                conn.execute(text(stmt))


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context; commits on success, rolls back on error."""
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
