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
from dataclasses import dataclass, field, asdict, fields as dataclass_fields
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
    # Intent classification
    intent_classification: str = "general_forecast"
    intent_confidence: float = 0.0
    intent_source: str = "keyword_fallback"
    # Graph selected by type-based routing
    selected_graph: str = "standard_forecast_graph"
    # Routing features derived from weather payload
    routing_features: dict = field(default_factory=dict)
    # Whether a prior checkpoint was explicitly approved by a human
    checkpoint_approved: bool = False
    # Task ledger for transparency
    task_ledger: list = field(default_factory=list)
    # Checkpointing for high-risk decisions
    checkpoint: dict = field(default_factory=dict)
    # Weather data
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

    # ─── TASK LEDGER METHODS ───────────────────────────────────────────────────
    
    def add_task(self, task_name: str, status: str = "pending", result: dict = None):
        """Add a task to the ledger for transparency"""
        self.task_ledger.append({
            'task': task_name,
            'status': status,  # 'pending', 'in_progress', 'completed', 'failed'
            'result': result,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    
    def update_task(self, task_name: str, status: str, result: dict = None):
        """Update an existing task in the ledger"""
        for task in self.task_ledger:
            if task['task'] == task_name:
                task['status'] = status
                if result:
                    task['result'] = result
                task['updated_at'] = datetime.now(timezone.utc).isoformat()
                break
    
    def get_pending_tasks(self):
        """Get all pending tasks"""
        return [t for t in self.task_ledger if t.get('status') == 'pending']
    
    def get_completed_tasks(self):
        """Get all completed tasks"""
        return [t for t in self.task_ledger if t.get('status') == 'completed']

    # ─── CHECKPOINTING METHODS ─────────────────────────────────────────────────
    
    def create_checkpoint(self, paused_at: str, requires_approval: bool, pending_action: str, 
                         approval_role: str = "admin", auto_expire_minutes: int = 30):
        """Create a checkpoint for human-in-the-loop approval"""
        self.checkpoint = {
            'paused_at': paused_at,
            'requires_approval': requires_approval,
            'pending_action': pending_action,
            'approval_role': approval_role,
            'auto_expire_minutes': auto_expire_minutes,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'approved': False,
            'approved_by': None,
            'approved_at': None
        }
    
    def approve_checkpoint(self, approved_by: str):
        """Approve a checkpointed workflow"""
        if self.checkpoint:
            self.checkpoint['approved'] = True
            self.checkpoint['approved_by'] = approved_by
            self.checkpoint['approved_at'] = datetime.now(timezone.utc).isoformat()
    
    def is_checkpointed(self) -> bool:
        """Check if message has a pending checkpoint"""
        return bool(self.checkpoint and not self.checkpoint.get('approved', False))

    def to_state(self) -> dict:
        """Serialize the full agent envelope for checkpoint resume."""
        return asdict(self)

    @classmethod
    def from_state(cls, state: dict) -> "AgentMessage":
        """Restore agent envelope from persisted checkpoint state."""
        allowed = {f.name for f in dataclass_fields(cls)}
        payload = {k: v for k, v in (state or {}).items() if k in allowed}
        return cls(**payload)


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


# ─── INTENT CLASSIFIER AGENT ──────────────────────────────────────────────────
# Implements Handoff Pattern - routes queries to specialist agents

class IntentClassifierAgent(BaseAgent):
    """
    Routes incoming queries to specialist agents based on intent.
    This is the entry point that determines which sub-graph to use.
    """
    agent_type = "intent_classifier"
    
    # Intent patterns for classification
    INTENT_PATTERNS = {
        'flood_specialist': {
            'keywords': ['flood', 'flooding', 'flash flood', 'river', 'inundation', 'overflow', 
                        'heavy rain', 'storm water', 'drainage', 'sewer'],
            'risk_type': 'flood'
        },
        'drought_specialist': {
            'keywords': ['drought', 'dry', 'water shortage', 'water scarcity', 'arid', 
                        'irrigation', 'crop water', 'reservoir', 'groundwater'],
            'risk_type': 'drought'
        },
        'heatwave_specialist': {
            'keywords': ['heat', 'heatwave', 'hot', 'temperature', 'heat stress', 'sunstroke',
                        'high temp', 'hot weather', 'warming', 'heat advisory'],
            'risk_type': 'heatwave'
        },
        'agriculture_specialist': {
            'keywords': ['crop', 'plant', 'harvest', 'farming', 'agriculture', 'livestock',
                        'pesticide', 'spray', 'planting', 'soil', 'yield'],
            'risk_type': 'agriculture'
        },
        'emergency_specialist': {
            'keywords': ['evacuation', 'emergency', 'warning', 'alert', 'disaster', 'crisis',
                        'urgent', 'immediate', 'critical', 'life-threatening'],
            'risk_type': 'emergency'
        },
        'general_forecast': {
            'keywords': ['forecast', 'weather', 'rain', 'sunny', 'cloudy', 'humid', 'wind',
                        'what is the weather', 'temperature', 'should i bring umbrella'],
            'risk_type': 'general'
        }
    }

    MODEL_CONFIDENCE_THRESHOLD = 0.65

    def _keyword_classify(self, query: str) -> tuple[str, float, dict]:
        """Deterministic fallback classifier."""
        intent_scores = {}
        for intent, config in self.INTENT_PATTERNS.items():
            score = 0
            for keyword in config['keywords']:
                if keyword in query:
                    score += 1
            if score > 0:
                intent_scores[intent] = score

        if not intent_scores:
            return 'general_forecast', 0.40, {}

        selected_intent = max(intent_scores, key=intent_scores.get)
        max_score = max(intent_scores.values())
        confidence = min(0.95, 0.45 + 0.1 * max_score)
        return selected_intent, confidence, intent_scores

    def _model_classify(self, query: str) -> tuple[str, float, dict]:
        """
        LLM-based intent classification with confidence.
        Returns (intent, confidence, raw_output). Safe fallback is handled by caller.
        """
        allowed_intents = list(self.INTENT_PATTERNS.keys())
        system = f"""You classify user weather-risk queries into one intent label.
{_JSON_ONLY}
Allowed intents: {allowed_intents}
Return keys:
  intent (string, one of allowed intents),
  confidence (number 0.0 to 1.0),
  signals (list of strings)."""
        user = f"User query: {query}"

        result = self._complete(system, user, temperature=0.0)
        parsed = self._parse_json(result.get("text", ""))
        intent = str(parsed.get("intent", "")).strip().lower()
        if intent not in self.INTENT_PATTERNS:
            intent = "general_forecast"
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        return intent, confidence, parsed
    
    def run(self, msg: AgentMessage) -> AgentMessage:
        """Classify the user query and route to appropriate specialist"""
        start = time.time()
        
        # Add task to ledger
        msg.add_task("Intent Classification", "in_progress")
        
        query = msg.user_query.lower()

        keyword_intent, keyword_confidence, keyword_scores = self._keyword_classify(query)
        selected_intent = keyword_intent
        selected_confidence = keyword_confidence
        selected_source = "keyword_fallback"
        model_payload = {}

        try:
            model_intent, model_confidence, model_payload = self._model_classify(msg.user_query)
            if model_confidence >= self.MODEL_CONFIDENCE_THRESHOLD:
                selected_intent = model_intent
                selected_confidence = model_confidence
                selected_source = "model"
        except Exception as e:
            logger.warning(f"[IntentClassifier] Model classification failed, fallback to keyword: {e}")

        msg.intent_classification = selected_intent
        msg.intent_confidence = round(float(selected_confidence), 3)
        msg.intent_source = selected_source

        risk_type = self.INTENT_PATTERNS[selected_intent].get('risk_type', 'general')
        msg.update_task("Intent Classification", "completed", {
            'selected_intent': selected_intent,
            'confidence': msg.intent_confidence,
            'source': selected_source,
            'risk_type': risk_type,
            'keyword_scores': keyword_scores,
            'model_payload': model_payload,
        })
        
        latency_ms = int((time.time() - start) * 1000)
        msg.log_step("intent_classifier", "completed", latency_ms, "intent_classifier", selected_source)
        
        logger.info(
            "[IntentClassifier] Query classified as: %s (source=%s confidence=%.3f)",
            selected_intent,
            selected_source,
            msg.intent_confidence,
        )
        
        return msg


# ─── TYPE-BASED ROUTER ─────────────────────────────────────────────────────────
# Routes to different analysis sub-graphs based on weather conditions

class TypeBasedRouter:
    """
    Routes analysis to different sub-graphs based on weather conditions.
    Implements Graph-based workflow type-routing.
    """

    ROUTING_RULES = {
        'severe_weather': {
            'conditions': [
                # Sustained or extreme rainfall totals.
                lambda f: f.get('total_rain_24h') is not None and f.get('total_rain_24h') >= 50,
                lambda f: f.get('forecast_rain_today') is not None and f.get('forecast_rain_today') >= 40,
                lambda f: f.get('forecast_rain_tomorrow') is not None and f.get('forecast_rain_tomorrow') >= 60,
                # Severe convective risk requires BOTH high probability and meaningful rain volume.
                lambda f: (
                    f.get('precip_probability') is not None and f.get('precip_probability') >= 92 and
                    (
                        (f.get('forecast_rain_today') is not None and f.get('forecast_rain_today') >= 15) or
                        (f.get('forecast_rain_tomorrow') is not None and f.get('forecast_rain_tomorrow') >= 25)
                    )
                ),
                lambda f: f.get('wind_speed') is not None and f.get('wind_speed') >= 90,
            ],
            'sub_graph': 'severe_weather_graph',
            'min_conditions': 1,
        },
        'heatwave': {
            'conditions': [
                lambda f: f.get('temperature') is not None and f.get('temperature') >= 34,
                lambda f: f.get('heat_index') is not None and f.get('heat_index') >= 36,
            ],
            'sub_graph': 'heatwave_graph',
            'min_conditions': 1,
        },
        'flood': {
            'conditions': [
                lambda f: f.get('total_rain_24h') is not None and f.get('total_rain_24h') >= 20,
                lambda f: f.get('forecast_rain_today') is not None and f.get('forecast_rain_today') >= 10,
                lambda f: f.get('forecast_rain_tomorrow') is not None and f.get('forecast_rain_tomorrow') >= 20,
                lambda f: (
                    f.get('precip_probability') is not None and f.get('precip_probability') >= 85 and
                    (
                        (f.get('forecast_rain_today') is not None and f.get('forecast_rain_today') >= 5) or
                        (f.get('forecast_rain_tomorrow') is not None and f.get('forecast_rain_tomorrow') >= 8)
                    )
                ),
            ],
            'sub_graph': 'flood_graph',
            'min_conditions': 1,
        },
        'drought': {
            'conditions': [
                lambda f: f.get('rain_30d') is not None and f.get('rain_30d') < 5,
                lambda f: f.get('soil_moisture') is not None and f.get('soil_moisture') < 30,
            ],
            'sub_graph': 'drought_graph',
            'min_conditions': 1,
        },
    }

    INTENT_GRAPH_FALLBACK = {
        'flood_specialist': 'flood_graph',
        'drought_specialist': 'drought_graph',
        'heatwave_specialist': 'heatwave_graph',
    }

    @staticmethod
    def _to_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def extract_features(cls, weather_data: dict) -> dict:
        """Extract routing features from canonical weather payload + middleware metadata."""
        middleware = weather_data.get('_middleware', {}) if isinstance(weather_data, dict) else {}
        routing = middleware.get('routing_features', {}) if isinstance(middleware, dict) else {}
        metrics = middleware.get('metrics', {}) if isinstance(middleware, dict) else {}

        today_forecast = weather_data.get('today_forecast', {}) if isinstance(weather_data, dict) else {}
        tomorrow_forecast = weather_data.get('tomorrow_forecast', {}) if isinstance(weather_data, dict) else {}

        features = {
            'temperature': cls._to_float(weather_data.get('temperature')),
            'heat_index': cls._to_float(weather_data.get('heat_index')),
            'precip_probability': cls._to_float(weather_data.get('precip_probability')),
            'total_rain_24h': cls._to_float(weather_data.get('total_rain_24h')),
            'forecast_rain_today': cls._to_float(weather_data.get('today_forecast', {}).get('daily_total_mm')) if isinstance(weather_data.get('today_forecast', {}), dict) else None,
            'forecast_rain_tomorrow': cls._to_float(weather_data.get('tomorrow_forecast', {}).get('daily_total_mm')) if isinstance(weather_data.get('tomorrow_forecast', {}), dict) else None,
            'wind_speed': cls._to_float(weather_data.get('wind_speed')),
            'rain_30d': cls._to_float(weather_data.get('rain_30d')),
            'soil_moisture': cls._to_float(weather_data.get('soil_moisture')),
        }

        # Fill from middleware-derived features
        for key, value in routing.items():
            if key in features and features[key] is None:
                features[key] = cls._to_float(value)

        # Fill from middleware metrics/summary fallback
        if features['heat_index'] is None:
            features['heat_index'] = cls._to_float(metrics.get('heat_index'))
        if features['total_rain_24h'] is None:
            features['total_rain_24h'] = cls._to_float(metrics.get('total_precipitation_24h'))
        if features['precip_probability'] is None and isinstance(today_forecast, dict):
            features['precip_probability'] = cls._to_float(today_forecast.get('precip_prob'))
        if features['forecast_rain_today'] is None and isinstance(today_forecast, dict):
            features['forecast_rain_today'] = cls._to_float(today_forecast.get('daily_total_mm'))
        if features['forecast_rain_tomorrow'] is None and isinstance(tomorrow_forecast, dict):
            features['forecast_rain_tomorrow'] = cls._to_float(tomorrow_forecast.get('daily_total_mm'))

        return features

    @classmethod
    def route(cls, weather_data: dict, intent: str = "general_forecast") -> tuple[str, dict]:
        """Determine sub-graph using weather features, with intent fallback."""
        features = cls.extract_features(weather_data or {})

        for config in cls.ROUTING_RULES.values():
            conditions_met = sum(
                1 for condition in config['conditions'] if condition(features)
            )
            if conditions_met >= config.get('min_conditions', 1):
                return config['sub_graph'], features

        # If weather evidence is inconclusive, use intent as fallback handoff target.
        fallback_graph = cls.INTENT_GRAPH_FALLBACK.get(intent, 'standard_forecast_graph')
        return fallback_graph, features


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
Intent classification: {msg.intent_classification}
Selected graph: {msg.selected_graph}
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
Intent classification: {msg.intent_classification}
Selected graph: {msg.selected_graph}
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
# Implements Checkpointing for Human-in-the-Loop (HITL)

class DecisionAgent(BaseAgent):
    agent_type = "decision"
    
    # Risk thresholds for checkpointing
    CRITICAL_THRESHOLD = 80  # Requires human approval
    HIGH_THRESHOLD = 60     # Auto-proceed but log
    
    def run(self, msg: AgentMessage) -> AgentMessage:
        start = time.time()
        
        # Add task to ledger
        msg.add_task("Decision Analysis", "in_progress")

        from ..services.policy_engine import evaluate_risk_policy

        risk = msg.risk_assessment
        policy = evaluate_risk_policy(
            risk_assessment=risk,
            weather_data=msg.weather_data,
            intent_classification=msg.intent_classification,
        )
        flood_risk = policy.get("scores", {}).get("flood_risk", 0)

        # Checkpoint logic from deterministic policy
        if policy.get("requires_checkpoint", False) and not msg.checkpoint_approved:
            msg.create_checkpoint(
                paused_at='decision_agent',
                requires_approval=True,
                pending_action=policy.get("pending_action", "issue_critical_alert"),
                approval_role=policy.get("required_role", "admin"),
                auto_expire_minutes=policy.get("auto_expire_minutes", 30),
            )

            msg.decision = {
                "alert_level": policy.get("alert_level", "RED"),
                "priority": policy.get("priority", "critical"),
                "immediate_action_required": policy.get("immediate_action_required", True),
                "response_timeline_hours": policy.get("response_timeline_hours", 1),
                "recommended_actions": policy.get("recommended_actions", []),
                "policy_name": policy.get("policy_name"),
                "policy_version": policy.get("policy_version"),
                "policy_rule_id": policy.get("rule_id"),
                "triggered_rules": policy.get("triggered_rules", []),
                "why_alert_level": policy.get("why_alert_level", ""),
                "why_selected_graph": (
                    f"Graph '{msg.selected_graph}' selected using routing features "
                    f"{msg.routing_features} and intent '{msg.intent_classification}'."
                ),
                "text": (
                    f"Checkpoint pending approval. {policy.get('why_alert_level', '')}"
                ).strip(),
            }

            logger.warning(
                "[Decision] Policy checkpoint created: session=%s risk=%s rule=%s role=%s",
                msg.session_id,
                policy.get("max_risk"),
                policy.get("rule_id"),
                policy.get("required_role", "admin"),
            )

            msg.update_task("Decision Analysis", "completed", {
                "risk_level": policy.get("max_risk"),
                "requires_approval": True,
                "checkpoint_id": msg.checkpoint.get("created_at"),
                "policy_rule": policy.get("rule_id"),
            })
            latency_ms = int((time.time() - start) * 1000)
            msg.log_step("decision", "checkpointed", latency_ms, "policy_engine", "deterministic")
            return msg

        # High risk: pre-scale infra for deterministic policy high/critical levels.
        if policy.get("max_risk", 0) >= self.HIGH_THRESHOLD:
            scale_result = mcp.execute("scale_aks_nodepool", {
                "resource_group": os.getenv("AZURE_RESOURCE_GROUP", "resilienteco-rg"),
                "cluster_name": os.getenv("AKS_CLUSTER_NAME", "resilienteco-aks"),
                "nodepool_name": "agentpool",
                "node_count": 5,
                "reason": (
                    f"High policy risk ({policy.get('max_risk', 0)}%) "
                    f"rule={policy.get('rule_id')} location={msg.location}"
                ),
            })
            msg.mcp_actions.append({
                "agent": "decision",
                "mcp_call": "scale_aks_nodepool",
                "trigger": f"policy_max_risk={policy.get('max_risk', 0)}%",
                "result": scale_result,
            })

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
Intent classification: {msg.intent_classification}
Selected graph: {msg.selected_graph}
Risk assessment from Predict Agent: {risk}
Deterministic policy decision (must be respected): {policy}
Monitor analysis: {msg.weather_data.get('monitor_analysis', {})}
Azure infrastructure actions taken: {msg.mcp_actions}

Return JSON decision only."""

        result = self._complete(system, user, temperature=0.4)
        latency_ms = int((time.time() - start) * 1000)

        text = result["text"]
        parsed = self._parse_json(text)
        parsed["text"] = text

        # Deterministic policy is source of truth for high-stakes decision fields.
        parsed["alert_level"] = policy.get("alert_level", parsed.get("alert_level", "GREEN"))
        parsed["priority"] = policy.get("priority", parsed.get("priority", "low"))
        parsed["immediate_action_required"] = policy.get(
            "immediate_action_required",
            parsed.get("immediate_action_required", False),
        )
        parsed["response_timeline_hours"] = policy.get(
            "response_timeline_hours",
            parsed.get("response_timeline_hours", 24),
        )
        if not parsed.get("recommended_actions"):
            parsed["recommended_actions"] = policy.get("recommended_actions", [])

        parsed["policy_name"] = policy.get("policy_name")
        parsed["policy_version"] = policy.get("policy_version")
        parsed["policy_source"] = policy.get("policy_source")
        parsed["policy_rule_id"] = policy.get("rule_id")
        parsed["triggered_rules"] = policy.get("triggered_rules", [])
        parsed["why_alert_level"] = policy.get("why_alert_level", "")
        parsed["why_selected_graph"] = (
            f"Graph '{msg.selected_graph}' selected using routing features "
            f"{msg.routing_features} and intent '{msg.intent_classification}'."
        )
        parsed["explainability"] = {
            "policy": {
                "name": policy.get("policy_name"),
                "version": policy.get("policy_version"),
                "rule_id": policy.get("rule_id"),
                "triggered_rules": policy.get("triggered_rules", []),
            },
            "why_alert_level": policy.get("why_alert_level", ""),
            "why_selected_graph": parsed["why_selected_graph"],
            "evidence_flags": policy.get("evidence_flags", []),
        }

        msg.decision = parsed
        msg.update_task("Decision Analysis", "completed", {
            'risk_level': policy.get("max_risk", flood_risk),
            'alert_level': parsed.get('alert_level', 'UNKNOWN'),
            'policy_rule': policy.get("rule_id"),
        })
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

def _build_agent_pipeline(
    selected_graph: str,
    *,
    return_meta: bool = False,
) -> list[tuple[str, BaseAgent]] | tuple[list[tuple[str, BaseAgent]], dict]:
    """
    Build execution pipeline from externalized graph config.
    """
    from ..services.workflow_config import resolve_pipeline_steps

    step_map: dict[str, type[BaseAgent]] = {
        "monitor": MonitorAgent,
        "predict": PredictAgent,
        "decision": DecisionAgent,
        "action": ActionAgent,
        "governance": GovernanceAgent,
    }

    pipeline_steps, pipeline_meta = resolve_pipeline_steps(selected_graph)
    agents = [(step, step_map[step]()) for step in pipeline_steps if step in step_map]

    if return_meta:
        return agents, pipeline_meta
    return agents


def run_all_agents(
    user_query: str,
    lat: float,
    lon: float,
    city_name: str,
    *,
    session_id: Optional[str] = None,
    checkpoint_approved: bool = False,
    resume_from_step: Optional[str] = None,
    resume_state: Optional[dict] = None,
    resume_results: Optional[dict] = None,
) -> dict:
    import uuid
    from ..services.weather_service import get_weather_summary
    from ..services.weather_middleware import transform_weather_data
    
    session_id = session_id or str(uuid.uuid4())[:8]

    if resume_state:
        msg = AgentMessage.from_state(resume_state)
        msg.session_id = session_id or msg.session_id
        msg.checkpoint_approved = checkpoint_approved or msg.checkpoint_approved
        if checkpoint_approved and msg.checkpoint:
            msg.checkpoint['approved'] = True
            msg.checkpoint['approved_at'] = datetime.now(timezone.utc).isoformat()

        selected_graph = msg.selected_graph or 'standard_forecast_graph'
        routing_features = msg.routing_features or TypeBasedRouter.extract_features(msg.weather_data)
        transformed_weather = {
            "summary": msg.weather_data.get('_middleware', {}).get('summary', {}),
            "metrics": msg.weather_data.get('_middleware', {}).get('metrics', {}),
            "alerts": msg.weather_data.get('_middleware', {}).get('alerts', []),
            "narrative": msg.weather_data.get('_middleware', {}).get('narrative', ''),
        }
        results = dict(resume_results or {})
        start_step = resume_from_step
    else:
        try:
            weather = get_weather_summary(lat, lon, city_name)
        except Exception as e:
            logger.error(f"Weather fetch failed: {e}")
            weather = {}

        # Apply weather middleware transformation
        try:
            transformed_weather = transform_weather_data(weather, city_name)
        except Exception as e:
            logger.warning(f"Weather middleware transform failed: {e}")
            transformed_weather = {'enhanced_data': weather, 'routing_features': {}}

        msg = AgentMessage(
            session_id=session_id,
            location=city_name,
            lat=lat,
            lon=lon,
            user_query=user_query,
            weather_data=transformed_weather.get('enhanced_data', weather),
            checkpoint_approved=checkpoint_approved,
        )
        
        # Add middleware metadata to message
        msg.weather_data['_middleware'] = {
            'summary': transformed_weather.get('summary', {}),
            'metrics': transformed_weather.get('metrics', {}),
            'alerts': transformed_weather.get('alerts', []),
            'narrative': transformed_weather.get('narrative', ''),
            'routing_features': transformed_weather.get('routing_features', {}),
        }

        # STEP 1: Intent Classification
        intent_agent = IntentClassifierAgent()
        try:
            msg = intent_agent.run(msg)
            logger.info(f"[Orchestrator] Intent classified: {msg.intent_classification}")
        except Exception as e:
            logger.warning(f"Intent classification failed: {e}")

        # STEP 2: Type-based routing (check if we need special handling)
        router = TypeBasedRouter()
        selected_graph, routing_features = router.route(msg.weather_data, msg.intent_classification)
        msg.selected_graph = selected_graph
        msg.routing_features = routing_features
        logger.info(f"[Orchestrator] Selected sub-graph: {selected_graph}")
        results = {}
        start_step = None

    agents, pipeline_meta = _build_agent_pipeline(selected_graph, return_meta=True)
    logger.info(f"[Orchestrator] Agent pipeline: {[name for name, _ in agents]}")

    start_idx = 0
    if start_step:
        for idx, (name, _) in enumerate(agents):
            if name == start_step:
                start_idx = idx
                break

    paused_at_step = None
    next_step = None

    for idx in range(start_idx, len(agents)):
        name, agent = agents[idx]
        try:
            # Add task to ledger
            msg.add_task(f"{name.title()} Agent Execution", "in_progress")
            
            msg = agent.run(msg)
            
            # Update task as completed
            msg.update_task(f"{name.title()} Agent Execution", "completed")
            
            if name == "monitor":
                results[name] = msg.weather_data.get("monitor_text", "Monitor complete.")
                results["monitor_data"] = msg.weather_data.get("monitor_analysis", {})
            elif name == "predict":
                results[name] = msg.risk_assessment.get("text", "Predict complete.")
                results["predict_data"] = msg.risk_assessment
            elif name == "decision":
                results[name] = msg.decision.get("text", "Decision complete.")
                results["decision_data"] = msg.decision
            elif name == "action":
                results[name] = msg.action_plan.get("text", "Action complete.")
                results["action_data"] = msg.action_plan
            elif name == "governance":
                results[name] = msg.governance_review.get("text", "Governance complete.")
                results["governance_data"] = msg.governance_review
            
            # Check for checkpoint - if checkpoint exists and needs approval, stop pipeline
            if msg.is_checkpointed():
                paused_at_step = name
                if idx + 1 < len(agents):
                    next_step = agents[idx + 1][0]
                logger.warning(f"[Orchestrator] Pipeline paused at {name} - checkpoint requires approval")
                break
                
        except Exception as e:
            logger.exception(f"Agent {name} failed")
            msg.update_task(f"{name.title()} Agent Execution", "failed", {'error': str(e)})
            results[name] = f"Warning: {name} agent error: {str(e)}"

    # Keep response shape stable for clients expecting all keys
    for expected in ("monitor", "predict", "decision", "action", "governance"):
        if expected not in results:
            results[expected] = f"{expected.title()} skipped by {selected_graph}."
    for expected_data in ("monitor_data", "predict_data", "decision_data", "action_data", "governance_data"):
        if expected_data not in results:
            results[expected_data] = {}

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
    
    # Include new metadata
    results["task_ledger"] = msg.task_ledger
    results["intent_classification"] = msg.intent_classification
    results["intent_confidence"] = msg.intent_confidence
    results["intent_source"] = msg.intent_source
    results["selected_graph"] = selected_graph
    results["routing_features"] = routing_features
    results["pipeline"] = [name for name, _ in agents]
    results["pipeline_config"] = pipeline_meta
    results["explainability"] = {
        "why_selected_graph": (
            f"Graph '{selected_graph}' selected using routing features "
            f"{routing_features} and intent '{msg.intent_classification}'."
        ),
        "why_alert_level": msg.decision.get("why_alert_level", ""),
        "policy_rule_id": msg.decision.get("policy_rule_id"),
        "triggered_rules": msg.decision.get("triggered_rules", []),
    }
    
    # Checkpoint status
    if msg.checkpoint:
        results["checkpoint_status"] = {
            "requires_approval": msg.checkpoint.get('requires_approval', False),
            "pending_action": msg.checkpoint.get('pending_action'),
            "approved": msg.checkpoint.get('approved', False),
            "approval_role": msg.checkpoint.get('approval_role', 'admin'),
            "auto_expire_minutes": msg.checkpoint.get('auto_expire_minutes', 30),
            "created_at": msg.checkpoint.get('created_at'),
            "paused_at_step": paused_at_step or msg.checkpoint.get("paused_at"),
            "resume_from_step": next_step,
        }
    
    # Middleware output
    results["weather_summary"] = transformed_weather.get('summary', {})
    results["weather_metrics"] = transformed_weather.get('metrics', {})
    results["weather_alerts"] = transformed_weather.get('alerts', [])
    results["weather_narrative"] = transformed_weather.get('narrative', '')
    results["workflow_state"] = msg.to_state()

    return results

