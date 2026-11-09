"""Unknown fixtures must retain the real product collection default."""

from types import SimpleNamespace
from platform_app import config
from platform_app.db import create_db_engine, create_session_factory


def test_missing_mode_defaults_to_zabbix_only_outside_dedicated_module():
    assert config.zabbix_only()
    assert config.zabbix_only(SimpleNamespace())
    engine = create_db_engine("sqlite://")
    with create_session_factory(engine)() as db:
        assert config.zabbix_only(db=db)
    engine.dispose()
