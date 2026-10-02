from functools import lru_cache

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.errors import ApiError
from app.models import User


bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def verify_supabase_token(token: str) -> dict:
    settings = get_settings()
    if not settings.supabase_url:
        raise ApiError(503, "AUTH_NOT_CONFIGURED", "Supabase authentication is not configured.")
    if settings.supabase_jwt_secret:
        try:
            claims = jwt.decode(
                token,
                settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                issuer=f"{settings.supabase_url.rstrip('/')}/auth/v1",
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise ApiError(401, "INVALID_TOKEN", "The access token is invalid or expired.") from exc
    else:
        issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"
        url = f"{issuer}/.well-known/jwks.json"
        try:
            signing_key = jwks_client(url).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256"],
                audience="authenticated",
                issuer=issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except (jwt.PyJWTError, OSError) as exc:
            raise ApiError(401, "INVALID_TOKEN", "The access token is invalid or expired.") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ApiError(401, "INVALID_TOKEN", "The access token has no valid subject.")
    return claims


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "AUTHENTICATION_REQUIRED", "A bearer access token is required.")
    claims = verify_supabase_token(credentials.credentials)
    user = db.scalar(select(User).where(User.auth_subject == claims["sub"]))
    if user is None:
        raise ApiError(401, "PROFILE_REQUIRED", "Exchange your Supabase identity for an app profile first.")
    return user
