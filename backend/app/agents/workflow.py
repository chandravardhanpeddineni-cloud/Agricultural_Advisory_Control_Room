import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Never

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler
from pydantic import BaseModel, Field

from app.agents.prompts import (
    ADVISOR_INSTRUCTIONS,
    CROP_INSTRUCTIONS,
    MARKET_INSTRUCTIONS,
    RISK_INSTRUCTIONS,
    SCHEME_INSTRUCTIONS,
    WEATHER_INSTRUCTIONS,
)
from app.core.config import Settings
from app.models.schemas import (
    CropFinding,
    FinalAdvisory,
    RiskFinding,
    SchemeFinding,
    SchemeMatch,
    SourceReference,
)
from app.services.blackboard import (
    AGENT_NAMES,
    assemble_final,
    get_blackboard,
    publish_finding,
    update_execution,
    utcnow,
)
from app.services.chroma import ChromaKnowledgeBase
from app.services.llm import create_framework_agent, run_structured
from app.services.providers import fetch_market, fetch_weather

logger = logging.getLogger(__name__)


class WorkflowRequest(BaseModel):
    session_id: str
    user_id: str


class WorkflowPacket(BaseModel):
    session_id: str
    user_id: str


class WeatherImpact(BaseModel):
    agricultural_impact: str


class MarketDecision(BaseModel):
    recommendation: str = Field(pattern="^(sell|hold|monitor)$")
    reason: str


class SchemeAnswer(BaseModel):
    available: bool
    matches: list[dict[str, Any]] = Field(default_factory=list)
    limitation: str | None = None


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item),
    )


async def _record_start(database, packet: WorkflowPacket, name: str) -> datetime:
    started = utcnow()
    await update_execution(
        database, packet.session_id, packet.user_id, name, "running", started_at=started
    )
    return started


async def _record_failure(
    database,
    packet: WorkflowPacket,
    name: str,
    started: datetime,
    error: Exception,
    data: dict | None = None,
) -> None:
    safe_error = f"{type(error).__name__}: {str(error)[:400]}"
    logger.exception("Agent %s failed for advisory session %s", name, packet.session_id)
    if data is not None:
        await publish_finding(database, packet.session_id, packet.user_id, name, data)
    await update_execution(
        database,
        packet.session_id,
        packet.user_id,
        name,
        "failed",
        started_at=started,
        error=safe_error,
    )


class IntakeExecutor(Executor):
    def __init__(self, database):
        super().__init__(id="intake_agent")
        self.database = database

    @handler
    async def handle(self, request: WorkflowRequest, ctx: WorkflowContext[WorkflowPacket]) -> None:
        packet = WorkflowPacket(session_id=request.session_id, user_id=request.user_id)
        await self.database.advisory_sessions.update_one(
            {"session_id": packet.session_id, "user_id": packet.user_id},
            {"$set": {"overall_status": "running"}},
        )
        started = await _record_start(self.database, packet, "intake_agent")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        if board is None:
            await _record_failure(
                self.database, packet, "intake_agent", started, ValueError("Session not found")
            )
            return
        await publish_finding(
            self.database, packet.session_id, packet.user_id, "intake_agent", board["farmer_input"]
        )
        await update_execution(
            self.database,
            packet.session_id,
            packet.user_id,
            "intake_agent",
            "completed",
            started_at=started,
            result_summary="Farmer input validated and attached to this advisory session",
        )
        await ctx.send_message(packet)


class WeatherExecutor(Executor):
    def __init__(self, database, settings):
        super().__init__(id="weather_agent")
        self.database, self.settings = database, settings
        self.agent = create_framework_agent(
            settings, name="WeatherAgent", instructions=WEATHER_INSTRUCTIONS
        )

    @handler
    async def handle(self, packet: WorkflowPacket, ctx: WorkflowContext[WorkflowPacket]) -> None:
        started = await _record_start(self.database, packet, "weather_agent")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        try:
            data = await fetch_weather(self.settings, board["farmer_input"])
            if data["available"]:
                analysis = await run_structured(self.agent, _json(data), WeatherImpact)
                data["agricultural_impact"] = analysis.agricultural_impact
                data["sources"] = [
                    SourceReference(
                        title="Weather forecast",
                        source=data.get("source") or "Configured weather provider",
                        retrieved_at=data.get("retrieved_at"),
                    ).model_dump(mode="json")
                ]
            else:
                data["agricultural_impact"] = None
            await publish_finding(
                self.database, packet.session_id, packet.user_id, "weather_agent", data
            )
            state = "completed" if data["available"] else "failed"
            err = None if data["available"] else data.get("reason_unavailable")
            await update_execution(
                self.database,
                packet.session_id,
                packet.user_id,
                "weather_agent",
                state,
                started_at=started,
                result_summary=data.get("summary") or data.get("reason_unavailable"),
                error=err,
            )
        except Exception as exc:
            await _record_failure(
                self.database,
                packet,
                "weather_agent",
                started,
                exc,
                {
                    "available": False,
                    "status": "failed",
                    "reason_unavailable": "Weather analysis failed; no forecast is available.",
                },
            )
        await ctx.send_message(packet)


class CropExecutor(Executor):
    def __init__(self, database, settings):
        super().__init__(id="crop_agent")
        self.database = database
        self.agent = create_framework_agent(
            settings, name="CropAgent", instructions=CROP_INSTRUCTIONS
        )

    @handler
    async def handle(self, packet: WorkflowPacket, ctx: WorkflowContext[WorkflowPacket]) -> None:
        started = await _record_start(self.database, packet, "crop_agent")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        try:
            farmer = board["farmer_input"]
            weather = (
                board.get("findings", {})
                .get("weather_agent", {})
                .get(
                    "data",
                    {
                        "available": False,
                        "reason_unavailable": "Weather Agent did not publish a forecast.",
                    },
                )
            )
            prompt = {
                "farmer": farmer,
                "weather_finding": weather,
                "task": "Recommend a conservative crop-stage action. Do not infer unavailable weather.",
            }
            result = await run_structured(self.agent, _json(prompt), CropFinding)
            data = result.model_dump(mode="json")
            await publish_finding(
                self.database, packet.session_id, packet.user_id, "crop_agent", data
            )
            await update_execution(
                self.database,
                packet.session_id,
                packet.user_id,
                "crop_agent",
                "completed",
                started_at=started,
                result_summary=result.field_action[:240],
            )
        except Exception as exc:
            await _record_failure(
                self.database,
                packet,
                "crop_agent",
                started,
                exc,
                {
                    "status": "unavailable",
                    "reason": "Crop analysis unavailable. Confirm guidance with a local extension officer.",
                },
            )
        await ctx.send_message(packet)


class MarketExecutor(Executor):
    def __init__(self, database, settings):
        super().__init__(id="market_agent")
        self.database, self.settings = database, settings
        self.agent = create_framework_agent(
            settings, name="MarketAgent", instructions=MARKET_INSTRUCTIONS
        )

    @handler
    async def handle(self, packet: WorkflowPacket, ctx: WorkflowContext[WorkflowPacket]) -> None:
        started = await _record_start(self.database, packet, "market_agent")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        farmer = board["farmer_input"]
        try:
            source = await fetch_market(self.settings, farmer)
            if not source["available"]:
                await publish_finding(
                    self.database, packet.session_id, packet.user_id, "market_agent", source
                )
                await update_execution(
                    self.database,
                    packet.session_id,
                    packet.user_id,
                    "market_agent",
                    "failed",
                    started_at=started,
                    error=source.get("reason"),
                    result_summary="Live market data unavailable",
                )
            else:
                prompt = {
                    "provider_record": source,
                    "task": "Use only the supplied market records. If historical trend is insufficient_data, recommend monitor and state that one dated snapshot cannot support a sell-or-hold trend. Explain whether the records are location-matched or an India-wide fallback, and identify the data source. Do not repeat or alter provider prices.",
                }
                decision = await run_structured(self.agent, _json(prompt), MarketDecision)
                data = {
                    **source,
                    "recommendation": decision.recommendation,
                    "reason": decision.reason,
                    "sources": [
                        SourceReference(
                            title=f"{source['market']} market price",
                            source=source["source"],
                            retrieved_at=source.get("retrieved_at"),
                        ).model_dump(mode="json")
                    ],
                }
                await publish_finding(
                    self.database, packet.session_id, packet.user_id, "market_agent", data
                )
                await update_execution(
                    self.database,
                    packet.session_id,
                    packet.user_id,
                    "market_agent",
                    "completed",
                    started_at=started,
                    result_summary=f"{source['market']}: {source['current_price']} {source['unit']} on {source['price_date']}; trend unavailable",
                )
        except Exception as exc:
            await _record_failure(
                self.database,
                packet,
                "market_agent",
                started,
                exc,
                {
                    "available": False,
                    "status": "failed",
                    "crop": farmer["crop"],
                    "reason": "Market analysis failed; no recommendation is available.",
                },
            )
        await ctx.send_message(packet)


class SchemeExecutor(Executor):
    def __init__(self, database, settings, knowledge_base: ChromaKnowledgeBase):
        super().__init__(id="scheme_agent")
        self.database, self.knowledge_base = database, knowledge_base
        self.agent = create_framework_agent(
            settings, name="SchemeAgent", instructions=SCHEME_INSTRUCTIONS
        )

    @handler
    async def handle(self, packet: WorkflowPacket, ctx: WorkflowContext[WorkflowPacket]) -> None:
        started = await _record_start(self.database, packet, "scheme_agent")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        farmer = board["farmer_input"]
        try:
            query = f"Government agricultural schemes for crop {farmer['crop']} in {farmer['location']}; stage {farmer['crop_stage']}; land area {farmer['land_area']} hectares; soil {farmer.get('soil_type') or 'unspecified'}"
            passages = await self.knowledge_base.search(query, limit=6)
            if not passages:
                data = {
                    "available": False,
                    "status": "unavailable",
                    "matches": [],
                    "retrieved_sources": [],
                    "reason_unavailable": "No scheme documents are indexed. Ask an administrator to upload current official material.",
                }
                await publish_finding(
                    self.database, packet.session_id, packet.user_id, "scheme_agent", data
                )
                await update_execution(
                    self.database,
                    packet.session_id,
                    packet.user_id,
                    "scheme_agent",
                    "failed",
                    started_at=started,
                    error=data["reason_unavailable"],
                    result_summary="No indexed scheme documents",
                )
            elif self.agent is None:
                sources = [
                    {
                        "title": match["metadata"].get("title", "Untitled source"),
                        "source": match["metadata"].get("source_name", "Uploaded document"),
                        "url": match["metadata"].get("source_url") or None,
                        "excerpt": match["text"],
                        "page": match["metadata"].get("page"),
                        "retrieved_at": utcnow(),
                    }
                    for match in passages
                ]
                data = {
                    "available": False,
                    "status": "unavailable",
                    "matches": [],
                    "retrieved_sources": sources,
                    "reason_unavailable": "Source passages were retrieved, but scheme synthesis needs a configured LLM. Eligibility is not established.",
                }
                await publish_finding(
                    self.database, packet.session_id, packet.user_id, "scheme_agent", data
                )
                await update_execution(
                    self.database,
                    packet.session_id,
                    packet.user_id,
                    "scheme_agent",
                    "failed",
                    started_at=started,
                    error="LLM provider is not configured",
                    result_summary=f"Retrieved {len(sources)} source passage(s); no eligibility claim made",
                )
            else:
                prompt = {
                    "farmer": farmer,
                    "retrieved_passages": passages,
                    "task": "Summarize relevant schemes only when these passages identify them. Cite passage indexes. Do not assert eligibility unless every stated criterion is evidenced; otherwise require verification.",
                }
                answer = await run_structured(self.agent, _json(prompt), SchemeAnswer)
                refs = [
                    SourceReference(
                        title=p["metadata"].get("title", "Untitled source"),
                        source=p["metadata"].get("source_name", "Uploaded document"),
                        url=p["metadata"].get("source_url") or None,
                        excerpt=p["text"][:700],
                        page=p["metadata"].get("page"),
                        retrieved_at=utcnow(),
                    )
                    for p in passages
                ]
                allowed = {reference.title for reference in refs}
                matches = []
                for raw in answer.matches:
                    if raw.get("name") in allowed:
                        matches.append(
                            SchemeMatch(
                                name=raw["name"],
                                eligibility=raw.get(
                                    "eligibility",
                                    "Eligibility requires verification with the issuing department.",
                                ),
                                benefits=raw.get(
                                    "benefits", "Not established by the retrieved excerpts."
                                ),
                                application_steps=raw.get("application_steps", []),
                                sources=[ref for ref in refs if ref.title == raw["name"]],
                                needs_verification=True,
                            )
                        )
                data = SchemeFinding(
                    available=bool(matches),
                    status="available" if matches else "unavailable",
                    matches=matches,
                    retrieved_sources=refs,
                    reason_unavailable=answer.limitation if not matches else None,
                ).model_dump(mode="json")
                await publish_finding(
                    self.database, packet.session_id, packet.user_id, "scheme_agent", data
                )
                await update_execution(
                    self.database,
                    packet.session_id,
                    packet.user_id,
                    "scheme_agent",
                    "completed" if matches else "failed",
                    started_at=started,
                    error=None if matches else answer.limitation,
                    result_summary=f"Retrieved {len(passages)} source passage(s)",
                )
        except Exception as exc:
            await _record_failure(
                self.database,
                packet,
                "scheme_agent",
                started,
                exc,
                {
                    "available": False,
                    "status": "failed",
                    "matches": [],
                    "retrieved_sources": [],
                    "reason_unavailable": "Scheme retrieval failed; no scheme or eligibility is asserted.",
                },
            )
        await ctx.send_message(packet)


class RiskExecutor(Executor):
    def __init__(self, database, settings):
        super().__init__(id="risk_alert_agent")
        self.database = database
        self.agent = create_framework_agent(
            settings, name="RiskAlertAgent", instructions=RISK_INSTRUCTIONS
        )

    @handler
    async def handle(self, packet: WorkflowPacket, ctx: WorkflowContext[WorkflowPacket]) -> None:
        started = await _record_start(self.database, packet, "risk_alert_agent")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        farmer = board["farmer_input"]
        weather = board.get("findings", {}).get("weather_agent", {}).get("data", {})
        try:
            if not weather.get("available"):
                result = RiskFinding(
                    risk_level="UNASSESSED",
                    description="Risk could not be assessed because no verified weather forecast is available.",
                )
            elif self.agent:
                prompt = {
                    "crop": farmer["crop"],
                    "stage": farmer["crop_stage"],
                    "weather_finding": weather,
                    "task": "Assess crop-specific risk from measured evidence; return UNASSESSED when evidence is insufficient.",
                }
                result = await run_structured(self.agent, _json(prompt), RiskFinding)
            else:
                rain = weather.get("rainfall_next_48h_mm")
                temp = weather.get("temperature_c")
                probability = weather.get("precipitation_probability_percent")
                evidence = []
                if rain is not None:
                    evidence.append(f"Provider rainfall forecast next 48h: {rain} mm")
                if probability is not None:
                    evidence.append(f"Provider precipitation probability: {probability}%")
                if temp is not None:
                    evidence.append(f"Provider temperature: {temp}°C")
                level, kind, actions = "UNASSESSED", None, []
                if rain is not None and rain >= 100:
                    level, kind, actions = (
                        "HIGH",
                        "heavy rainfall",
                        ["Check field drainage", "Inspect for standing water after rainfall"],
                    )
                elif temp is not None and temp >= 42:
                    level, kind, actions = (
                        "HIGH",
                        "extreme heat",
                        [
                            "Check crop and soil moisture",
                            "Seek local extension guidance on heat protection",
                        ],
                    )
                elif (
                    rain is not None
                    and rain >= 50
                    and probability is not None
                    and probability >= 70
                ):
                    level, kind, actions = (
                        "MODERATE",
                        "heavy rainfall",
                        ["Monitor drainage", "Check the verified forecast before field operations"],
                    )
                result = RiskFinding(
                    risk_level=level,
                    risk_type=kind,
                    description="Rule-based risk triage from provider measurements; verify with local extension services."
                    if level != "UNASSESSED"
                    else "Weather data lacks measurable risk fields; risk is unassessed.",
                    preventive_actions=actions,
                    evidence=evidence,
                    sources=[
                        SourceReference(
                            title="Weather provider observation",
                            source=weather.get("source", "Configured weather provider"),
                            retrieved_at=weather.get("retrieved_at"),
                        )
                    ],
                )
            data = result.model_dump(mode="json")
            await publish_finding(
                self.database, packet.session_id, packet.user_id, "risk_alert_agent", data
            )
            if result.risk_level in {"HIGH", "CRITICAL"}:
                await self.database.risk_alerts.insert_one(
                    {
                        "session_id": packet.session_id,
                        "user_id": packet.user_id,
                        **data,
                        "created_at": utcnow(),
                    }
                )
            await update_execution(
                self.database,
                packet.session_id,
                packet.user_id,
                "risk_alert_agent",
                "completed",
                started_at=started,
                result_summary=f"Risk level: {result.risk_level}",
            )
        except Exception as exc:
            await _record_failure(
                self.database,
                packet,
                "risk_alert_agent",
                started,
                exc,
                {
                    "risk_level": "UNASSESSED",
                    "description": "Risk analysis failed; no alert could be assessed.",
                },
            )
        await ctx.send_message(packet)


class AdvisorExecutor(Executor):
    def __init__(self, database, settings):
        super().__init__(id="advisor_coordinator")
        self.database = database
        self.agent = create_framework_agent(
            settings, name="AdvisorCoordinator", instructions=ADVISOR_INSTRUCTIONS
        )

    @handler
    async def handle(
        self, packets: list[WorkflowPacket], ctx: WorkflowContext[Never, WorkflowPacket]
    ) -> None:
        packet = packets[0]
        started = await _record_start(self.database, packet, "advisor_coordinator")
        board = await get_blackboard(self.database, packet.session_id, packet.user_id)
        findings = board.get("findings", {})
        limitations = []
        for name in AGENT_NAMES:
            execution = board.get("execution_status", {}).get(name, {})
            if execution.get("execution_status") == "failed":
                limitations.append(
                    f"{name.replace('_', ' ').title()} did not complete successfully; its information may be unavailable."
                )
        risk_data = findings.get("risk_alert_agent", {}).get("data", {})
        try:
            if self.agent:
                result = await run_structured(
                    self.agent,
                    _json(
                        {
                            "farmer": board["farmer_input"],
                            "validated_agent_findings": findings,
                            "limitations": limitations,
                        }
                    ),
                    FinalAdvisory,
                )
            else:
                weather = findings.get("weather_agent", {}).get("data", {})
                crop = findings.get("crop_agent", {}).get("data", {})
                market = findings.get("market_agent", {}).get("data", {})
                scheme = findings.get("scheme_agent", {}).get("data", {})
                risks = (
                    [RiskFinding.model_validate(risk_data)]
                    if risk_data.get("risk_level") in {"HIGH", "CRITICAL"}
                    else []
                )
                if not weather.get("available"):
                    limitations.append(
                        weather.get("reason_unavailable", "Weather forecast unavailable.")
                    )
                if not market.get("available"):
                    limitations.append(market.get("reason", "Market data unavailable."))
                if not scheme.get("available"):
                    limitations.append(
                        scheme.get(
                            "reason_unavailable", "Scheme eligibility could not be verified."
                        )
                    )
                result = FinalAdvisory(
                    farmer_summary=f"Advisory for {board['farmer_input']['crop']} at {board['farmer_input']['location']}.",
                    field_action=crop.get(
                        "field_action",
                        "Crop guidance is unavailable until the model provider is configured. Confirm advice with a local agricultural extension officer.",
                    ),
                    weather_summary=weather.get(
                        "summary",
                        weather.get(
                            "reason_unavailable", "No verified weather forecast is available."
                        ),
                    ),
                    market_recommendation=market.get(
                        "reason",
                        "No verified mandi price data is available; no sell-or-hold recommendation is made.",
                    ),
                    schemes=[
                        SchemeMatch.model_validate(item) for item in scheme.get("matches", [])
                    ],
                    risk_alerts=risks,
                    reasoning=crop.get(
                        "reasoning",
                        "AI crop reasoning is unavailable until the model provider is configured.",
                    ),
                    sources=[
                        SourceReference.model_validate(source)
                        for finding in findings.values()
                        for source in finding.get("data", {}).get("sources", [])
                    ],
                    limitations=list(dict.fromkeys(limitations)),
                )
            if risk_data.get("risk_level") in {"HIGH", "CRITICAL"}:
                if not any(
                    alert.risk_level == risk_data["risk_level"] for alert in result.risk_alerts
                ):
                    result.risk_alerts.append(RiskFinding.model_validate(risk_data))
                result.farmer_summary = f"{risk_data.get('risk_type') or 'Weather'} risk alert: {risk_data.get('description', 'Check the risk details below.')}. {result.farmer_summary}"
            source_map = {}
            for finding in findings.values():
                data = finding.get("data", {})
                for source in [*data.get("sources", []), *data.get("retrieved_sources", [])]:
                    try:
                        ref = SourceReference.model_validate(source)
                        source_map[(ref.title, ref.source, ref.url)] = ref
                    except Exception:
                        continue
                for scheme in data.get("matches", []):
                    for source in scheme.get("sources", []):
                        try:
                            ref = SourceReference.model_validate(source)
                            source_map[(ref.title, ref.source, ref.url)] = ref
                        except Exception:
                            continue
            result.sources = list(source_map.values())
            result.limitations = list(dict.fromkeys([*result.limitations, *limitations]))
            final_data = result.model_dump(mode="json")
            await assemble_final(self.database, packet.session_id, packet.user_id, final_data)
            failures = await self.database.agent_executions.count_documents(
                {
                    "session_id": packet.session_id,
                    "user_id": packet.user_id,
                    "execution_status": "failed",
                }
            )
            overall = "partial" if failures else "completed"
            await self.database.advisory_sessions.update_one(
                {"session_id": packet.session_id, "user_id": packet.user_id},
                {
                    "$set": {
                        "final_advisory": final_data,
                        "overall_status": overall,
                        "completed_at": utcnow(),
                        "risk_level": risk_data.get("risk_level"),
                    }
                },
            )
            await update_execution(
                self.database,
                packet.session_id,
                packet.user_id,
                "advisor_coordinator",
                "completed",
                started_at=started,
                result_summary="Consolidated advisory saved to the session",
            )
        except Exception as exc:
            await _record_failure(self.database, packet, "advisor_coordinator", started, exc)
            await self.database.advisory_sessions.update_one(
                {"session_id": packet.session_id, "user_id": packet.user_id},
                {"$set": {"overall_status": "failed", "completed_at": utcnow()}},
            )
        await ctx.yield_output(packet)


def build_workflow(database, settings: Settings, knowledge_base: ChromaKnowledgeBase):
    intake = IntakeExecutor(database)
    weather = WeatherExecutor(database, settings)
    crop = CropExecutor(database, settings)
    market = MarketExecutor(database, settings)
    scheme = SchemeExecutor(database, settings, knowledge_base)
    risk = RiskExecutor(database, settings)
    advisor = AdvisorExecutor(database, settings)
    workflow = (
        WorkflowBuilder(start_executor=intake)
        .add_edge(intake, weather)
        .add_fan_out_edges(weather, [crop, market, scheme])
        .add_edge(crop, risk)
        .add_fan_in_edges([risk, market, scheme], advisor)
        .build()
    )
    return workflow, [
        executor.agent
        for executor in [weather, crop, market, scheme, risk, advisor]
        if executor.agent is not None
    ]


async def execute_workflow(
    database, settings: Settings, knowledge_base: ChromaKnowledgeBase, session_id: str, user_id: str
) -> None:
    workflow = None
    agents = []
    try:
        workflow, agents = build_workflow(database, settings, knowledge_base)
        events = await asyncio.wait_for(
            workflow.run(WorkflowRequest(session_id=session_id, user_id=user_id)), timeout=150
        )
        if not events.get_outputs():
            raise RuntimeError("Agent Framework workflow ended without an advisor result")
    except Exception as exc:
        logger.exception("Advisory workflow failed for session %s", session_id)
        await database.advisory_sessions.update_one(
            {"session_id": session_id, "user_id": user_id},
            {
                "$set": {
                    "overall_status": "failed",
                    "completed_at": utcnow(),
                    "workflow_error": f"{type(exc).__name__}: {str(exc)[:400]}",
                }
            },
        )
        await database.agent_executions.update_many(
            {
                "session_id": session_id,
                "user_id": user_id,
                "execution_status": {"$in": ["pending", "running"]},
            },
            {
                "$set": {
                    "execution_status": "failed",
                    "error": "Workflow stopped before this agent completed",
                    "completed_at": utcnow(),
                },
                "$push": {
                    "events": {
                        "state": "failed",
                        "timestamp": utcnow(),
                        "error": "Workflow stopped before this agent completed",
                    }
                },
            },
        )
        await database.blackboards.update_one(
            {"session_id": session_id, "user_id": user_id},
            {
                "$set": {
                    "execution_status": {
                        name: {"execution_status": "failed"} for name in AGENT_NAMES
                    }
                }
            },
        )
    finally:
        for agent in agents:
            try:
                await agent.close()
            except Exception:
                logger.warning("Agent cleanup failed for session %s", session_id, exc_info=True)
