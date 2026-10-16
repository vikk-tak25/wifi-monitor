"""Alembic environment. The database URL comes from the `sqlalchemy.url` option when set
(tests pass it in), otherwise from the fleet settings (DATABASE_URL)."""

from alembic import context
from sqlalchemy import create_engine, pool

from fleet.config import load_settings
from fleet.tables import metadata

config = context.config
url = config.get_main_option("sqlalchemy.url") or load_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
