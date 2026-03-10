"""
ScheduledAnalysis/__init__.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Azure Function: ScheduledAnalysis

Trigger: Timer — every 30 minutes  (SCHEDULE_CRON env var)

Keeps every org's dashboard data fresh so farmers see results
immediately on login rather than waiting for an agent run to complete.

Per tick:
  1. Fetch all active agricultural orgs + their monitoring zones from Django
  2. Queue one AgentRunRequest per zone (up to 3 zones per org) to Service Bus
  3. AgentOrchestrator picks up and runs each one
  4. Results flow back to Django via callback_url
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

import azure.functions as func
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from shared.models import AgentRunRequest, CosmosStore, ServiceBusPublisher, make_session_id

logger    = logging.getLogger(__name__)
_cosmos   = CosmosStore()
_svc_bus  = ServiceBusPublisher()

BATCH_SIZE = int(os.environ.get("SCHEDULED_QUERY_BATCH_SIZE", "50"))


def main(timer: func.TimerRequest) -> None:
    start = time.time()
    if timer.past_due:
        logger.warning("[ScheduledAnalysis] Timer past due — running anyway")

    logger.info(f"[ScheduledAnalysis] Tick {datetime.now(timezone.utc).isoformat()}")

    orgs = _fetch_active_orgs()
    if not orgs:
        logger.info("[ScheduledAnalysis] No active orgs — nothing to do")
        return

    queued = errors = skipped = 0

    for org in orgs[:BATCH_SIZE]:
        org_id    = str(org.get("org_id") or org.get("id", ""))
        locations = org.get("locations", [])
        if not org_id or not locations:
            skipped += 1
            continue

        # Primary location first, cap at 3 zones per org per tick
        primary_first = sorted(locations, key=lambda l: 0 if l.get("is_primary") else 1)

        for loc in primary_first[:3]:
            try:
                sid = make_session_id(org_id, loc.get("name", ""))
                base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
                run_req = AgentRunRequest(
                    session_id    = sid,
                    org_id        = org_id,
                    location_name = loc.get("name", "Unknown"),
                    lat           = float(loc.get("latitude", 0)),
                    lon           = float(loc.get("longitude", 0)),
                    user_query    = (
                        f"Scheduled 30-minute agricultural climate assessment for "
                        f"{loc.get('name', 'this zone')} ({org.get('org_name', '')}). "
                        f"Assess current and 48-hour flood, drought, and heatwave risk. "
                        f"Provide actionable crop-specific advisories and irrigation guidance."
                    ),
                    org_type     = org.get("org_type", "agriculture"),
                    callback_url = f"{base}/api/agent/callback/{org_id}/{sid}/" if base else "",
                    triggered_by = "scheduler",
                    priority     = "normal",
                )
                _svc_bus.publish_agent_run(run_req)
                _cosmos.write_scheduler_log(org_id, sid, "queued")
                queued += 1
            except Exception as e:
                logger.error(f"[ScheduledAnalysis] queue failed org={org_id} loc={loc.get('name','?')}: {e}")
                errors += 1

    logger.info(
        f"[ScheduledAnalysis] Done — orgs={len(orgs)} "
        f"queued={queued} skipped={skipped} errors={errors} "
        f"elapsed={int((time.time() - start) * 1000)}ms"
    )


def _fetch_active_orgs() -> list[dict]:
    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if not django_base:
        logger.warning("[ScheduledAnalysis] DJANGO_BASE_URL not set")
        return []
    try:
        resp = requests.get(
            f"{django_base}/api/orgs/active/",
            headers={"X-Internal-Token": token},
            params={"org_type": "agriculture", "has_locations": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        orgs = data.get("orgs") or (data if isinstance(data, list) else [])
        logger.info(f"[ScheduledAnalysis] {len(orgs)} active orgs")
        return orgs
    except Exception as e:
        logger.error(f"[ScheduledAnalysis] fetch orgs failed: {e}")
        return []