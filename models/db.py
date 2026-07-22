"""SQLAlchemy base and database session helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

DEFAULT_DATABASE_URL = "sqlite:///./paper_trading.db"


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(database_url: str | None = None, *, echo: bool = False):
    url = database_url or get_database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


def create_session_factory(engine=None) -> sessionmaker[Session]:
    eng = engine or create_db_engine()
    return sessionmaker(
        bind=eng,
        autoflush=False,
        autocommit=False,
        future=True,
        expire_on_commit=False,
    )


def init_db(engine=None) -> None:
    """Create all tables."""
    # Import models so they register on Base.metadata
    from models import portfolio as _portfolio  # noqa: F401
    from models import prediction as _prediction  # noqa: F401
    from models import trade as _trade  # noqa: F401

    eng = engine or create_db_engine()
    Base.metadata.create_all(eng)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
