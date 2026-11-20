import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.agents.workflow import execute_workflow
from app.api.dependencies import current_user
from app.db.mongodb import get_database
from app.models.schemas import AdvisoryRequest
from app.services.blackboard import create_blackboard, session_execution_rows, utcnow

router = APIRouter(prefix="/advisory", tags=["advisories"])


def _owner(user: dict) -> str:
    return str(user["_id"])


async def _session(database, session_id: str, user_id: str):
    row = await database.advisory_sessions.find_one(
        {"session_id": session_id, "user_id": user_id}, {"_id": 0}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Advisory session not found")
    return row


@router.post("/create", status_code=status.HTTP_202_ACCEPTED)
async def create_advisory(
    payload: AdvisoryRequest,
    background: BackgroundTasks,
    request: Request,
    user=Depends(current_user),
    database=Depends(get_database),
):
    user_id = _owner(user)
    session_id = str(uuid.uuid4())
    now = utcnow()
    farmer_input = payload.model_dump()
    await database.advisory_sessions.insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            **farmer_input,
            "created_at": now,
            "overall_status": "pending",
            "risk_level": None,
            "final_advisory": None,
        }
    )
    await create_blackboard(
        database, session_id=session_id, user_id=user_id, farmer_input=farmer_input
    )
    background.add_task(
        execute_workflow,
        database,
        request.app.state.settings,
        request.app.state.knowledge_base,
        session_id,
        user_id,
    )
    return {"session_id": session_id, "overall_status": "pending", "created_at": now}


@router.get("/history")
async def advisory_history(user=Depends(current_user), database=Depends(get_database)):
    rows = (
        await database.advisory_sessions.find(
            {"user_id": _owner(user)}, {"_id": 0, "final_advisory": 0}
        )
        .sort("created_at", -1)
        .limit(100)
        .to_list(length=100)
    )
    return rows


@router.get("/{session_id}")
async def advisory_detail(
    session_id: str, user=Depends(current_user), database=Depends(get_database)
):
    return await _session(database, session_id, _owner(user))


@router.get("/{session_id}/status")
async def advisory_status(
    session_id: str, user=Depends(current_user), database=Depends(get_database)
):
    row = await _session(database, session_id, _owner(user))
    return {
        "session_id": session_id,
        "overall_status": row["overall_status"],
        "risk_level": row.get("risk_level"),
        "agents": await session_execution_rows(database, session_id, _owner(user)),
    }


@router.get("/{session_id}/agents")
async def advisory_agents(
    session_id: str, user=Depends(current_user), database=Depends(get_database)
):
    await _session(database, session_id, _owner(user))
    return await session_execution_rows(database, session_id, _owner(user))


@router.get("/{session_id}/blackboard")
async def advisory_blackboard(
    session_id: str, user=Depends(current_user), database=Depends(get_database)
):
    await _session(database, session_id, _owner(user))
    board = await database.blackboards.find_one(
        {"session_id": session_id, "user_id": _owner(user)}, {"_id": 0}
    )
    return board


@router.get("/{session_id}/timeline")
async def advisory_timeline(
    session_id: str, user=Depends(current_user), database=Depends(get_database)
):
    await _session(database, session_id, _owner(user))
    executions = await session_execution_rows(database, session_id, _owner(user))
    return sorted(
        [
            {"agent_name": row["agent_name"], **event}
            for row in executions
            for event in row.get("events", [])
        ],
        key=lambda event: event.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc),
    )


@router.get("/{session_id}/pdf")
async def advisory_pdf(session_id: str, user=Depends(current_user), database=Depends(get_database)):
    row = await _session(database, session_id, _owner(user))
    advisory = row.get("final_advisory")
    if not advisory:
        raise HTTPException(status_code=409, detail="The advisory is not ready yet")
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=letter)
    width, height = letter
    y = height - 56
    pdf.setTitle("Agricultural Advisory")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(48, y, "Agricultural Advisory")
    y -= 30
    pdf.setFont("Helvetica", 10)
    sections = [
        ("Farmer", f"{row['crop']} · {row['location']} · {row['crop_stage']}"),
        ("Summary", advisory.get("farmer_summary", "")),
        ("Field action", advisory.get("field_action", "")),
        ("Weather", advisory.get("weather_summary", "")),
        ("Market", advisory.get("market_recommendation", "")),
        ("Reasoning", advisory.get("reasoning", "")),
        ("Limitations", "\n".join(advisory.get("limitations", [])) or "None reported"),
    ]
    for title, text in sections:
        if y < 80:
            pdf.showPage()
            y = height - 56
            pdf.setFont("Helvetica", 10)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(48, y, title)
        y -= 15
        pdf.setFont("Helvetica", 10)
        for line in str(text).splitlines() or [""]:
            for start in range(0, len(line), 95):
                if y < 60:
                    pdf.showPage()
                    y = height - 56
                    pdf.setFont("Helvetica", 10)
                pdf.drawString(48, y, line[start : start + 95])
                y -= 13
        y -= 10
    pdf.save()
    stream.seek(0)
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="advisory-{session_id}.pdf"'},
    )
