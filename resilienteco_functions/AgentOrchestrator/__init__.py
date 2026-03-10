"""
AgentOrchestrator/__init__.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Azure Function: AgentOrchestrator

Triggers:
  1. HTTP POST  /api/agent/run  — called from Django view (returns 202)
  2. Service Bus "resilienteco-agent-runs" — processes queued runs

Flow:
  Django view → POST /api/agent/run → 202 + session_id
                                     → SB queue
                                     → this function picks up
                                     → calls Django /api/agent/run/internal/
                                     → writes result to Cosmos
                                     → if ORANGE/RED → publishes to alerts queue
                                     → POSTs callback to Django
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import azure.functions as func
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from shared.models import (
    AgentRunRequest,
    AgentRunResult,
    AlertLevel,
    CosmosStore,
    RunStatus,
    ServiceBusPublisher,
    extract_risk_scores,
    make_session_id,
)

logger = logging.getLogger(__name__)

_cosmos      = CosmosStore()
_service_bus = ServiceBusPublisher()


# ── Entry points ──────────────────────────────────────────────────────────────

def main(req: func.HttpRequest = None, msg: func.ServiceBusMessage = None) -> func.HttpResponse:
    if msg is not None:
        _handle_service_bus(msg)
        return func.HttpResponse(status_code=200)
    return _handle_http(req)


def _handle_http(req: func.HttpRequest) -> func.HttpResponse:
    # Token check
    token    = req.headers.get("X-Internal-Token", "")
    expected = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if expected and token != expected:
        return func.HttpResponse(json.dumps({"error": "Unauthorised"}), status_code=401, mimetype="application/json")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(json.dumps({"error": "Invalid JSON"}), status_code=400, mimetype="application/json")

    try:
        run_req = AgentRunRequest(
            session_id    = body.get("session_id") or make_session_id(str(body.get("org_id", "")), body.get("location_name", "")),
            org_id        = str(body.get("org_id", "")),
            location_name = body.get("location_name", ""),
            lat           = float(body.get("lat", 0)),
            lon           = float(body.get("lon", 0)),
            user_query    = body.get("user_query") or body.get("query", ""),
            org_type      = body.get("org_type", "agriculture"),
            callback_url  = body.get("callback_url", ""),
            triggered_by  = body.get("triggered_by", "user"),
            priority      = body.get("priority", "normal"),
            checkpoint_approved = bool(body.get("checkpoint_approved", False)),
            resume_state  = body.get("resume_state"),
        )
    except (TypeError, ValueError) as e:
        return func.HttpResponse(json.dumps({"error": f"Bad payload: {e}"}), status_code=422, mimetype="application/json")

    # Sync mode: wait and return result (used for development / low-traffic)
    if body.get("sync", False):
        result = _execute(run_req)
        return func.HttpResponse(
            json.dumps(result.to_cosmos_doc(), default=str),
            status_code=200, mimetype="application/json",
        )

    # Async: write queued status, enqueue, return 202
    _cosmos.write_agent_result(AgentRunResult(
        session_id=run_req.session_id, org_id=run_req.org_id,
        location_name=run_req.location_name, status=RunStatus.QUEUED,
        triggered_by=run_req.triggered_by,
    ))
    _service_bus.publish_agent_run(run_req)

    return func.HttpResponse(
        json.dumps({"session_id": run_req.session_id, "status": "queued"}),
        status_code=202, mimetype="application/json",
    )


def _handle_service_bus(msg: func.ServiceBusMessage) -> None:
    try:
        run_req = AgentRunRequest.from_json(msg.get_body().decode("utf-8"))
    except Exception as e:
        logger.error(f"[AgentOrchestrator] SB parse error: {e}")
        return
    result = _execute(run_req)
    if run_req.callback_url:
        _post_callback(run_req.callback_url, result)


# ── Pipeline execution ────────────────────────────────────────────────────────

def _execute(run_req: AgentRunRequest) -> AgentRunResult:
    start = time.time()
    _cosmos.write_agent_result(AgentRunResult(
        session_id=run_req.session_id, org_id=run_req.org_id,
        location_name=run_req.location_name, status=RunStatus.RUNNING,
        triggered_by=run_req.triggered_by,
    ))

    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")

    try:
        resp = requests.post(
            f"{django_base}/api/agent/run/internal/",
            json={
                "session_id":         run_req.session_id,
                "org_id":             run_req.org_id,
                "location_name":      run_req.location_name,
                "lat":                run_req.lat,
                "lon":                run_req.lon,
                "user_query":         run_req.user_query,
                "checkpoint_approved": run_req.checkpoint_approved,
                "resume_state":       run_req.resume_state,
                "triggered_by":       run_req.triggered_by,
            },
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            timeout=300,
        )
        resp.raise_for_status()
        run_data = resp.json()
    except requests.exceptions.Timeout:
        return _error_result(run_req, "Agent pipeline timed out", time.time() - start)
    except Exception as e:
        return _error_result(run_req, str(e), time.time() - start)

    flood, drought, heatwave = extract_risk_scores(run_data)
    dd  = run_data.get("decision_data", {}) or {}
    ad  = run_data.get("action_data",   {}) or {}
    raw = (dd.get("alert_level") or "GREEN").upper()
    try:
        alert_level = AlertLevel(raw)
    except ValueError:
        alert_level = AlertLevel.GREEN

    result = AgentRunResult(
        session_id          = run_req.session_id,
        org_id              = run_req.org_id,
        location_name       = run_req.location_name,
        status              = RunStatus.CHECKPOINT if run_data.get("checkpoint_status", {}).get("requires_approval") else RunStatus.COMPLETED,
        alert_level         = alert_level,
        flood_risk          = flood,
        drought_risk        = drought,
        heatwave_risk       = heatwave,
        alert_message       = ad.get("alert_message", ""),
        sms_message         = ad.get("sms_message", ""),
        recommended_actions = dd.get("recommended_actions", []),
        agent_chain         = run_data.get("agent_chain", []),
        task_ledger         = run_data.get("task_ledger", []),
        explainability      = run_data.get("explainability", {}),
        checkpoint_status   = run_data.get("checkpoint_status", {}),
        weather_summary     = run_data.get("weather_summary", {}),
        full_result         = run_data,
        latency_ms          = int((time.time() - start) * 1000),
        triggered_by        = run_req.triggered_by,
    )

    _cosmos.write_agent_result(result)

    # Fan-out to notification queue if actionable
    if alert_level.is_actionable:
        _service_bus.publish_notification(result.to_notification_payload())
        logger.warning(
            f"[AgentOrchestrator] {alert_level.value} — "
            f"flood={flood}% drought={drought}% heat={heatwave}% "
            f"→ notification queued"
        )

    logger.info(
        f"[AgentOrchestrator] session={run_req.session_id} "
        f"alert={alert_level.value} latency={result.latency_ms}ms"
    )
    return result


def _error_result(run_req: AgentRunRequest, error: str, elapsed: float) -> AgentRunResult:
    r = AgentRunResult(
        session_id=run_req.session_id, org_id=run_req.org_id,
        location_name=run_req.location_name, status=RunStatus.FAILED,
        error=error, latency_ms=int(elapsed * 1000),
        triggered_by=run_req.triggered_by,
    )
    _cosmos.write_agent_result(r)
    logger.error(f"[AgentOrchestrator] FAILED session={run_req.session_id}: {error}")
    return r


def _post_callback(url: str, result: AgentRunResult) -> None:
    token = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    try:
        requests.post(
            url,
            json=result.to_cosmos_doc(),
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            timeout=15,
        )
    except Exception as e:
        logger.warning(f"[AgentOrchestrator] Callback failed: {e}")