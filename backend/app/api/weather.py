from fastapi import APIRouter, Depends, Request

from app.api.dependencies import current_user
from app.db.mongodb import get_database
from app.services.providers import fetch_weather

router = APIRouter(prefix="/weather", tags=["weather"])


@router.get("/local")
async def local_weather(
    request: Request,
    user=Depends(current_user),
    database=Depends(get_database),
):
    user_id = str(user["_id"])
    profile = await database.farmer_profiles.find_one({"user_id": user_id}, {"_id": 0})
    location = (profile or {}).get("location")
    if not location:
        recent = await database.advisory_sessions.find_one(
            {"user_id": user_id},
            {"_id": 0, "location": 1},
            sort=[("created_at", -1)],
        )
        location = (recent or {}).get("location")
    if not location:
        return {
            "available": False,
            "status": "unavailable",
            "location": None,
            "reason_unavailable": "Add a farm location in your profile to check local weather.",
        }

    data = await fetch_weather(
        request.app.state.settings,
        {"location": location, "crop": "General", "crop_stage": "Current"},
    )
    return {**data, "location": location}
