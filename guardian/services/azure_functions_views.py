"""
guardian/api/azure_functions_views.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Internal Django API views consumed by Azure Functions.
All protected by X-Internal-Token header.

Wire into urls.py:
    from guardian.api import azure_functions_views as af

    urlpatterns += [
        # Agent pipeline
        path('api/agent/run/internal/',                  af.internal_agent_run),
        path('api/agent/callback/<str:org_id>/<str:session_id>/', af.agent_callback),
        path('api/agent/status/<str:org_id>/<str:session_id>/',   af.agent_status),

        # Alerts
        path('api/alerts/internal/create/',              af.internal_create_alert),

        # Org data (for scheduler + notification dispatcher)
        path('api/orgs/active/',                         af.active_orgs),
        path('api/orgs/<str:org_id>/contacts/',          af.org_contacts),

        # Browser push subscriptions
        path('api/push/vapid-public-key/',               af.vapid_public_key),
        path('api/push/subscribe/',                      af.push_subscribe),
        path('api/push/subscriptions/<str:org_id>/',     af.push_subscriptions),
        path('api/push/subscriptions/remove/',           af.push_unsubscribe),

        # Webhook event audit log (from WebhookIngress)
        path('api/webhooks/internal/event/',             af.webhook_event_received),
    ]
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

INTERNAL_TOKEN = os.environ.get("DJANGO_INTERNAL_TOKEN", "")


# ── Auth helper ───────────────────────────────────────────────────────────────

def internal_only(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if INTERNAL_TOKEN:
            if request.headers.get("X-Internal-Token", "") != INTERNAL_TOKEN:
                return JsonResponse({"error": "Unauthorised"}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper


def _body(request) -> dict:
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# AGENT PIPELINE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
@internal_only
def internal_agent_run(request):
    """
    Called by AgentOrchestrator. Runs run_all_agents() synchronously
    and returns the full result. This is the compute endpoint.
    """
    data = _body(request)
    location_name = data.get("location_name", "")
    user_query    = data.get("user_query") or data.get("query", "")
    session_id    = data.get("session_id", "")
    org_id        = data.get("org_id", "")

    if not location_name or not user_query:
        return JsonResponse({"error": "location_name and user_query required"}, status=422)

    try:
        from guardian.agents.core_agents import run_all_agents
        result = run_all_agents(
            user_query          = user_query,
            lat                 = float(data.get("lat", 0)),
            lon                 = float(data.get("lon", 0)),
            city_name           = location_name,
            session_id          = session_id,
            checkpoint_approved = bool(data.get("checkpoint_approved", False)),
            resume_state        = data.get("resume_state"),
        )
        # Cache for polling
        cache.set(f"agent_result:{org_id}:{session_id}", result, timeout=3600)
        return JsonResponse({**result, "session_id": session_id, "org_id": org_id})
    except Exception as e:
        logger.exception(f"[internal_agent_run] session={session_id}")
        return JsonResponse({"error": str(e), "session_id": session_id}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
@internal_only
def agent_callback(request, org_id: str, session_id: str):
    """
    Called by AgentOrchestrator after async run completes.
    Writes to cache + DB, optionally broadcasts via Django Channels.
    """
    data = _body(request)
    cache.set(f"agent_result:{org_id}:{session_id}", data, timeout=3600)

    # Persist to AgentExecutionLog
    try:
        from guardian.models import AgentExecutionLog, Organization
        org = Organization.objects.filter(id=org_id).first()
        if org:
            AgentExecutionLog.objects.update_or_create(
                session_id=session_id,
                defaults={
                    "organization":   org,
                    "output_payload": data,
                    "latency_ms":     data.get("latency_ms"),
                    "status":         data.get("status", "completed"),
                    "triggered_by":   data.get("triggered_by", "azure_function"),
                    "executed_at":    datetime.now(timezone.utc),
                },
            )
    except Exception as e:
        logger.warning(f"[agent_callback] DB write failed: {e}")

    # Real-time broadcast via Django Channels (best-effort)
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer:
            dd = data.get("decision_data", {}) or {}
            async_to_sync(layer.group_send)(f"org_{org_id}", {
                "type":        "agent.result",
                "session_id":  session_id,
                "alert_level": dd.get("alert_level", "GREEN"),
                "status":      data.get("status", "completed"),
            })
    except Exception:
        pass

    return JsonResponse({"status": "received", "session_id": session_id})


@require_http_methods(["GET"])
@internal_only
def agent_status(request, org_id: str, session_id: str):
    """Frontend + FunctionsClient polls this until status is terminal."""
    result = cache.get(f"agent_result:{org_id}:{session_id}")
    if result:
        return JsonResponse({"session_id": session_id, "status": result.get("status", "completed"), "result": result})

    try:
        from guardian.models import AgentExecutionLog
        log = AgentExecutionLog.objects.filter(session_id=session_id).first()
        if log:
            return JsonResponse({
                "session_id": session_id,
                "status":     log.status or "completed",
                "result":     log.output_payload or {},
            })
    except Exception:
        pass

    return JsonResponse({"session_id": session_id, "status": "pending", "result": None})


# ══════════════════════════════════════════════════════════════════════════════
# ALERT LOG
# ══════════════════════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
@internal_only
def internal_create_alert(request):
    """Called by NotificationDispatcher to write an AlertLog entry."""
    data = _body(request)
    org_id = data.get("org_id", "")

    try:
        from guardian.models import AlertLog, Organization, SavedLocation
        org = Organization.objects.filter(id=org_id).first()
        if not org:
            return JsonResponse({"error": f"Org {org_id} not found"}, status=404)

        location = SavedLocation.objects.filter(
            organization=org,
            name__icontains=data.get("location_name", ""),
            is_active=True,
        ).first()

        AlertLog.objects.create(
            organization        = org,
            location            = location,
            risk_type           = data.get("risk_type", "flood"),
            risk_level          = int(data.get("risk_level", 0)),
            alert_level         = data.get("alert_level", "GREEN"),
            message             = data.get("message", ""),
            source              = "azure_function",
            session_id          = data.get("session_id", ""),
            recommended_actions = data.get("recommended_actions", []),
            triggered_by        = data.get("triggered_by", "azure_function"),
        )
        return JsonResponse({"status": "created"}, status=201)
    except Exception as e:
        logger.exception(f"[internal_create_alert] org={org_id}")
        return JsonResponse({"error": str(e)}, status=500)


# ══════════════════════════════════════════════════════════════════════════════
# ORG DATA  (for ScheduledAnalysis + NotificationDispatcher)
# ══════════════════════════════════════════════════════════════════════════════

@require_http_methods(["GET"])
@internal_only
def active_orgs(request):
    """Returns active orgs + their monitoring locations for the scheduler."""
    org_type = request.GET.get("org_type", "")
    has_locs = request.GET.get("has_locations", "").lower() == "true"
    try:
        from guardian.models import Organization, SavedLocation
        qs = Organization.objects.filter(is_active=True)
        if org_type:
            qs = qs.filter(org_type=org_type)

        result = []
        for org in qs:
            locs = list(
                SavedLocation.objects
                .filter(organization=org, is_active=True)
                .values("id", "name", "latitude", "longitude", "is_primary", "radius_km")
                .order_by("-is_primary", "name")
            )
            if has_locs and not locs:
                continue
            result.append({
                "org_id":    str(org.id),
                "org_name":  org.name,
                "org_type":  org.org_type,
                "locations": locs,
            })
        return JsonResponse({"orgs": result, "count": len(result)})
    except Exception as e:
        logger.exception("[active_orgs]")
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
@internal_only
def org_contacts(request, org_id: str):
    """Returns SMS/email contacts for an org (used by NotificationDispatcher)."""
    try:
        from guardian.models import Organization, OrganizationMembership
        org = Organization.objects.filter(id=org_id, is_active=True).first()
        if not org:
            return JsonResponse({"error": "Not found"}, status=404)

        phones = []
        emails = []
        for m in OrganizationMembership.objects.filter(organization=org, is_active=True).select_related("user"):
            u = m.user
            p = getattr(u, "profile", None)
            if m.role in ("admin", "operator"):
                if u.email:
                    emails.append(u.email)
                if p and getattr(p, "phone_number", None):
                    phones.append(p.phone_number)
            elif m.role == "analyst" and u.email:
                emails.append(u.email)

        settings = getattr(org, "settings_json", {}) or {}
        webhooks = [
            {"url": w["url"], "secret": w.get("secret", "")}
            for w in settings.get("alert_webhooks", []) if w.get("url")
        ]
        return JsonResponse({"org_id": str(org.id), "phone_numbers": phones, "emails": emails, "webhooks": webhooks})
    except Exception as e:
        logger.exception(f"[org_contacts] org={org_id}")
        return JsonResponse({"error": str(e)}, status=500)


# ══════════════════════════════════════════════════════════════════════════════
# BROWSER PUSH SUBSCRIPTION MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@require_http_methods(["GET"])
def vapid_public_key(request):
    """
    Returns the VAPID public key so the browser can subscribe.
    Called by the frontend service worker registration code.
    Public — no internal token required.
    """
    key = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not key:
        return JsonResponse({"error": "VAPID not configured"}, status=503)
    return JsonResponse({"publicKey": key})


@csrf_exempt
@require_http_methods(["POST"])
def push_subscribe(request):
    """
    Stores a browser Push subscription object from an authenticated user.
    Called by the frontend after navigator.serviceWorker.ready + pushManager.subscribe().

    Expected body:
    {
      "subscription": {
        "endpoint": "https://fcm.googleapis.com/...",
        "keys": { "p256dh": "...", "auth": "..." }
      }
    }

    Requires normal Django session auth (not internal token).
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    data         = _body(request)
    subscription = data.get("subscription", {})
    endpoint     = subscription.get("endpoint", "")
    keys         = subscription.get("keys", {})

    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return JsonResponse({"error": "Invalid subscription object"}, status=400)

    try:
        from guardian.models import PushSubscription, OrganizationMembership
        # Get the user's primary org
        membership = OrganizationMembership.objects.filter(
            user=request.user, is_active=True
        ).select_related("organization").first()

        if not membership:
            return JsonResponse({"error": "No active organisation membership"}, status=403)

        PushSubscription.objects.update_or_create(
            user     = request.user,
            endpoint = endpoint,
            defaults={
                "organization": membership.organization,
                "p256dh":       keys["p256dh"],
                "auth":         keys["auth"],
                "user_agent":   request.META.get("HTTP_USER_AGENT", "")[:200],
                "is_active":    True,
                "subscribed_at": datetime.now(timezone.utc),
            },
        )
        return JsonResponse({"status": "subscribed"}, status=201)
    except Exception as e:
        logger.exception("[push_subscribe]")
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
@internal_only
def push_subscriptions(request, org_id: str):
    """
    Returns all active push subscriptions for an org.
    Called by NotificationDispatcher to fan out browser push.
    """
    try:
        from guardian.models import PushSubscription
        subs = PushSubscription.objects.filter(
            organization_id=org_id, is_active=True
        ).values("endpoint", "p256dh", "auth")

        return JsonResponse({
            "subscriptions": [
                {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
                for s in subs
            ]
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
@internal_only
def push_unsubscribe(request):
    """
    Deactivates an expired/revoked push subscription.
    Called by NotificationDispatcher when push returns HTTP 410 Gone.
    """
    data     = _body(request)
    endpoint = data.get("endpoint", "")
    if not endpoint:
        return JsonResponse({"error": "endpoint required"}, status=400)
    try:
        from guardian.models import PushSubscription
        PushSubscription.objects.filter(endpoint=endpoint).update(is_active=False)
        return JsonResponse({"status": "removed"})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ══════════════════════════════════════════════════════════════════════════════
# WEBHOOK EVENT AUDIT LOG
# ══════════════════════════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
@internal_only
def webhook_event_received(request):
    """
    Called by WebhookIngress after normalising each government API event.
    Writes to WebhookEventLog so staff can audit all inbound alerts.
    """
    data = _body(request)
    try:
        from guardian.models import WebhookEventLog, Organization
        org_id = data.get("org_id", "")
        org    = Organization.objects.filter(id=org_id).first() if org_id else None

        WebhookEventLog.objects.create(
            event_id         = data.get("event_id", ""),
            source           = data.get("source", "government_api"),
            organization     = org,
            location_name    = data.get("location_name", ""),
            severity         = data.get("severity", "unknown"),
            event_type       = data.get("event_type", "unknown"),
            description      = data.get("description", ""),
            triggered_agent  = bool(data.get("triggered_agent", False)),
            received_at      = data.get("received_at") or datetime.now(timezone.utc).isoformat(),
        )
        return JsonResponse({"status": "logged"}, status=201)
    except Exception as e:
        logger.warning(f"[webhook_event_received] {e}")
        return JsonResponse({"status": "ok"})  # Don't cause retries for log failures