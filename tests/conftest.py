"""Opt-in compatibility for modules that exercise retired direct workflows."""

from types import SimpleNamespace
import pytest


@pytest.fixture
def legacy_collection_compatibility(monkeypatch):
    from platform_app import config

    original = config.zabbix_only
    legacy = SimpleNamespace(collection_mode="legacy")

    def mode(value=None, *, db=None):
        value = (
            value
            if value is not None
            else (db.info.get("platform_config") if db is not None else None)
        )
        return original(
            value if value is not None and hasattr(value, "collection_mode") else legacy
        )

    monkeypatch.setattr(config, "zabbix_only", mode)
