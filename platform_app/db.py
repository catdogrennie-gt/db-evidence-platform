"""SQLAlchemy sessions. Migrations run explicitly, never from API startup."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import PlatformConfig


def utcnow() -> datetime:
    """Naive UTC matches MySQL DATETIME(6); API serializers append Z."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def create_db_engine(config: PlatformConfig | str | URL) -> Engine:
    if isinstance(config, PlatformConfig):
        c = config.control_db
        url = URL.create(
            "mysql+pymysql",
            username=c.user,
            password=c.password.get_secret_value(),
            host=c.host,
            port=c.port,
            database=c.database,
            query={"charset": "utf8mb4"},
        )
    else:
        url = config
    kwargs = {"pool_pre_ping": True, "echo": False, "hide_parameters": True}
    if str(url).startswith("sqlite"):
        kwargs.update(connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        # Authorization rechecks must observe revocations committed by another
        # request, including inside long-lived worker sessions. Queue writes use
        # explicit row locks and fencing rather than repeatable-read snapshots.
        kwargs["isolation_level"] = "READ COMMITTED"
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "mysql":

        @event.listens_for(engine, "connect")
        def set_utc(connection, record):
            with connection.cursor() as cursor:
                cursor.execute("SET time_zone = '+00:00'")
    elif engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_fks(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def create_session_factory(engine: Engine, config: PlatformConfig | None = None):
    return sessionmaker(
        bind=engine,
        expire_on_commit=False,
        info={"platform_config": config} if config is not None else {},
    )


def get_db(request: Request):
    with request.app.state.session_factory() as session:
        yield session
