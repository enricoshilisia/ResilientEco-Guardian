"""
guardian/services/azure_functions_client.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Drop-in replacement for the stub trigger_azure_function call in
core_agents.py.  Also used by Django views to submit async agent runs.

Graceful fallback: if AZURE_FUNCTION_APP_URL is not configured (local
dev) every method falls back to running run_all_agents() in-process —
the app never shows an error to users.

Usage:
    from guardian.services.azure_functions_client import functions_client

    # Fire-and-forget (returns session_id immediately)
    session_id = functions_client.submit(
        org_id="abc", location_name="Nakuru",
        lat=-0.303, lon=36.08, user_query="Flood risk?",
    )

    # Wait for result (sync — wraps async run with polling)
    result = functions_client.run_sync(org_id="abc", ...)

    # Resume a checkpoint
    functions_client.resume_checkpoint(org_id=..., session_id=..., ...)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class AzureFunctionsClient:

    def __init__(self):
        self._app_url       = os.environ.get("AZURE_FUNCTION_APP_URL", "").rstrip("/")
        self._function_key  = os.environ.get("AZURE_FUNCTION_KEY", "")
        self._internal_token= os.environ.get("DJANGO_INTERNAL_TOKEN", "")
        self._enabled       = bool(self._app_url and self._function_key)
        if not self._enabled:
            logger.info("[FunctionsClient] Stub mode — set AZURE_FUNCTION_APP_URL + AZURE_FUNCTION_KEY to enable")

    # ── Public API ─────────────────────────────────────────────────────────────

    def submit(
        self, *,
        org_id:        str,
        location_name: str,
        lat:           float,
        lon:           float,
        user_query:    str,
        org_type:      str  = "agriculture",
        session_id:    str  = "",
        callback_url:  str  = "",
        triggered_by:  str  = "user",
        priority:      str  = "normal",
        checkpoint_approved: bool         = False,
        resume_state:        Optional[dict] = None,
    ) -> str:
        """Submit an async agent run. Returns session_id immediately (202)."""
        sid = session_id or self._sid(org_id, location_name)
        payload = dict(
            session_id=sid, org_id=str(org_id),
            location_name=location_name, lat=lat, lon=lon,
            user_query=user_query, org_type=org_type,
            callback_url=callback_url, triggered_by=triggered_by,
            priority=priority, checkpoint_approved=checkpoint_approved,
            resume_state=resume_state, sync=False,
        )
        if not self._enabled:
            logger.info(f"[FunctionsClient][STUB] submit session={sid} loc={location_name}")
            return sid
        try:
            resp = requests.post(
                f"{self._app_url}/api/agent/run",
                json=payload, headers=self._headers(), timeout=15,
            )
            if resp.status_code == 202:
                return resp.json().get("session_id", sid)
            logger.warning(f"[FunctionsClient] submit HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"[FunctionsClient] submit failed: {e}")
        return sid

    def run_sync(
        self, *,
        org_id: str, location_name: str,
        lat: float, lon: float, user_query: str,
        org_type: str = "agriculture",
        session_id: str = "",
        triggered_by: str = "user",
        checkpoint_approved: bool = False,
        resume_state: Optional[dict] = None,
        timeout: int = 120,
    ) -> dict:
        """Submit and wait for the result. Falls back to local run on failure."""
        sid = session_id or self._sid(org_id, location_name)
        if not self._enabled:
            return self._local(sid, location_name, lat, lon, user_query, checkpoint_approved, resume_state)

        payload = dict(
            session_id=sid, org_id=str(org_id),
            location_name=location_name, lat=lat, lon=lon,
            user_query=user_query, org_type=org_type,
            triggered_by=triggered_by,
            checkpoint_approved=checkpoint_approved,
            resume_state=resume_state, sync=True,
        )
        try:
            resp = requests.post(
                f"{self._app_url}/api/agent/run",
                json=payload, headers=self._headers(), timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            logger.warning("[FunctionsClient] run_sync timeout — local fallback")
        except Exception as e:
            logger.error(f"[FunctionsClient] run_sync error: {e} — local fallback")
        return self._local(sid, location_name, lat, lon, user_query, checkpoint_approved, resume_state)

    def poll(self, org_id: str, session_id: str, *, max_wait: int = 120, interval: int = 3) -> Optional[dict]:
        """Poll Django status endpoint until result is ready."""
        base     = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{base}/api/agent/status/{org_id}/{session_id}/",
                    headers={"X-Internal-Token": self._internal_token},
                    timeout=10,
                )
                if resp.ok:
                    data   = resp.json()
                    status = data.get("status", "")
                    if status in ("completed", "failed", "checkpoint"):
                        return data
            except Exception as e:
                logger.warning(f"[FunctionsClient] poll error: {e}")
            time.sleep(interval)
        return None

    def resume_checkpoint(
        self, *, org_id: str, session_id: str, approved_by: str,
        location_name: str, lat: float, lon: float,
        user_query: str, resume_state: dict, resume_from_step: str,
    ) -> str:
        return self.submit(
            org_id=org_id, location_name=location_name,
            lat=lat, lon=lon, user_query=user_query,
            session_id=session_id, triggered_by=f"checkpoint_resume:{approved_by}",
            checkpoint_approved=True,
            resume_state={
                **resume_state,
                "checkpoint": {
                    **resume_state.get("checkpoint", {}),
                    "approved": True, "approved_by": approved_by,
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                },
                "resume_from_step": resume_from_step,
            },
        )

    # ── Private ────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "x-functions-key":  self._function_key,
            "X-Internal-Token": self._internal_token,
            "Content-Type":     "application/json",
        }

    @staticmethod
    def _sid(org_id: str, location: str) -> str:
        raw = f"{org_id}:{location}:{datetime.now(timezone.utc).isoformat()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _local(
        sid: str, location_name: str, lat: float, lon: float,
        user_query: str, checkpoint_approved: bool, resume_state: Optional[dict],
    ) -> dict:
        logger.info(f"[FunctionsClient] Local fallback session={sid}")
        try:
            from guardian.agents.core_agents import run_all_agents
            return run_all_agents(
                user_query=user_query, lat=lat, lon=lon,
                city_name=location_name, session_id=sid,
                checkpoint_approved=checkpoint_approved,
                resume_state=resume_state,
            )
        except Exception as e:
            logger.error(f"[FunctionsClient] Local fallback failed: {e}")
            return {
                "session_id": sid, "error": str(e), "status": "failed",
                "monitor": "", "predict": "", "decision": "", "action": "", "governance": "",
                "monitor_data": {}, "predict_data": {}, "decision_data": {},
                "action_data": {}, "governance_data": {},
                "agent_chain": [], "task_ledger": [],
            }


# Singleton
functions_client = AzureFunctionsClient()