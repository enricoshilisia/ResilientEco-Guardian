"""
Offline evaluation harness for routing + policy quality.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from django.utils import timezone

from ..agents.core_agents import TypeBasedRouter
from ..models import OfflineEvaluationRun
from .policy_engine import evaluate_risk_policy


DEFAULT_SCENARIOS: List[Dict] = [
    {
        "id": "severe_flood",
        "intent": "flood_specialist",
        "weather_data": {
            "temperature": 27,
            "total_rain_24h": 62,
            "today_forecast": {"precip_prob": 96, "daily_total_mm": 48},
            "tomorrow_forecast": {"precip_prob": 94, "daily_total_mm": 66},
            "_middleware": {
                "routing_features": {
                    "temperature": 27,
                    "heat_index": 31,
                    "total_rain_24h": 62,
                    "precip_probability": 96,
                    "forecast_rain_today": 48,
                    "forecast_rain_tomorrow": 66,
                }
            },
        },
        "risk_assessment": {"flood_risk": 92, "drought_risk": 5, "heatwave_risk": 8},
        "expected_graph": "severe_weather_graph",
        "expected_alert": "RED",
        "expected_checkpoint": True,
    },
    {
        "id": "heatwave_high",
        "intent": "heatwave_specialist",
        "weather_data": {
            "temperature": 35.5,
            "total_rain_24h": 0.3,
            "today_forecast": {"precip_prob": 18, "daily_total_mm": 0.6},
            "tomorrow_forecast": {"precip_prob": 15, "daily_total_mm": 0.2},
            "_middleware": {
                "routing_features": {
                    "temperature": 35.5,
                    "heat_index": 40.2,
                    "total_rain_24h": 0.3,
                    "precip_probability": 18,
                    "forecast_rain_today": 0.6,
                    "forecast_rain_tomorrow": 0.2,
                }
            },
        },
        "risk_assessment": {"flood_risk": 15, "drought_risk": 25, "heatwave_risk": 74},
        "expected_graph": "heatwave_graph",
        "expected_alert": "ORANGE",
        "expected_checkpoint": False,
    },
    {
        "id": "flood_moderate",
        "intent": "flood_specialist",
        "weather_data": {
            "temperature": 29,
            "total_rain_24h": 12,
            "today_forecast": {"precip_prob": 88, "daily_total_mm": 14},
            "tomorrow_forecast": {"precip_prob": 80, "daily_total_mm": 18},
            "_middleware": {
                "routing_features": {
                    "temperature": 29,
                    "heat_index": 33,
                    "total_rain_24h": 12,
                    "precip_probability": 88,
                    "forecast_rain_today": 14,
                    "forecast_rain_tomorrow": 18,
                }
            },
        },
        "risk_assessment": {"flood_risk": 58, "drought_risk": 10, "heatwave_risk": 20},
        "expected_graph": "flood_graph",
        "expected_alert": "YELLOW",
        "expected_checkpoint": False,
    },
    {
        "id": "drought_risk",
        "intent": "drought_specialist",
        "weather_data": {
            "temperature": 31,
            "total_rain_24h": 0.0,
            "today_forecast": {"precip_prob": 5, "daily_total_mm": 0.0},
            "tomorrow_forecast": {"precip_prob": 8, "daily_total_mm": 0.0},
            "_middleware": {
                "routing_features": {
                    "temperature": 31,
                    "heat_index": 33,
                    "total_rain_24h": 0.0,
                    "precip_probability": 5,
                    "forecast_rain_today": 0.0,
                    "forecast_rain_tomorrow": 0.0,
                    "rain_30d": 2,
                    "soil_moisture": 18,
                }
            },
        },
        "risk_assessment": {"flood_risk": 8, "drought_risk": 62, "heatwave_risk": 28},
        "expected_graph": "drought_graph",
        "expected_alert": "YELLOW",
        "expected_checkpoint": False,
    },
    {
        "id": "benign_conditions",
        "intent": "general_forecast",
        "weather_data": {
            "temperature": 25,
            "total_rain_24h": 0.5,
            "today_forecast": {"precip_prob": 22, "daily_total_mm": 1.5},
            "tomorrow_forecast": {"precip_prob": 20, "daily_total_mm": 1.2},
            "_middleware": {
                "routing_features": {
                    "temperature": 25,
                    "heat_index": 26,
                    "total_rain_24h": 0.5,
                    "precip_probability": 22,
                    "forecast_rain_today": 1.5,
                    "forecast_rain_tomorrow": 1.2,
                }
            },
        },
        "risk_assessment": {"flood_risk": 18, "drought_risk": 12, "heatwave_risk": 10},
        "expected_graph": "standard_forecast_graph",
        "expected_alert": "GREEN",
        "expected_checkpoint": False,
    },
]


def run_offline_evaluation(
    *,
    scenario_pack: str = "default",
    triggered_by=None,
    scenarios: Optional[List[Dict]] = None,
) -> Dict:
    scenarios = scenarios or DEFAULT_SCENARIOS

    run = OfflineEvaluationRun.objects.create(
        scenario_pack=scenario_pack,
        status="pending",
        summary_metrics={},
        scenario_results=[],
        triggered_by=triggered_by if getattr(triggered_by, "is_authenticated", False) else None,
    )

    route_hits = 0
    alert_hits = 0
    checkpoint_hits = 0
    scenario_results: List[Dict] = []

    try:
        for scenario in scenarios:
            weather_data = scenario.get("weather_data", {})
            intent = scenario.get("intent", "general_forecast")
            expected_graph = scenario.get("expected_graph")
            expected_alert = scenario.get("expected_alert")
            expected_checkpoint = bool(scenario.get("expected_checkpoint", False))
            risk_assessment = scenario.get("risk_assessment", {})

            selected_graph, features = TypeBasedRouter.route(weather_data, intent)
            policy = evaluate_risk_policy(
                risk_assessment=risk_assessment,
                weather_data=weather_data,
                intent_classification=intent,
            )

            route_ok = selected_graph == expected_graph
            alert_ok = policy.get("alert_level") == expected_alert
            checkpoint_ok = bool(policy.get("requires_checkpoint", False)) == expected_checkpoint

            if route_ok:
                route_hits += 1
            if alert_ok:
                alert_hits += 1
            if checkpoint_ok:
                checkpoint_hits += 1

            scenario_results.append(
                {
                    "scenario_id": scenario.get("id"),
                    "expected_graph": expected_graph,
                    "selected_graph": selected_graph,
                    "route_ok": route_ok,
                    "expected_alert": expected_alert,
                    "selected_alert": policy.get("alert_level"),
                    "alert_ok": alert_ok,
                    "expected_checkpoint": expected_checkpoint,
                    "selected_checkpoint": bool(policy.get("requires_checkpoint", False)),
                    "checkpoint_ok": checkpoint_ok,
                    "features": features,
                    "policy_rule_id": policy.get("rule_id"),
                }
            )

        total = max(1, len(scenarios))
        summary = {
            "scenario_count": len(scenarios),
            "route_accuracy": round(route_hits / total, 4),
            "alert_accuracy": round(alert_hits / total, 4),
            "checkpoint_accuracy": round(checkpoint_hits / total, 4),
        }

        run.status = "completed"
        run.summary_metrics = summary
        run.scenario_results = scenario_results
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "summary_metrics", "scenario_results", "completed_at"])

        return {
            "run_id": run.id,
            "status": run.status,
            "summary_metrics": summary,
            "scenario_results": scenario_results,
        }
    except Exception as exc:
        run.status = "failed"
        run.notes = str(exc)
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "notes", "completed_at"])
        raise
