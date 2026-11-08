"""Argon2id credentials, hashed revocable sessions and same-origin CSRF."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from .access import ActorContext, actor_for_user
from .db import get_db, utcnow
from .models import Session, User

COOKIE_NAME = "dba_session"
ANON_COOKIE_NAME = "dba_login_nonce"
PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for_token(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"dba-platform-csrf-v1", hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def create_session(db: DBSession, user: User, *, hours: int = 8):
    if not user.active:
        raise HTTPException(401, "Authentication required")
    token = secrets.token_urlsafe(48)
    csrf = csrf_for_token(token)
    session = Session(
        user_id=user.id,
        token_hash=token_hash(token),
        csrf_hash=token_hash(csrf),
        permission_version=user.permission_version,
        expires_at=utcnow() + timedelta(hours=hours),
    )
    db.add(session)
    db.commit()
    return token, csrf, session


def authenticate(db: DBSession, token: str | None) -> ActorContext:
    if not token or len(token) > 256:
        raise HTTPException(401, "Authentication required")
    session = db.scalar(
        select(Session)
        .where(Session.token_hash == token_hash(token))
        .execution_options(populate_existing=True)
    )
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        raise HTTPException(401, "Session expired or revoked")
    return actor_for_user(db, session.user_id, session.id)


def revoke_session(db: DBSession, session_id: str):
    session = db.get(Session, session_id)
    if session:
        session.revoked_at = utcnow()
        db.commit()


def require_origin(request: Request):
    if request.headers.get("origin", "").rstrip(
        "/"
    ) != request.app.state.config.public_origin.rstrip("/"):
        raise HTTPException(403, "Untrusted request origin")


def require_csrf(request: Request, stored_hash: str):
    require_origin(request)
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not hmac.compare_digest(token_hash(supplied), stored_hash):
        raise HTTPException(403, "CSRF validation failed")


def current_actor(request: Request, db: DBSession = Depends(get_db)) -> ActorContext:
    actor = authenticate(db, request.cookies.get(COOKIE_NAME))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_csrf(request, db.get(Session, actor.session_id).csrf_hash)
    return actor
