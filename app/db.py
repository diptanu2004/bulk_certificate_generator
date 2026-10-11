from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)

# expire_on_commit=False keeps ORM objects readable after commit, which the
# worker relies on: it commits the claim, then renders from the same objects.
SessionLocal = sessionmaker(engine, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Create tables if absent.

    Deliberately not Alembic: for a two-table service this keeps setup to a
    single command. Production would use migrations -- see the README.
    """
    from app import models  # noqa: F401  (registers mappers on Base)

    Base.metadata.create_all(engine)
