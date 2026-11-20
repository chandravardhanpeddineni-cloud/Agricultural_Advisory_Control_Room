import logging

from fastapi import Request
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

logger = logging.getLogger(__name__)
COLLECTIONS = (
    "users",
    "farmer_profiles",
    "advisory_sessions",
    "blackboards",
    "agent_executions",
    "risk_alerts",
    "knowledge_documents",
    "revoked_tokens",
)


async def connect_mongodb(app) -> None:
    settings = app.state.settings
    if not settings.mongodb_uri:
        raise RuntimeError("MONGODB_URI is required to start the production application")
    client = AsyncMongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    await client.admin.command("ping")
    database = client[settings.mongodb_database]
    await database.users.create_index([("email", ASCENDING)], unique=True)
    await database.farmer_profiles.create_index([("user_id", ASCENDING)], unique=True)
    await database.advisory_sessions.create_index([("session_id", ASCENDING)], unique=True)
    await database.advisory_sessions.create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)]
    )
    await database.blackboards.create_index([("session_id", ASCENDING)], unique=True)
    await database.agent_executions.create_index(
        [("session_id", ASCENDING), ("started_at", ASCENDING)]
    )
    await database.risk_alerts.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await database.knowledge_documents.create_index(
        [("status", ASCENDING), ("created_at", DESCENDING)]
    )
    await database.revoked_tokens.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    app.state.mongo_client = client
    app.state.db = database
    logger.info("MongoDB connected; application indexes are ready")


async def close_mongodb(app) -> None:
    client = getattr(app.state, "mongo_client", None)
    if client:
        await client.close()


def get_database(request: Request):
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise RuntimeError("MongoDB is unavailable")
    return database
