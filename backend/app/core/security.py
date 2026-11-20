from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import Settings

password_hash = PasswordHash.recommended()
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, password_digest: str) -> bool:
    return password_hash.verify(password, password_digest)


def create_access_token(user_id: str, settings: Settings) -> tuple[str, str, datetime]:
    token_id = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    token = jwt.encode(
        {"sub": user_id, "jti": token_id, "exp": expires_at},
        settings.secret_key.get_secret_value(),
        algorithm=ALGORITHM,
    )
    return token, token_id, expires_at


def decode_access_token(token: str, settings: Settings) -> dict:
    return jwt.decode(token, settings.secret_key.get_secret_value(), algorithms=[ALGORITHM])
