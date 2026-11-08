import pytest
from types import SimpleNamespace
from platform_app.network_policy import validate_target
from platform_app.common import PlatformError


def config(**overrides):
    base = dict(
        allowed_networks=["127.0.0.0/8", "192.168.2.0/24"],
        allowed_db_ports=[3306, 3307],
        ssh_profiles=[
            {
                "id": "pinned",
                "host": "192.168.2.132",
                "port": 22,
                "allowed_destinations": ["127.0.0.1"],
                "allowed_ports": [3307],
            }
        ],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_mixed_dns_answer_rejected(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("192.168.2.132", 3306)),
            (2, 1, 6, "", ("169.254.169.254", 3306)),
        ],
    )
    with pytest.raises(PlatformError) as exc:
        validate_target(config(), {"mode": "direct", "db_host": "db.internal", "db_port": 3306})
    assert exc.value.detail["code"] == "target_not_allowed"


def test_direct_dns_pinned_for_connector(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("192.168.2.132", 3306))]
    )
    result = validate_target(
        config(), {"mode": "direct", "db_host": "db.internal", "db_port": 3306}
    )
    assert result["db_host"] == "192.168.2.132"
    assert result["tls_server_name"] == "db.internal"


@pytest.mark.parametrize("host", ["http://127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1"])
def test_unsafe_addresses_rejected(host):
    with pytest.raises(PlatformError):
        validate_target(config(), {"mode": "direct", "db_host": host, "db_port": 3306})


def test_ssh_does_not_allow_arbitrary_remote_destination():
    with pytest.raises(PlatformError):
        validate_target(
            config(),
            {"mode": "ssh", "db_host": "192.168.2.2", "db_port": 3307, "ssh_profile_ref": "pinned"},
        )
    result = validate_target(
        config(),
        {"mode": "ssh", "db_host": "127.0.0.1", "db_port": 3307, "ssh_profile_ref": "pinned"},
    )
    assert result["db_host"] == "127.0.0.1"


def test_control_database_port_is_not_an_onboarding_target():
    with pytest.raises(PlatformError):
        validate_target(config(), {"mode": "direct", "db_host": "127.0.0.1", "db_port": 13306})
