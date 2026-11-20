from datetime import datetime, timezone
from typing import Any

AGENT_NAMES = (
    "intake_agent",
    "weather_agent",
    "crop_agent",
    "market_agent",
    "scheme_agent",
    "risk_alert_agent",
    "advisor_coordinator",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_blackboard(
    database, *, session_id: str, user_id: str, farmer_input: dict[str, Any]
) -> None:
    now = utcnow()
    await database.blackboards.insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "farmer_input": farmer_input,
            "findings": {},
            "execution_status": {name: {"execution_status": "pending"} for name in AGENT_NAMES},
            "final_advisory": None,
        }
    )
    for name in AGENT_NAMES:
        await database.agent_executions.insert_one(
            {
                "session_id": session_id,
                "user_id": user_id,
                "agent_name": name,
                "execution_status": "pending",
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "result_summary": None,
                "error": None,
                "events": [],
            }
        )


async def get_blackboard(database, session_id: str, user_id: str) -> dict | None:
    return await database.blackboards.find_one(
        {"session_id": session_id, "user_id": user_id}, {"_id": 0}
    )


async def publish_finding(
    database, session_id: str, user_id: str, agent_name: str, data: Any
) -> None:
    now = utcnow()
    await database.blackboards.update_one(
        {"session_id": session_id, "user_id": user_id},
        {
            "$set": {
                f"findings.{agent_name}": {"agent": agent_name, "timestamp": now, "data": data},
                "updated_at": now,
            },
            "$inc": {"version": 1},
        },
    )


async def update_execution(
    database,
    session_id: str,
    user_id: str,
    agent_name: str,
    state: str,
    *,
    started_at=None,
    result_summary=None,
    error=None,
) -> None:
    now = utcnow()
    record = await database.agent_executions.find_one(
        {"session_id": session_id, "user_id": user_id, "agent_name": agent_name}
    )
    if record is None:
        return
    events = {"state": state, "timestamp": now, **({"error": error} if error else {})}
    update: dict[str, Any] = {
        "execution_status": state,
        "updated_at": now,
        "result_summary": result_summary,
        "error": error,
    }
    if state == "running":
        update.update(started_at=now, completed_at=None, duration_ms=None)
    elif state in {"completed", "failed"}:
        begun = started_at or record.get("started_at")
        update["completed_at"] = now
        update["duration_ms"] = max(0, int((now - begun).total_seconds() * 1000)) if begun else None
    await database.agent_executions.update_one(
        {"session_id": session_id, "user_id": user_id, "agent_name": agent_name},
        {"$set": update, "$push": {"events": events}},
    )
    await database.blackboards.update_one(
        {"session_id": session_id, "user_id": user_id},
        {
            "$set": {
                f"execution_status.{agent_name}": {
                    "execution_status": state,
                    "timestamp": now,
                    "error": error,
                },
                "updated_at": now,
            }
        },
    )


async def assemble_final(database, session_id: str, user_id: str, advisory: dict) -> None:
    now = utcnow()
    await database.blackboards.update_one(
        {"session_id": session_id, "user_id": user_id},
        {"$set": {"final_advisory": advisory, "updated_at": now}, "$inc": {"version": 1}},
    )


async def session_execution_rows(database, session_id: str, user_id: str) -> list[dict]:
    return await database.agent_executions.find(
        {"session_id": session_id, "user_id": user_id}, {"_id": 0}
    ).to_list(length=30)
