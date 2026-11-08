"""Portable schema regressions plus explicitly enabled real-MySQL checks."""

from pathlib import Path
import os

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]


def alembic_config():
    config = Config(str(ROOT / "migrations" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_migration_creates_schema_with_asset_identity_uniqueness(tmp_path):
    from platform_app.db import create_db_engine, create_session_factory
    from platform_app.models import Department, System, Instance

    engine = create_db_engine(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    for table, columns in [
        ("metric_frames", ["observed_at", "run_id"]),
        ("digest_snapshots", ["observed_at", "id"]),
        ("evidence_manifests", ["retain_until", "id"]),
        ("evidence_pins", ["retain_until", "id"]),
    ]:
        assert any(
            index["column_names"] == columns for index in inspect(engine).get_indexes(table)
        ), table
    with create_session_factory(engine)() as db:
        department = Department(name="migration-check")
        db.add(department)
        db.flush()
        system = System(name="migration-check", department_id=department.id)
        db.add(system)
        db.flush()
        db.add(
            Instance(
                system_id=system.id,
                name="A",
                endpoint="localhost",
                expected_server_uuid="duplicate",
            )
        )
        db.flush()
        db.add(
            Instance(
                system_id=system.id,
                name="B",
                endpoint="localhost",
                expected_server_uuid="duplicate",
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    engine.dispose()


@pytest.mark.skipif(
    os.environ.get("DBA_PLATFORM_MYSQL_TEST") != "1",
    reason="requires real private MySQL control database",
)
def test_real_mysql_upgrade_is_repeatable_and_has_utc_precision():
    from platform_app.config import load_config
    from platform_app.db import create_db_engine

    engine = create_db_engine(load_config())
    assert engine.dialect.name == "mysql"
    for _ in range(2):
        with engine.begin() as connection:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT @@session.time_zone")) == "+00:00"
        assert (
            connection.scalar(
                text(
                    "SELECT DATETIME_PRECISION FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='evidence' AND column_name='observed_at'"
                )
            )
            == 6
        )
        assert (
            connection.scalar(
                text(
                    "SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='tasks'"
                )
            )
            == "InnoDB"
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(alembic_config()).get_current_head()
        )
        assert len(inspect(connection).get_table_names()) >= 52
    engine.dispose()
