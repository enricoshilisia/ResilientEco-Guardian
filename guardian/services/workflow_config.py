"""
Externalized workflow DAG configuration loader.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from django.utils import timezone

from ..models import WorkflowGraphConfig


ALLOWED_STEPS = {"monitor", "predict", "decision", "action", "governance"}

DEFAULT_WORKFLOW_CONFIG = {
    "graphs": {
        "standard_forecast_graph": {
            "pipeline": ["monitor", "predict", "decision", "action", "governance"],
        },
        "flood_graph": {
            "pipeline": ["monitor", "predict", "decision", "governance", "action"],
        },
        "severe_weather_graph": {
            "pipeline": ["monitor", "predict", "decision", "governance", "action"],
        },
        "heatwave_graph": {
            "pipeline": ["monitor", "predict", "decision", "action", "governance"],
        },
        "drought_graph": {
            "pipeline": ["monitor", "predict", "decision", "action", "governance"],
        },
    }
}


def _sanitize_pipeline(pipeline: List[str]) -> List[str]:
    clean = [step for step in (pipeline or []) if step in ALLOWED_STEPS]
    # Ensure core analysis chain exists in correct order.
    core = ["monitor", "predict", "decision"]
    for step in core:
        if step not in clean:
            clean.insert(core.index(step), step)
    # Append remaining if missing.
    for step in ("action", "governance"):
        if step not in clean:
            clean.append(step)
    return clean


def get_active_workflow_config() -> Dict:
    """
    Load active graph config from DB, fallback to default.
    """
    cfg = WorkflowGraphConfig.objects.filter(is_active=True).order_by("-activated_at", "-created_at").first()
    if not cfg:
        return {
            "name": "global_graph",
            "version": "default",
            "source": "default",
            "config": DEFAULT_WORKFLOW_CONFIG,
            "activated_at": timezone.now().isoformat(),
        }

    return {
        "name": cfg.name,
        "version": cfg.version,
        "source": "database",
        "config": cfg.config or DEFAULT_WORKFLOW_CONFIG,
        "activated_at": cfg.activated_at.isoformat() if cfg.activated_at else None,
    }


def resolve_pipeline_steps(selected_graph: str) -> Tuple[List[str], Dict]:
    """
    Resolve graph -> pipeline steps using externalized config.
    """
    active = get_active_workflow_config()
    config = active.get("config", {}) or {}
    graphs = config.get("graphs", {}) if isinstance(config, dict) else {}

    graph_conf = graphs.get(selected_graph) or graphs.get("standard_forecast_graph") or {}
    requested_pipeline = graph_conf.get("pipeline", [])
    pipeline = _sanitize_pipeline(requested_pipeline)

    return pipeline, {
        "config_name": active.get("name"),
        "config_version": active.get("version"),
        "config_source": active.get("source"),
        "selected_graph": selected_graph,
        "resolved_graph": selected_graph if selected_graph in graphs else "standard_forecast_graph",
    }
