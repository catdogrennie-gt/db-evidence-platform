"""Same-origin login with one-use anonymous CSRF and revocable sessions."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
import hmac
import secrets
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from ..access import ActorContext, authorize, current_permissions, current_roles
from ..db import get_db, utcnow
from ..identity import (
    ANON_COOKIE_NAME,
    COOKIE_NAME,
    PASSWORD_HASHER,
    authenticate,
    create_session,
    csrf_for_token,
    current_actor,
    require_csrf,
    require_origin,
    revoke_session,
    token_hash,
    verify_password,
)
from ..models import AuditLog, LoginNonce, User

router = APIRouter(prefix="/auth", tags=["auth"])
_attempts: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()
_dummy_hash: str | None = None


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=1024)


def _cookie(response: Response, request: Request, name: str, value: str, seconds: int):
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        httponly=True,
        secure=request.app.state.config.secure_cookies,
        samesite="strict",
        path="/api/v1",
    )


def _session_response(db, actor, token):
    user = db.get(User, actor.user_id)
    permissions = current_permissions(actor)
    ids = authorize(actor, "data.read").instance_ids if "data.read" in permissions else frozenset()
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "active": user.active,
            "permission_version": user.permission_version,
        },
        "roles": current_roles(actor),
        "permissions": sorted(permissions),
        "scope": {"instance_ids": sorted(ids), "instance_count": len(ids)},
        "csrf_token": csrf_for_token(token),
    }


@router.get("/csrf")
def csrf(request: Request, response: Response, db: DBSession = Depends(get_db)):
    token = secrets.token_urlsafe(48)
    csrf_token = csrf_for_token(token)
    db.add(
        LoginNonce(
            token_hash=token_hash(token),
            csrf_hash=token_hash(csrf_token),
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    db.commit()
    _cookie(response, request, ANON_COOKIE_NAME, token, 600)
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": csrf_token}


def _rate_limit(request: Request, username: str):
    key = (request.client.host if request.client else "local") + ":" + username.casefold()
    now = time.monotonic()
    with _rate_lock:
        # Bound memory even when attackers rotate usernames.
        if len(_attempts) > 5000:
            for old_key in list(_attempts):
                if not _attempts[old_key] or _attempts[old_key][-1] < now - 300:
                    del _attempts[old_key]
            if len(_attempts) > 5000:
                raise HTTPException(429, "Login temporarily rate limited")
        history = _attempts[key]
        while history and history[0] < now - 300:
            history.popleft()
        if len(history) >= 5:
            raise HTTPException(429, "Too many login attempts")
        history.append(now)
        return key, now


def _successful_attempt(reservation):
    # Legitimate shared-demo visitors should not consume the failed-login cap.
    # Remove only this request, preserving concurrent failures for the same key.
    key, timestamp = reservation
    with _rate_lock:
        history = _attempts.get(key)
        if history is not None:
            try:
                history.remove(timestamp)
            except ValueError:
                pass


@router.post("/login")
def login(body: LoginInput, request: Request, response: Response, db: DBSession = Depends(get_db)):
    global _dummy_hash
    require_origin(request)
    reservation = _rate_limit(request, body.username)
    token = request.cookies.get(ANON_COOKIE_NAME, "")
    nonce = db.scalar(
        select(LoginNonce).where(LoginNonce.token_hash == token_hash(token)).with_for_update()
    )
    if not nonce or nonce.consumed_at or nonce.expires_at <= utcnow():
        raise HTTPException(403, "Login CSRF nonce expired")
    require_csrf(request, nonce.csrf_hash)
    nonce.consumed_at = utcnow()
    db.commit()
    user = db.scalar(select(User).where(User.username == body.username))
    if _dummy_hash is None:
        _dummy_hash = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
    valid = verify_password(
        user.password_hash if user else _dummy_hash, body.password.get_secret_value()
    )
    if not valid or user is None or not user.active:
        db.add(
            AuditLog(
                actor_id=user.id if user else None,
                action="auth.login",
                target_type="user",
                target_id=user.id if user else None,
                result="denied",
            )
        )
        db.commit()
        raise HTTPException(401, "Invalid username or password")
    if PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = PASSWORD_HASHER.hash(body.password.get_secret_value())
    _successful_attempt(reservation)
    token, csrf_token, session = create_session(
        db, user, hours=request.app.state.config.session_hours
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="auth.login",
            target_type="session",
            target_id=session.id,
            result="success",
        )
    )
    db.commit()
    _cookie(response, request, COOKIE_NAME, token, request.app.state.config.session_hours * 3600)
    response.delete_cookie(ANON_COOKIE_NAME, path="/api/v1")
    response.headers["Cache-Control"] = "no-store"
    return _session_response(db, authenticate(db, token), token)


@router.get("/me")
def me(request: Request, response: Response, actor: ActorContext = Depends(current_actor)):
    response.headers["Cache-Control"] = "no-store"
    return _session_response(actor.db, actor, request.cookies[COOKIE_NAME])


@router.post("/logout", status_code=204)
def logout(response: Response, actor: ActorContext = Depends(current_actor)):
    revoke_session(actor.db, actor.session_id)
    actor.db.add(
        AuditLog(
            actor_id=actor.user_id,
            action="auth.logout",
            target_type="session",
            target_id=actor.session_id,
            result="success",
        )
    )
    actor.db.commit()
    response.delete_cookie(COOKIE_NAME, path="/api/v1")
