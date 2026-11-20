from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.api.dependencies import current_user
from app.db.mongodb import get_database
from app.models.schemas import FarmerProfileInput

router = APIRouter(prefix="/farmer/profile", tags=["farmer profile"])


@router.get("")
async def get_profile(user=Depends(current_user), database=Depends(get_database)):
    profile = await database.farmer_profiles.find_one({"user_id": str(user["_id"])}, {"_id": 0})
    return profile


@router.put("")
async def upsert_profile(
    payload: FarmerProfileInput, user=Depends(current_user), database=Depends(get_database)
):
    user_id = str(user["_id"])
    document = {
        **payload.model_dump(),
        "user_id": user_id,
        "updated_at": datetime.now(timezone.utc),
    }
    await database.farmer_profiles.update_one({"user_id": user_id}, {"$set": document}, upsert=True)
    return document
