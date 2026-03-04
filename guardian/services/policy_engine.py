"""
Deterministic risk policy engine with versioned policy support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from django.utils import timezone

from ..models import RiskPolicyVersion


DEFAULT_POLICY_NAME = "global_default"
DEFAULT_POLICY_VERSION = "2026.03.1"

DEFAULT_POLICY_RULES = {
    "rules": [
        {
            "id": "critical_any_risk",
            "risk_type": "any",
            "threshold": 85,
            "alert_level": "RED",
            "priority": "critical",
            "immediate_action_required": True,
            "response_timeline_hours": 1,
            "requires_checkpoint": True,
            "required_role": "admin",
            "auto_expire_minutes": 30,
            "recommended_actions": [
                "Issue immediate emergency alert",
                "Notify county disaster response teams",
                "Activate evacuation readiness plan",
            ],
        },
        {
            "id": "high_any_risk",
            "risk_type": "any",
            "threshold": 70,
            "alert_level": "ORANGE",
            "priority": "high",
            "immediate_action_required": True,
            "response_timeline_hours": 3,
            "requires_checkpoint": False,
            "required_role": "operator",
            "auto_expire_minutes": 30,
            "recommended_actions": [
                "Issue preparedness advisory",
                "Pre-position response resources",
                "Increase monitoring cadence",
            ],
        },
        {
            "id": "medium_any_risk",
            "risk_type": "any",
            "threshold": 50,
            "alert_level": "YELLOW",
            "priority": "medium",
            "immediate_action_required": False,
            "response_timeline_hours": 8,
            "requires_checkpoint": False,
            "required_role": "operator",
            "auto_expire_minutes": 60,
            "recommended_actions": [
                "Issue caution bulletin",
                "Advise local responders to monitor",
            ],
        },
        {
            "id": "low_risk_default",
            "risk_type": "any",
            "threshold": 0,
            "alert_level": "GREEN",
            "priority": "low",
            "immediate_action_required": False,
            "response_timeline_hours": 24,
            "requires_checkpoint": False,
            "required_role": "operator",
            "auto_expire_minutes": 120,
            "recommended_actions": [
                "Continue routine monitoring",
            ],
        },
    ]
}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_active_policy() -> Dict[str, Any]:
    """
    Resolve active policy from DB, fallback to in-code default.
    """
    policy = (
        RiskPolicyVersion.objects.filter(is_active=True)
        .order_by("-activated_at", "-created_at")
        .first()
    )
    if policy:
        return {
            "name": policy.name,
            "version": policy.version,
            "rules": (policy.rules or {}).get("rules", []),
            "source": "database",
            "activated_at": policy.activated_at.isoformat() if policy.activated_at else None,
        }

    return {
        "name": DEFAULT_POLICY_NAME,
        "version": DEFAULT_POLICY_VERSION,
        "rules": DEFAULT_POLICY_RULES["rules"],
        "source": "default",
        "activated_at": timezone.now().isoformat(),
    }


def evaluate_risk_policy(
    risk_assessment: Optional[Dict[str, Any]],
    weather_data: Optional[Dict[str, Any]],
    intent_classification: str = "general_forecast",
) -> Dict[str, Any]:
    """
    Deterministic policy decision from model outputs + weather features.
    """
    risk_assessment = risk_assessment or {}
    weather_data = weather_data or {}
    policy = get_active_policy()

    flood_risk = _to_int(risk_assessment.get("flood_risk"), 0)
    drought_risk = _to_int(risk_assessment.get("drought_risk"), 0)
    heatwave_risk = _to_int(risk_assessment.get("heatwave_risk"), 0)

    scores = {
        "flood_risk": flood_risk,
        "drought_risk": drought_risk,
        "heatwave_risk": heatwave_risk,
    }

    primary_risk, max_risk = max(scores.items(), key=lambda x: x[1])
    primary_risk = primary_risk.replace("_risk", "")

    middleware = weather_data.get("_middleware", {}) if isinstance(weather_data, dict) else {}
    routing_features = middleware.get("routing_features", {}) if isinstance(middleware, dict) else {}
    precip_probability = _to_float(routing_features.get("precip_probability"), 0.0)
    total_rain_24h = _to_float(routing_features.get("total_rain_24h"), 0.0)

    monitor_analysis = weather_data.get("monitor_analysis", {}) if isinstance(weather_data, dict) else {}
    data_quality_score = _to_int(monitor_analysis.get("data_quality_score"), 75)

    triggered_rules: List[Dict[str, Any]] = []
    selected_rule: Optional[Dict[str, Any]] = None

    for rule in policy.get("rules", []):
        risk_type = rule.get("risk_type", "any")
        threshold = _to_int(rule.get("threshold"), 0)

        if risk_type == "any":
            candidate_value = max_risk
        else:
            candidate_value = _to_int(scores.get(f"{risk_type}_risk"), 0)

        if candidate_value >= threshold:
            selected_rule = rule
            triggered_rules.append(
                {
                    "rule_id": rule.get("id"),
                    "risk_type": risk_type,
                    "threshold": threshold,
                    "observed_value": candidate_value,
                }
            )
            break

    if selected_rule is None:
        # Safety fallback.
        selected_rule = {
            "id": "fallback_green",
            "alert_level": "GREEN",
            "priority": "low",
            "immediate_action_required": False,
            "response_timeline_hours": 24,
            "requires_checkpoint": False,
            "required_role": "operator",
            "auto_expire_minutes": 120,
            "recommended_actions": ["Continue routine monitoring"],
        }
        triggered_rules.append(
            {
                "rule_id": "fallback_green",
                "risk_type": "any",
                "threshold": 0,
                "observed_value": max_risk,
            }
        )

    # Secondary escalation evidence from weather intensity.
    evidence_flags = []
    if precip_probability >= 80:
        evidence_flags.append("high_precip_probability")
    if total_rain_24h >= 50:
        evidence_flags.append("extreme_24h_rainfall")
    if data_quality_score < 50:
        evidence_flags.append("low_data_quality")

    why_alert_level = (
        f"Policy {policy['name']}@{policy['version']} selected "
        f"{selected_rule.get('alert_level')} from rule {selected_rule.get('id')} "
        f"(max_risk={max_risk}, primary_risk={primary_risk}, "
        f"precip_probability={precip_probability:.1f}%, rain_24h={total_rain_24h:.1f}mm)."
    )

    return {
        "policy_name": policy["name"],
        "policy_version": policy["version"],
        "policy_source": policy.get("source", "default"),
        "rule_id": selected_rule.get("id"),
        "alert_level": selected_rule.get("alert_level", "GREEN"),
        "priority": selected_rule.get("priority", "low"),
        "immediate_action_required": bool(selected_rule.get("immediate_action_required", False)),
        "response_timeline_hours": _to_int(selected_rule.get("response_timeline_hours"), 24),
        "requires_checkpoint": bool(selected_rule.get("requires_checkpoint", False)),
        "required_role": selected_rule.get("required_role", "admin"),
        "auto_expire_minutes": _to_int(selected_rule.get("auto_expire_minutes"), 30),
        "recommended_actions": selected_rule.get("recommended_actions", []),
        "pending_action": "issue_critical_alert"
        if selected_rule.get("alert_level") == "RED"
        else "issue_risk_advisory",
        "scores": scores,
        "max_risk": max_risk,
        "primary_risk": primary_risk,
        "intent_classification": intent_classification,
        "triggered_rules": triggered_rules,
        "evidence_flags": evidence_flags,
        "data_quality_score": data_quality_score,
        "why_alert_level": why_alert_level,
    }
