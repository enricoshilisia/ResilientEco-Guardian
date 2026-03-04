"""
SLO metrics aggregator for agent reliability/quality.
"""

from __future__ import annotations

from datetime import timedelta
from statistics import mean

from django.utils import timezone

from ..models import AgentExecutionLog, OfflineEvaluationRun, WorkflowCheckpoint


def compute_slo_metrics(window_hours: int = 24) -> dict:
    since = timezone.now() - timedelta(hours=window_hours)
    logs = AgentExecutionLog.objects.filter(executed_at__gte=since).order_by("-executed_at")

    latencies = [row.latency_ms for row in logs if isinstance(row.latency_ms, int)]
    mean_run_time_ms = round(mean(latencies), 2) if latencies else None

    total_runs = logs.count()
    failure_runs = 0
    selected_graph_counts = {}
    checkpoint_requests = 0

    for row in logs:
        payload = row.output_payload or {}
        for step in ("monitor", "predict", "decision", "action", "governance"):
            val = payload.get(step)
            if isinstance(val, str) and val.lower().startswith("warning:"):
                failure_runs += 1
                break

        graph = payload.get("selected_graph")
        if graph:
            selected_graph_counts[graph] = selected_graph_counts.get(graph, 0) + 1

        checkpoint = payload.get("checkpoint_status") or {}
        if isinstance(checkpoint, dict) and checkpoint.get("requires_approval"):
            checkpoint_requests += 1

    failure_rate = round((failure_runs / total_runs), 4) if total_runs else None

    checkpoints = WorkflowCheckpoint.objects.filter(created_at__gte=since)
    checkpoint_total = checkpoints.count()
    resumed = checkpoints.filter(status="resumed").count()
    approval_rate = round((resumed / checkpoint_total), 4) if checkpoint_total else None

    latencies_min = []
    for c in checkpoints.filter(status="resumed", resumed_at__isnull=False):
        if c.created_at and c.resumed_at:
            latencies_min.append((c.resumed_at - c.created_at).total_seconds() / 60.0)
    checkpoint_latency_minutes = round(mean(latencies_min), 2) if latencies_min else None

    latest_eval = (
        OfflineEvaluationRun.objects.filter(status="completed")
        .order_by("-completed_at", "-started_at")
        .first()
    )
    route_accuracy = None
    if latest_eval:
        route_accuracy = (latest_eval.summary_metrics or {}).get("route_accuracy")

    return {
        "window_hours": window_hours,
        "total_runs": total_runs,
        "mean_run_time_ms": mean_run_time_ms,
        "failure_rate": failure_rate,
        "checkpoint_request_count": checkpoint_requests,
        "checkpoint_approval_rate": approval_rate,
        "checkpoint_latency_minutes": checkpoint_latency_minutes,
        "route_accuracy": route_accuracy,
        "selected_graph_distribution": selected_graph_counts,
    }
