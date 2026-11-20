
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.db.mongodb import get_database

bearer = HTTPBearer(auto_error=False)


async def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
    database=Depends(get_database),
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_access_token(credentials.credentials, settings)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    token_id = claims.get("jti")
    if not claims.get("sub") or not token_id:
        raise HTTPException(status_code=401, detail="Invalid access token")
    if await database.revoked_tokens.find_one({"_id": token_id}):
        raise HTTPException(status_code=401, detail="Access token has been revoked")
    user = await database.users.find_one({"_id": claims["sub"], "disabled": {"$ne": True}})
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    request.state.token_claims = claims
    return user


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user


def user_public(user: dict) -> dict:
    return {
        "id": str(user["_id"]),
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
    }
