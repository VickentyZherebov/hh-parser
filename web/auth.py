"""
Авторизация: JWT + bcrypt.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

SECRET_KEY = os.environ.get("SECRET_KEY", "hh-parser-secret-change-me-in-prod")
if SECRET_KEY == "hh-parser-secret-change-me-in-prod":
    logger.warning("ВНИМАНИЕ: используется дефолтный SECRET_KEY! Задайте переменную окружения SECRET_KEY.")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30
COOKIE_NAME = "hh_token"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: int, login: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "login": login,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(request: Request) -> dict | None:
    """Извлечь пользователя из JWT cookie. Возвращает None если не авторизован."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_token(token)


def require_auth(request: Request) -> dict:
    """Проверить авторизацию. Редирект на /login если нет токена."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return user


def require_admin(request: Request) -> dict:
    """Проверить что пользователь — админ."""
    user = require_auth(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    return user


# Список путей, не требующих авторизации
PUBLIC_PATHS = {"/login", "/api/auth/login", "/health", "/static"}


def is_public_path(path: str) -> bool:
    for p in PUBLIC_PATHS:
        if path == p or path.startswith(p + "/"):
            return True
    return False
