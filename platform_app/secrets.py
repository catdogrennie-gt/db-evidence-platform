"""Fernet file secret store; master key absence and tampering fail closed."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .config import PlatformConfig
from .db import utcnow


class SecretUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SecretRef:
    ref: str
    key_id: str
    version: int
    has_secret: bool = True


@dataclass(frozen=True)
class SecretHandle:
    value: object = field(repr=False)
    ref: str = ""
    purpose: str = ""


class SecretStore:
    def __init__(
        self,
        config: PlatformConfig | None = None,
        *,
        fernet_key_path: str | Path | None = None,
        secrets_dir: str | Path | None = None,
    ):
        self.key_path = Path(fernet_key_path or config.fernet_key_path)
        self.directory = Path(secrets_dir or config.secrets_dir)

    def _key(self):
        try:
            key = self.key_path.read_bytes().strip()
            return Fernet(key), hashlib.sha256(key).hexdigest()[:16]
        except (OSError, ValueError):
            raise SecretUnavailable("Secret master key is unavailable or invalid") from None

    def _path(self, ref: str) -> Path:
        if (
            not isinstance(ref, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", ref
            )
            is None
        ):
            raise SecretUnavailable("Invalid secret reference")
        return self.directory / f"{ref}.fernet"

    def put_secret(self, value: object, purpose: str = "database") -> SecretRef:
        if purpose not in ("database", "model", "ssh", "explain_parameter", "zabbix"):
            raise ValueError("Unsupported secret purpose")
        cipher, key_id = self._key()
        ref = str(uuid4())
        envelope = {
            "ref": ref,
            "purpose": purpose,
            "key_id": key_id,
            "version": 1,
            "updated_at": utcnow().isoformat() + "Z",
            "value": value,
        }
        encrypted = cipher.encrypt(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(ref)
        # Exclusive creation ensures no secret overwrite; permissions inherit the private directory ACL.
        with path.open("xb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        return SecretRef(ref, key_id, 1)

    def resolve(self, ref: str | SecretRef, purpose: str = "database") -> SecretHandle:
        ref = ref.ref if isinstance(ref, SecretRef) else ref
        path = self._path(ref)
        cipher, key_id = self._key()  # Re-read key: losing it must never keep working from memory.
        try:
            envelope = json.loads(cipher.decrypt(path.read_bytes()))
            if (
                envelope["key_id"] != key_id
                or envelope["ref"] != ref
                or envelope["purpose"] != purpose
            ):
                raise SecretUnavailable("Secret identity or purpose mismatch")
            return SecretHandle(envelope["value"], ref, purpose)
        except (OSError, ValueError, KeyError, InvalidToken):
            raise SecretUnavailable("Secret is unavailable or cannot be authenticated") from None
