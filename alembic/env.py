from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, create_engine, pool

from app.core.config import get_settings
from app.core.database import CONNECT_TIMEOUT_SECONDS
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: don't silence the app's loggers when migrations
    # run in-process (e.g. from the test suite).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> URL:
    url = config.attributes.get("database_url")
    return url if url is not None else get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url().render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
