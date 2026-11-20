import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pwdlib import PasswordHash

from app.api import advisory, auth, farmer, knowledge, weather
from app.core.config import get_settings
from app.db.mongodb import close_mongodb, connect_mongodb
from app.services.chroma import ChromaKnowledgeBase
from app.services.providers import MARKET_DATA_FILE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = get_settings()
    await connect_mongodb(app)
    app.state.knowledge_base = ChromaKnowledgeBase(app.state.settings)
    if app.state.settings.bootstrap_admin_email and app.state.settings.bootstrap_admin_password:
        email = app.state.settings.bootstrap_admin_email.lower().strip()
        password = app.state.settings.bootstrap_admin_password.get_secret_value()
        if len(password) < 16:
            raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must contain at least 16 characters")
        await app.state.db.users.update_one(
            {"email": email},
            {
                "$setOnInsert": {
                    "_id": email,
                    "email": email,
                    "name": "Administrator",
                    "password_hash": PasswordHash.recommended().hash(password),
                    "role": "admin",
                    "created_at": datetime.now(timezone.utc),
                    "disabled": False,
                }
            },
            upsert=True,
        )
    yield
    await close_mongodb(app)


app = FastAPI(title="Agricultural Advisory Control Room API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(auth.router, prefix="/api")
app.include_router(farmer.router, prefix="/api")
app.include_router(advisory.router, prefix="/api")
app.include_router(knowledge.router, prefix="/api")
app.include_router(weather.router, prefix="/api")


@app.get("/api/health")
async def health(request: Request):
    try:
        await request.app.state.db.command("ping")
        knowledge_count = await request.app.state.knowledge_base.count()
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="A required database service is unavailable"
        ) from exc
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": "agricultural-advisory-api",
        "components": {"mongodb": "ok", "chroma": "ok", "indexed_chunks": knowledge_count},
        "providers": {
            "llm": settings.llm_configured,
            "weather": bool(settings.weather_api_url),
            "market": MARKET_DATA_FILE.is_file(),
        },
    }
