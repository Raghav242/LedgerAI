from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings

CONNECT_TIMEOUT_SECONDS = 5


def create_db_engine(settings: Settings) -> Engine:
    # pool_pre_ping transparently replaces connections killed by a DB restart;
    # connect_timeout makes an unreachable database fail fast instead of hanging.
    return create_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False keeps committed objects readable for response serialization
    # without issuing a fresh SELECT per attribute after the transaction has ended.
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def atomic(session: Session) -> Iterator[None]:
    """Run the enclosed block as one database transaction.

    Commits if the block finishes, rolls back everything if it raises. This is the
    single place where service-level writes are committed; repositories only add/flush.
    """
    try:
        yield
        session.commit()
    except Exception:
        session.rollback()
        raise
