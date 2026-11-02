import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from .config import get_settings
from .database import get_db
from .models import Session, User, UserRole


password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session(db: DBSession, user: User, request: Request) -> tuple[Session, str]:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(48)
    session = Session(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        csrf_token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.session_hours),
        user_agent=(request.headers.get("user-agent") or "")[:500],
        ip_address=request.client.host if request.client else None,
    )
    db.add(session)
    db.flush()
    return session, raw_token


@dataclass
class AuthContext:
    user: User
    session: Session


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def current_context(
    session_token: str | None = Cookie(default=None, alias="annotation_session"),
    db: DBSession = Depends(get_db),
) -> AuthContext:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    session = db.scalar(select(Session).where(Session.token_hash == token_hash(session_token)))
    if not session or _as_utc(session.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已停用")
    return AuthContext(user=user, session=session)


def ready_context(ctx: AuthContext = Depends(current_context)) -> AuthContext:
    if ctx.user.must_change_password:
        raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="首次登录必须修改密码")
    return ctx


def csrf_context(
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ctx: AuthContext = Depends(current_context),
) -> AuthContext:
    if not csrf_token or not secrets.compare_digest(csrf_token, ctx.session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="请求校验失败，请刷新页面后重试")
    return ctx


def ready_csrf_context(ctx: AuthContext = Depends(csrf_context)) -> AuthContext:
    if ctx.user.must_change_password:
        raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="首次登录必须修改密码")
    return ctx


def require_roles(*roles: UserRole):
    def dependency(ctx: AuthContext = Depends(ready_context)) -> AuthContext:
        if not any(ctx.user.has_role(role) for role in roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有执行此操作的权限")
        return ctx

    return dependency


def require_roles_csrf(*roles: UserRole):
    def dependency(ctx: AuthContext = Depends(ready_csrf_context)) -> AuthContext:
        if not any(ctx.user.has_role(role) for role in roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有执行此操作的权限")
        return ctx

    return dependency
