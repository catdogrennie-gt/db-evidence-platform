"""Explicit migrations from DBA_PLATFORM_CONFIG, never a credential-bearing INI."""

from alembic import context
from platform_app.config import load_config
from platform_app.db import Base, create_db_engine
from platform_app import models  # noqa: F401

config = context.config


def run(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        dialect_name="mysql",
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    engine = create_db_engine(load_config(config.attributes.get("config_path")))
    with engine.connect() as connection:
        run(connection)
    engine.dispose()
