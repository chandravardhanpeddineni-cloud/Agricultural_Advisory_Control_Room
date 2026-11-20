from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from app.api.dependencies import current_user, user_public
from app.core.config import Settings, get_settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.mongodb import get_database
from app.models.schemas import TokenResponse, UserCreate, UserLogin

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    payload: UserCreate,
    request: Request,
    database=Depends(get_database),
    settings: Settings = Depends(get_settings),
):
    email = payload.email.lower().strip()
    user = {
        "_id": email,
        "name": payload.name,
        "email": email,
        "password_hash": hash_password(payload.password),
        "role": "farmer",
        "created_at": datetime.now(timezone.utc),
        "disabled": False,
    }
    try:
        await database.users.insert_one(user)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="An account with this email already exists"
        ) from exc
    token, token_id, expires = create_access_token(email, settings)
    return TokenResponse(access_token=token, user=user_public(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: UserLogin, database=Depends(get_database), settings: Settings = Depends(get_settings)
):
    email = payload.email.lower().strip()
    user = await database.users.find_one({"email": email, "disabled": {"$ne": True}})
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    token, _token_id, _expires = create_access_token(str(user["_id"]), settings)
    return TokenResponse(access_token=token, user=user_public(user))


@router.get("/me")
async def me(user=Depends(current_user)):
    return user_public(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, user=Depends(current_user), database=Depends(get_database)):
    claims = request.state.token_claims
    expires_at = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
    await database.revoked_tokens.update_one(
        {"_id": claims["jti"]},
        {"$setOnInsert": {"user_id": str(user["_id"]), "expires_at": expires_at}},
        upsert=True,
    )
