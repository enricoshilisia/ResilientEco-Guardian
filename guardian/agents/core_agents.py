"""
ResilientEco Guardian - Core Multi-Agent System
Upgraded to:
  - Route all LLM calls through Azure AI Foundry (model routing, RAI filters, observability)
  - True A2A (Agent-to-Agent) messaging via shared AgentMessage envelope
  - MCP tool calls for autonomous Azure resource management
  - Structured output passing between agents (not just text)
"""

import os
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone

from .foundry_client import foundry
from ..mcp.azure_mcp import mcp

logger = logging.getLogger(__name__)

# ─── SHARED JSON INSTRUCTION ───────────────────────────────────────────────────
_JSON_ONLY = (
    "Output ONLY a raw JSON object — no markdown, no code fences, no backticks, "
    "no explanation, no preamble. Start your response with { and end with }."
)


# ─── A2A MESSAGE ENVELOPE ──────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    session_id: str
    location: str
    lat: float
    lon: float
    user_query: str
    weather_data: dict = field(default_factory=dict)
    risk_assessment: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    action_plan: dict = field(default_factory=dict)
    governance_review: dict = field(default_factory=dict)
    mcp_actions: list = field(default_factory=list)
    agent_chain: list = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def log_step(self, agent: str, status: str, latency_ms: int, model: str = "", source: str = ""):
        self.agent_chain.append({
            "agent": agent,
            "status": status,
            "latency_ms": latency_ms,
            "model": model,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


# ─── BASE AGENT ────────────────────────────────────────────────────────────────

class BaseAgent:
    agent_type: str = "base"

    def run(self, msg: AgentMessage) -> AgentMessage:
        raise NotImplementedError

    def _complete(self, system: str, user: str, temperature: float = 0.4) -> dict:
        return foundry.complete(
            agent_type=self.agent_type,
            system_prompt=system,
            user_prompt=user,
            temperature=temperature,
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        import json, re
        # Strip markdown code fences if model ignores instructions
        text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.IGNORECASE)
        text = re.sub(r'\s*```$', '', text.strip())
        try:
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            return json.loads(json_match.group()) if json_match else {}
        except Exception:
            return {"raw": text}


# ─── MONITOR AGENT ─────────────────────────────────────────────────────────────

class MonitorAgent(BaseAgent):
    agent_type = "monitor"

    def run(self, msg: AgentMessage) -> AgentMessage:
        start = time.time()

        infra = mcp.execute("get_infrastructure_health", {})
        msg.mcp_actions.append({"agent": "monitor", "mcp_call": "get_infrastructure_health", "result": infra})

        history = mcp.execute("get_cosmos_agent_state", {
            "container": "risk_history",
            "location_id": msg.location,
            "hours_back": 72,
        })
        msg.mcp_actions.append({"agent": "monitor", "mcp_call": "get_cosmos_agent_state", "result": history})

        w = msg.weather_data or {}

        system = f"""You are the Monitor Agent in the ResilientEco Guardian climate intelligence system.
Your role: Analyze real-time weather sensor data and forecast data for anomalies and environmental signals.
{_JSON_ONLY}
Required keys:
  temperature_c (number),
  precipitation_mm (number, current),
  rain_24h_mm (number, last 24h total),
  humidity_pct (number),
  anomalies (list of strings),
  alert_signals (list of objects with: risk_level (integer 0-100), status (string)),
  data_quality_score (integer 0-100),
  today_evening_forecast (object with: precip_mm, precip_prob_pct, conditions),
  tomorrow_forecast (object with: precip_mm, temp_max, temp_min, conditions, precip_prob_pct)"""

        user = f"""Location: {msg.location} ({msg.lat}, {msg.lon})
User query: {msg.user_query}
Current weather & forecast data: {w}
Recent risk history (last 72h): {history.get('items', [])}
Infrastructure health: {infra.get('services', {})}

Analyze all data including today/tomorrow forecast and return JSON only."""

        result = self._complete(system, user, temperature=0.2)
        latency_ms = int((time.time() - start) * 1000)

        text = result["text"]
        parsed = self._parse_json(text)

        msg.weather_data["monitor_analysis"] = parsed
        msg.weather_data["monitor_text"] = text
        msg.log_step("monitor", "completed", latency_ms, result["model"], result["source"])
        return msg


# ─── PREDICT AGENT ─────────────────────────────────────────────────────────────

class PredictAgent(BaseAgent):
    agent_type = "predict"

    def run(self, msg: AgentMessage) -> AgentMessage:
        start = time.time()

        monitor_output = msg.weather_data.get("monitor_analysis", {})
        w = msg.weather_data

        system = f"""You are the Predict Agent in ResilientEco Guardian.
Your role: Use weather anomalies and forecast data from the Monitor Agent to forecast climate risks.
Consider time-of-day forecasts (morning/afternoon/evening/night) when answering time-specific queries.
{_JSON_ONLY}
Required keys:
  flood_risk (integer 0-100),
  drought_risk (integer 0-100),
  heatwave_risk (integer 0-100),
  overall_risk_level (string: low/medium/high/critical),
  confidence_pct (integer 0-100),
  primary_risk (string),
  reasoning (string, max 3 sentences, plain text)"""

        user = f"""Location: {msg.location}
User query: {msg.user_query}
Monitor Agent analysis: {monitor_output}
Raw weather & forecast data: {w}

Return JSON risk assessment only."""

        result = self._complete(system, user, temperature=0.3)
        latency_ms = int((time.time() - start) * 1000)

        text = result["text"]
        parsed = self._parse_json(text)
        parsed["text"] = text

        msg.risk_assessment = parsed
        msg.log_step("predict", "completed", latency_ms, result["model"], result["source"])
        return msg


# ─── DECISION AGENT ────────────────────────────────────────────────────────────

class DecisionAgent(BaseAgent):
    agent_type = "decision"

    def run(self, msg: AgentMessage) -> AgentMessage:
        start = time.time()

        risk = msg.risk_assessment
        flood_risk = risk.get("flood_risk", 0)

        if isinstance(flood_risk, (int, float)) and flood_risk >= 70:
            scale_result = mcp.execute("scale_aks_nodepool", {
                "resource_group": os.getenv("AZURE_RESOURCE_GROUP", "resilienteco-rg"),
                "cluster_name": os.getenv("AKS_CLUSTER_NAME", "resilienteco-aks"),
                "nodepool_name": "agentpool",
                "node_count": 5,
                "reason": f"High flood risk ({flood_risk}%) detected for {msg.location}",
            })
            msg.mcp_actions.append({
                "agent": "decision",
                "mcp_call": "scale_aks_nodepool",
                "trigger": f"flood_risk={flood_risk}%",
                "result": scale_result,
            })
            logger.info(f"[Decision] Auto-scaled AKS due to flood_risk={flood_risk}% in {msg.location}")

        system = f"""You are the Decision Agent in ResilientEco Guardian.
Your role: Given risk predictions, decide the alert level and response actions.
{_JSON_ONLY}
Required keys:
  alert_level (string: GREEN/YELLOW/ORANGE/RED),
  immediate_action_required (boolean),
  recommended_actions (list of strings),
  notify_groups (list of strings),
  priority (string: low/medium/high/critical),
  estimated_affected_population (integer),
  response_timeline_hours (integer)"""

        user = f"""Location: {msg.location}
User query: {msg.user_query}
Risk assessment from Predict Agent: {risk}
Monitor analysis: {msg.weather_data.get('monitor_analysis', {})}
Azure infrastructure actions taken: {msg.mcp_actions}

Return JSON decision only."""

        result = self._complete(system, user, temperature=0.4)
        latency_ms = int((time.time() - start) * 1000)

        text = result["text"]
        parsed = self._parse_json(text)
        parsed["text"] = text

        msg.decision = parsed
        msg.log_step("decision", "completed", latency_ms, result["model"], result["source"])
        return msg


# ─── ACTION AGENT ──────────────────────────────────────────────────────────────

class ActionAgent(BaseAgent):
    agent_type = "action"

    def run(self, msg: AgentMessage) -> AgentMessage:
        start = time.time()

        decision = msg.decision
        risk = msg.risk_assessment
        alert_level = decision.get("alert_level", "GREEN")

        if alert_level in ("ORANGE", "RED"):
            func_result = mcp.execute("trigger_azure_function", {
                "function_url": os.getenv("AZURE_ALERT_FUNCTION_URL", ""),
                "payload": {
                    "location": msg.location,
                    "alert_level": alert_level,
                    "risk_type": risk.get("primary_risk", "flood"),
                    "risk_level": risk.get("flood_risk", 0),
                    "message": decision.get("recommended_actions", []),
                    "session_id": msg.session_id,
                }
            })
            msg.mcp_actions.append({
                "agent": "action",
                "mcp_call": "trigger_azure_function",
                "trigger": f"alert_level={alert_level}",
                "result": func_result,
            })

        cosmos_result = mcp.execute("write_cosmos_risk_event", {
            "location": msg.location,
            "risk_type": risk.get("primary_risk", "unknown"),
            "risk_level": risk.get("flood_risk", 0),
            "agent_chain": [s["agent"] for s in msg.agent_chain],
            "metadata": {
                "session_id": msg.session_id,
                "alert_level": alert_level,
                "lat": msg.lat,
                "lon": msg.lon,
            }
        })
        msg.mcp_actions.append({
            "agent": "action",
            "mcp_call": "write_cosmos_risk_event",
            "result": cosmos_result,
        })

        system = f"""You are the Action Agent in ResilientEco Guardian.
Your role: Generate concrete, actionable alert messages and response protocols.
{_JSON_ONLY}
Required keys:
  alert_message (string, public-facing, max 2 sentences),
  sms_message (string, max 160 chars),
  risk_type (string),
  risk_level (integer 0-100),
  immediate_steps (list of strings),
  resources_needed (list of strings)"""

        user = f"""Location: {msg.location}
User query: {msg.user_query}
Decision from Decision Agent: {decision}
Risk from Predict Agent: {risk}
Azure actions already taken: {[a['mcp_call'] for a in msg.mcp_actions]}

Generate alert content. Return JSON only."""

        result = self._complete(system, user, temperature=0.5)
        latency_ms = int((time.time() - start) * 1000)

        text = result["text"]
        parsed = self._parse_json(text)
        parsed["text"] = text

        msg.action_plan = parsed
        msg.log_step("action", "completed", latency_ms, result["model"], result["source"])
        return msg


# ─── GOVERNANCE AGENT ──────────────────────────────────────────────────────────

class GovernanceAgent(BaseAgent):
    agent_type = "governance"

    def run(self, msg: AgentMessage) -> AgentMessage:
        start = time.time()

        monitor_logs = mcp.execute("query_azure_monitor", {
            "workspace_id": os.getenv("AZURE_LOG_WORKSPACE_ID", ""),
            "query": f"AppTraces | where Properties.location == '{msg.location}' | take 5",
            "hours_back": 1,
        })
        msg.mcp_actions.append({
            "agent": "governance",
            "mcp_call": "query_azure_monitor",
            "result": monitor_logs,
        })

        system = f"""You are the Governance Agent in ResilientEco Guardian — the responsible AI oversight layer.
Your role: Review the full agent chain output for accuracy, bias, proportionality, and responsible AI compliance.
Apply UN SDG principles (Climate Action, Reduced Inequalities).
{_JSON_ONLY}
Required keys:
  approved (boolean),
  issues (list of strings),
  rai_flags (list of strings),
  final_recommendation (string, max 2 sentences),
  confidence_in_chain (integer 0-100),
  sdg_alignment (list of strings)"""

        user = f"""Full agent chain review for {msg.location}:
User query: {msg.user_query}
Monitor analysis: {msg.weather_data.get('monitor_analysis', {})}
Risk assessment: {msg.risk_assessment}
Decision: {msg.decision}
Action plan: {msg.action_plan}
MCP Azure actions taken: {[a['mcp_call'] for a in msg.mcp_actions]}
Azure Monitor logs: {monitor_logs.get('rows', [])}
Agent chain audit: {msg.agent_chain}

Review for RAI compliance, proportionality, and SDG alignment. Return JSON only."""

        result = self._complete(system, user, temperature=0.3)
        latency_ms = int((time.time() - start) * 1000)

        text = result["text"]
        parsed = self._parse_json(text)
        parsed["text"] = text

        msg.governance_review = parsed
        msg.log_step("governance", "completed", latency_ms, result["model"], result["source"])
        return msg


# ─── ORCHESTRATOR ──────────────────────────────────────────────────────────────

def run_all_agents(user_query: str, lat: float, lon: float, city_name: str) -> dict:
    import uuid
    from ..services.weather_service import get_weather_summary

    session_id = str(uuid.uuid4())[:8]

    try:
        weather = get_weather_summary(lat, lon, city_name)
    except Exception as e:
        logger.error(f"Weather fetch failed: {e}")
        weather = {}

    msg = AgentMessage(
        session_id=session_id,
        location=city_name,
        lat=lat,
        lon=lon,
        user_query=user_query,
        weather_data=weather,
    )

    agents = [
        ("monitor",    MonitorAgent()),
        ("predict",    PredictAgent()),
        ("decision",   DecisionAgent()),
        ("action",     ActionAgent()),
        ("governance", GovernanceAgent()),
    ]

    results = {}

    for name, agent in agents:
        try:
            msg = agent.run(msg)
            if name == "monitor":
                results[name] = msg.weather_data.get("monitor_text", "Monitor complete.")
            elif name == "predict":
                results[name] = msg.risk_assessment.get("text", "Predict complete.")
            elif name == "decision":
                results[name] = msg.decision.get("text", "Decision complete.")
            elif name == "action":
                results[name] = msg.action_plan.get("text", "Action complete.")
            elif name == "governance":
                results[name] = msg.governance_review.get("text", "Governance complete.")
        except Exception as e:
            logger.exception(f"Agent {name} failed")
            results[name] = f"⚠️ {name} agent error: {str(e)}"

    if msg.mcp_actions:
        mcp_summary = []
        mcp_initialized = mcp._mgmt_client is not None
        for action in msg.mcp_actions:
            simulated = action.get("result", {}).get("simulated", True)
            tag = "[LIVE]" if (not simulated or mcp_initialized) else "[SIM]"
            mcp_summary.append(f"  {tag} {action['agent'].upper()} → {action['mcp_call']}")
        results["mcp_actions"] = "\n".join(mcp_summary)

    results["agent_chain"] = msg.agent_chain
    results["session_id"] = session_id

    return results