import pytest
from cryptography.fernet import Fernet


def test_encrypted_storage_and_fail_closed(tmp_path):
    from platform_app.secrets import SecretStore, SecretUnavailable

    key = tmp_path / "master.key"
    key.write_bytes(Fernet.generate_key())
    store = SecretStore(fernet_key_path=key, secrets_dir=tmp_path / "secrets")
    ref = store.put_secret({"username": "test-reader", "password": "sensitive-unique-marker"})
    assert "sensitive-unique-marker" not in next((tmp_path / "secrets").iterdir()).read_text()
    assert "sensitive-unique-marker" not in repr(store.resolve(ref.ref, "database"))
    assert store.resolve(ref.ref, "database").value["password"] == "sensitive-unique-marker"
    with pytest.raises(SecretUnavailable):
        store.resolve(ref.ref, "model")
    key.unlink()
    with pytest.raises(SecretUnavailable):
        store.resolve(ref.ref, "database")
    assert not key.exists()


def test_tampering_and_bad_ref_are_rejected(tmp_path):
    from platform_app.secrets import SecretStore, SecretUnavailable

    key = tmp_path / "master.key"
    key.write_bytes(Fernet.generate_key())
    store = SecretStore(fernet_key_path=key, secrets_dir=tmp_path / "secrets")
    ref = store.put_secret({"password": "sensitive"})
    path = next((tmp_path / "secrets").iterdir())
    path.write_bytes(b"corrupted")
    with pytest.raises(SecretUnavailable):
        store.resolve(ref.ref, "database")
    with pytest.raises(SecretUnavailable):
        store.resolve("../master.key", "database")
