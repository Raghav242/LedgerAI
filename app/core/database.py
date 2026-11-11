from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import CommitOutcomeUnknownError, ConcurrencyConflictError

CONNECT_TIMEOUT_SECONDS = 5

# PostgreSQL SQLSTATEs that mean "another transaction got in the way; nothing was
# committed; retrying the whole transaction is safe".
LOCK_NOT_AVAILABLE = "55P03"  # lock_timeout expired
DEADLOCK_DETECTED = "40P01"
SERIALIZATION_FAILURE = "40001"
CONTENTION_SQLSTATES = frozenset({LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED, SERIALIZATION_FAILURE})


def create_db_engine(settings: Settings) -> Engine:
    # pool_pre_ping transparently replaces connections killed by a DB restart;
    # connect_timeout makes an unreachable database fail fast instead of hanging.
    return create_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False keeps committed objects readable for response serialization
    # without issuing a fresh SELECT per attribute after the transaction has ended.
    return sessionmaker(bind=engine, expire_on_commit=False)


def sqlstate_of(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


@contextmanager
def atomic(session: Session, *, lock_timeout_ms: int | None = None) -> Iterator[None]:
    """Run the enclosed block as one database transaction (the explicit transaction manager).

    - Commits exactly once if the block finishes; rolls back everything if it raises.
    - `lock_timeout_ms` bounds how long any statement in the block may wait for a row
      lock (`SET LOCAL lock_timeout`, so it expires with the transaction).
    - Lock timeouts, deadlocks and serialization failures become ConcurrencyConflictError.
    - A connection lost *during* COMMIT becomes CommitOutcomeUnknownError: the server may
      have committed before the connection died, so the caller must not assume either way.

    This is the single place where service-level writes are committed; repositories
    only add/flush.
    """
    try:
        if lock_timeout_ms is not None:
            session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{int(lock_timeout_ms)}ms"},
            )
        yield
    except DBAPIError as exc:
        session.rollback()
        if sqlstate_of(exc) in CONTENTION_SQLSTATES:
            raise ConcurrencyConflictError(
                "The operation conflicted with a concurrent transaction and was rolled back",
                {"sqlstate": sqlstate_of(exc)},
            ) from exc
        raise
    except Exception:
        session.rollback()
        raise

    try:
        session.commit()
    except DBAPIError as exc:
        session.rollback()
        if exc.connection_invalidated:
            raise CommitOutcomeUnknownError(
                "The database connection was lost while committing; the outcome is unknown"
            ) from exc
        if sqlstate_of(exc) in CONTENTION_SQLSTATES:
            raise ConcurrencyConflictError(
                "The operation conflicted with a concurrent transaction and was rolled back",
                {"sqlstate": sqlstate_of(exc)},
            ) from exc
        # The server answered COMMIT with an error (e.g. a deferred constraint trigger):
        # the transaction definitely rolled back.
        raise
