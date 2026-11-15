import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from tests.platform.test_ai_failures import prepared_run
from tests.platform.test_ai_scope import ai_env


@pytest.fixture
def provider_config(tmp_path):
    key_path = tmp_path / "provider.key"
    key_path.write_text("private-test-token", encoding="utf-8")
    return SimpleNamespace(model={
        "base_url": "https://provider.invalid", "key_path": str(key_path),
    })


def failing_client(monkeypatch, error):
    from platform_app import investigations as inv

    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise error

    monkeypatch.setattr(inv.httpx, "Client", Client)
    return calls


@pytest.mark.parametrize("error_type, expected", [
    (httpx.ConnectTimeout, "connect_timeout"),
    (httpx.ReadTimeout, "read_timeout"),
    (httpx.WriteTimeout, "write_timeout"),
    (httpx.PoolTimeout, "pool_timeout"),
    (httpx.TimeoutException, "timeout"),
    (httpx.ConnectError, "connect_error"),
    (httpx.ReadError, "read_error"),
    (httpx.WriteError, "write_error"),
    (httpx.CloseError, "close_error"),
    (httpx.RemoteProtocolError, "remote_protocol_error"),
    (httpx.LocalProtocolError, "local_protocol_error"),
    (httpx.ProxyError, "proxy_error"),
    (httpx.UnsupportedProtocol, "unsupported_protocol"),
    (httpx.TransportError, "transport_error"),
])
def test_transport_kind_preserves_unknown_and_never_retries(
    provider_config, monkeypatch, error_type, expected,
):
    from platform_app.investigations import call_model, ModelTransportError

    calls = failing_client(monkeypatch, error_type(
        "private-test-token https://user:password@private.invalid/secret",
    ))
    with pytest.raises(ModelTransportError) as caught:
        call_model(provider_config, {"messages": []}, 5)
    error = caught.value
    assert error.code == "model_call_outcome_unknown" and error.unknown is True
    assert error.transport_kind == expected
    assert str(error) == "model_call_outcome_unknown"
    assert len(calls) == 1


def test_unknown_subclass_name_never_becomes_diagnostic(provider_config, monkeypatch):
    from platform_app.investigations import call_model, ModelTransportError

    secret_type = type("CredentialInExceptionName", (httpx.TransportError,), {})
    failing_client(monkeypatch, secret_type("private-test-token"))
    with pytest.raises(ModelTransportError) as caught:
        call_model(provider_config, {}, 5)
    assert caught.value.transport_kind == "transport_error"


@pytest.mark.parametrize("kind", [None, "read_timeout", "https://private.invalid/secret", [], {}])
def test_diagnostic_constructor_only_accepts_fixed_categories(kind):
    from platform_app.investigations import ModelTransportError

    error = ModelTransportError("model_call_outcome_unknown", True, transport_kind=kind)
    assert error.transport_kind == ("read_timeout" if kind == "read_timeout" else None)


def test_actual_adapter_category_persists_without_secret_or_delivery_confirmation(
    ai_env, provider_config, monkeypatch,
):
    from platform_app.investigations import run_investigation
    from platform_app.models import ModelCall

    row, _, lease, config = prepared_run(ai_env)
    config.model.update(provider_config.model)
    calls = failing_client(monkeypatch, httpx.ReadTimeout(
        "private-test-token https://user:password@private.invalid/secret",
    ))
    result = run_investigation(ai_env["factory"], lease, config)
    assert result.status == "failed" and len(calls) == 1
    with ai_env["factory"]() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.investigation_id == row.id))
        assert call.status == "outcome_unknown" and call.cost is None
        assert call.input_tokens is None and call.output_tokens is None
        assert call.response["transport_kind"] == "read_timeout"
        assert call.response["request_delivery"]["status"] == "outcome_unknown"
        assert "audit_sha256" not in call.response["request_delivery"]
        assert "private_audit_ref" not in call.response
        stored = json.dumps({"response": call.response, "report": result.report})
        assert all(secret not in stored for secret in (
            "private-test-token", "private.invalid", "user:password",
        ))


def test_legacy_error_constructor_does_not_invent_transport_category(ai_env):
    from platform_app.investigations import run_investigation, ModelTransportError
    from platform_app.models import ModelCall

    row, _, lease, config = prepared_run(ai_env)

    def rejected(*args):
        raise ModelTransportError("model_http_400", False)

    run_investigation(ai_env["factory"], lease, config, model_client=rejected)
    with ai_env["factory"]() as db:
        call = db.scalar(select(ModelCall).where(ModelCall.investigation_id == row.id))
        assert call.status == "failed" and call.cost is None
        assert "transport_kind" not in call.response
