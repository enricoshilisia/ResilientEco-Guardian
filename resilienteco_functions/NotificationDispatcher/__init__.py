"""
NotificationDispatcher/__init__.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Azure Function: NotificationDispatcher

Trigger: Service Bus queue "resilienteco-alerts"

Receives alert payloads from AgentOrchestrator (when risk = ORANGE/RED)
and fans out to all configured notification channels:

  1. SMS           — Azure Communication Services SMS
  2. Email         — Azure Communication Services Email
  3. Browser Push  — Web Push API (VAPID) via stored subscriptions
  4. Django write-back — AlertLog entry + in-app notification bell

Channels are scoped per org and per alert level.
ORANGE: email + browser push + in-app
RED:    SMS + email + browser push + in-app

Environment variables:
  ACS_CONNECTION_STRING
  ACS_SENDER_PHONE
  ACS_EMAIL_SENDER
  VAPID_PRIVATE_KEY        — base64url VAPID private key
  VAPID_PUBLIC_KEY         — base64url VAPID public key
  VAPID_CLAIMS_EMAIL       — mailto: claim e.g. admin@resilienteco.com
  DJANGO_BASE_URL
  DJANGO_INTERNAL_TOKEN
  SERVICE_BUS_CONNECTION_STRING
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
from shared.models import AlertLevel, CosmosStore

logger = logging.getLogger(__name__)
_cosmos = CosmosStore()

# ── ACS availability check ────────────────────────────────────────────────────
try:
    from azure.communication.sms import SmsClient
    from azure.communication.email import EmailClient
    ACS_AVAILABLE = True
except ImportError:
    ACS_AVAILABLE = False

# ── Web Push availability check ───────────────────────────────────────────────
try:
    from pywebpush import webpush, WebPushException
    WEBPUSH_AVAILABLE = True
except ImportError:
    WEBPUSH_AVAILABLE = False
    logger.warning("[NotificationDispatcher] pywebpush not installed — browser push disabled")


# Alert level → which channels fire
CHANNEL_MATRIX = {
    AlertLevel.RED:    {"sms": True,  "email": True,  "browser_push": True,  "in_app": True},
    AlertLevel.ORANGE: {"sms": False, "email": True,  "browser_push": True,  "in_app": True},
    AlertLevel.YELLOW: {"sms": False, "email": False, "browser_push": False, "in_app": True},
    AlertLevel.GREEN:  {"sms": False, "email": False, "browser_push": False, "in_app": False},
}


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main(msg: func.ServiceBusMessage) -> None:
    start = time.time()

    try:
        payload = json.loads(msg.get_body().decode("utf-8"))
    except Exception as e:
        logger.error(f"[NotificationDispatcher] Failed to parse SB message: {e}")
        return

    session_id    = payload.get("session_id", "")
    org_id        = payload.get("org_id", "")
    location_name = payload.get("location_name", "")
    alert_message = payload.get("alert_message", "")
    sms_message   = payload.get("sms_message", "")
    recommended   = payload.get("recommended_actions", [])
    flood_risk    = int(payload.get("flood_risk", 0))
    drought_risk  = int(payload.get("drought_risk", 0))
    heatwave_risk = int(payload.get("heatwave_risk", 0))
    triggered_by  = payload.get("triggered_by", "system")

    raw_level = payload.get("alert_level", "GREEN").upper()
    try:
        alert_level = AlertLevel(raw_level)
    except ValueError:
        alert_level = AlertLevel.GREEN

    channels_cfg = CHANNEL_MATRIX.get(alert_level, CHANNEL_MATRIX[AlertLevel.GREEN])

    logger.info(
        f"[NotificationDispatcher] {alert_level.value} alert "
        f"org={org_id} loc={location_name} session={session_id}"
    )

    # Fetch contacts once
    contacts = _fetch_org_contacts(org_id)
    dispatch_results = []

    # ── 1. SMS (RED only) ─────────────────────────────────────────────────────
    if channels_cfg["sms"] and ACS_AVAILABLE:
        body = sms_message or _build_sms_body(
            location_name, alert_level.value, flood_risk, drought_risk, heatwave_risk
        )
        for phone in contacts.get("phone_numbers", []):
            ok, err = _send_sms(phone, body)
            dispatch_results.append({
                "channel": "sms", "to": phone,
                "status": "sent" if ok else "failed", "error": err,
            })

    # ── 2. Email (ORANGE + RED) ───────────────────────────────────────────────
    if channels_cfg["email"] and ACS_AVAILABLE:
        subject  = f"[{alert_level.value}] Agricultural Alert — {location_name}"
        html     = _build_email_html(
            location_name, alert_level, alert_message,
            flood_risk, drought_risk, heatwave_risk, recommended,
        )
        for email in contacts.get("emails", []):
            ok, err = _send_email(email, subject, html)
            dispatch_results.append({
                "channel": "email", "to": email,
                "status": "sent" if ok else "failed", "error": err,
            })

    # ── 3. Browser Push (ORANGE + RED) ────────────────────────────────────────
    if channels_cfg["browser_push"] and WEBPUSH_AVAILABLE:
        push_payload = json.dumps({
            "title":   f"ResilientEco — {alert_level.value} Alert",
            "body":    f"{location_name}: {alert_message or _short_summary(flood_risk, drought_risk, heatwave_risk)}",
            "icon":    "/static/img/logo-192.png",
            "badge":   "/static/img/badge-72.png",
            "data": {
                "url":         f"/dashboard/?alert={session_id}",
                "session_id":  session_id,
                "alert_level": alert_level.value,
            },
            "actions": [
                {"action": "view",    "title": "View Advisory"},
                {"action": "dismiss", "title": "Dismiss"},
            ],
            "tag":     f"alert-{session_id}",   # replaces previous notification with same tag
            "renotify": True,
        })

        subscriptions = _fetch_push_subscriptions(org_id)
        for sub in subscriptions:
            ok, err = _send_browser_push(sub, push_payload)
            dispatch_results.append({
                "channel":   "browser_push",
                "endpoint":  sub.get("endpoint", "")[:60] + "…",
                "status":    "sent" if ok else "failed",
                "error":     err,
            })

    # ── 4. In-app notification bell + Django AlertLog ─────────────────────────
    if channels_cfg["in_app"]:
        _write_to_django(
            org_id, session_id, location_name, alert_level,
            alert_message, flood_risk, drought_risk, heatwave_risk,
            recommended, triggered_by,
        )
        dispatch_results.append({"channel": "in_app", "status": "written"})

    # ── Persist dispatch log to Cosmos ────────────────────────────────────────
    _cosmos.write_notification_log({
        "id":           f"{org_id}:{session_id}",
        "partitionKey": org_id,
        "org_id":       org_id,
        "session_id":   session_id,
        "location_name": location_name,
        "alert_level":  alert_level.value,
        "channels":     dispatch_results,
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms":   int((time.time() - start) * 1000),
    })

    sent_count   = sum(1 for r in dispatch_results if r.get("status") == "sent")
    failed_count = sum(1 for r in dispatch_results if r.get("status") == "failed")
    logger.info(
        f"[NotificationDispatcher] Done — "
        f"sent={sent_count} failed={failed_count} "
        f"latency={int((time.time() - start) * 1000)}ms"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SENDERS
# ══════════════════════════════════════════════════════════════════════════════

def _send_sms(to_phone: str, body: str) -> tuple[bool, str]:
    conn   = os.environ.get("ACS_CONNECTION_STRING", "")
    sender = os.environ.get("ACS_SENDER_PHONE", "")
    if not conn or not sender:
        logger.info(f"[NotificationDispatcher][STUB] SMS → {to_phone}")
        return True, ""
    try:
        client = SmsClient.from_connection_string(conn)
        result = client.send(from_=sender, to=[to_phone], message=body[:160])[0]
        return (True, "") if result.successful else (False, result.error_message or "ACS error")
    except Exception as e:
        return False, str(e)


def _send_email(to_email: str, subject: str, html_body: str) -> tuple[bool, str]:
    conn   = os.environ.get("ACS_CONNECTION_STRING", "")
    sender = os.environ.get("ACS_EMAIL_SENDER", "alerts@resilienteco.azurecomm.net")
    if not conn:
        logger.info(f"[NotificationDispatcher][STUB] Email → {to_email}: {subject}")
        return True, ""
    try:
        client  = EmailClient.from_connection_string(conn)
        poller  = client.begin_send({
            "senderAddress": sender,
            "recipients": {"to": [{"address": to_email}]},
            "content": {
                "subject":   subject,
                "html":      html_body,
                "plainText": subject,
            },
        })
        poller.result()
        return True, ""
    except Exception as e:
        return False, str(e)


def _send_browser_push(subscription: dict, payload_json: str) -> tuple[bool, str]:
    """
    Send a Web Push notification using VAPID authentication.

    subscription dict structure (stored by Django when browser subscribes):
      {
        "endpoint": "https://fcm.googleapis.com/fcm/send/...",
        "keys": {
          "p256dh": "base64url public key",
          "auth":   "base64url auth secret"
        }
      }
    """
    private_key   = os.environ.get("VAPID_PRIVATE_KEY", "")
    claims_email  = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:admin@resilienteco.com")

    if not private_key:
        logger.info("[NotificationDispatcher][STUB] Browser push — VAPID_PRIVATE_KEY not set")
        return True, ""

    endpoint = subscription.get("endpoint", "")
    keys     = subscription.get("keys", {})
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return False, "Invalid subscription object"

    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys":     keys,
            },
            data=payload_json,
            vapid_private_key=private_key,
            vapid_claims={"sub": claims_email},
        )
        return True, ""
    except WebPushException as e:
        # HTTP 410 Gone = subscription expired/unsubscribed — remove it
        if "410" in str(e):
            _remove_push_subscription(subscription)
            return False, "subscription_expired"
        return False, str(e)
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
# DJANGO INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_org_contacts(org_id: str) -> dict:
    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if not django_base:
        return {"phone_numbers": [], "emails": []}
    try:
        resp = requests.get(
            f"{django_base}/api/orgs/{org_id}/contacts/",
            headers={"X-Internal-Token": token},
            timeout=10,
        )
        return resp.json() if resp.ok else {"phone_numbers": [], "emails": []}
    except Exception as e:
        logger.warning(f"[NotificationDispatcher] fetch contacts failed: {e}")
        return {"phone_numbers": [], "emails": []}


def _fetch_push_subscriptions(org_id: str) -> list[dict]:
    """
    Fetch all active Web Push subscriptions for an org from Django.
    Django stores these when users click "Enable notifications" in the browser.
    """
    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if not django_base:
        return []
    try:
        resp = requests.get(
            f"{django_base}/api/push/subscriptions/{org_id}/",
            headers={"X-Internal-Token": token},
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("subscriptions", [])
    except Exception as e:
        logger.warning(f"[NotificationDispatcher] fetch push subscriptions failed: {e}")
    return []


def _remove_push_subscription(subscription: dict) -> None:
    """Tell Django to remove an expired push subscription."""
    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if not django_base:
        return
    try:
        requests.post(
            f"{django_base}/api/push/subscriptions/remove/",
            json={"endpoint": subscription.get("endpoint", "")},
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            timeout=8,
        )
    except Exception:
        pass


def _write_to_django(
    org_id: str, session_id: str, location_name: str,
    alert_level: AlertLevel, alert_message: str,
    flood_risk: int, drought_risk: int, heatwave_risk: int,
    recommended: list, triggered_by: str,
) -> None:
    """Write to Django AlertLog + trigger in-app notification bell update."""
    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if not django_base:
        return

    primary_risk = max(
        ("flood", flood_risk), ("drought", drought_risk), ("heatwave", heatwave_risk),
        key=lambda x: x[1],
    )[0]

    try:
        requests.post(
            f"{django_base}/api/alerts/internal/create/",
            json={
                "org_id":       org_id,
                "session_id":   session_id,
                "location_name": location_name,
                "risk_type":    primary_risk,
                "risk_level":   max(flood_risk, drought_risk, heatwave_risk),
                "alert_level":  alert_level.value,
                "message":      alert_message,
                "recommended_actions": recommended,
                "triggered_by": triggered_by,
                "source":       "azure_function",
            },
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            timeout=12,
        )
    except Exception as e:
        logger.error(f"[NotificationDispatcher] Django write-back failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_sms_body(location: str, level: str, flood: int, drought: int, heat: int) -> str:
    primary = max(("flood", flood), ("drought", drought), ("heatwave", heat), key=lambda x: x[1])[0]
    risk    = max(flood, drought, heat)
    return (
        f"[ResilientEco {level}] {location}: "
        f"{primary.upper()} RISK {risk}%. "
        f"F:{flood}% D:{drought}% H:{heat}%. "
        f"Login for advisory."
    )[:160]


def _short_summary(flood: int, drought: int, heat: int) -> str:
    primary = max(("Flood", flood), ("Drought", drought), ("Heatwave", heat), key=lambda x: x[1])
    return f"{primary[0]} risk at {primary[1]}%."


def _build_email_html(
    location: str, alert_level: AlertLevel, message: str,
    flood: int, drought: int, heat: int, actions: list,
) -> str:
    colour = alert_level.colour_hex
    subject_prefix = {
        AlertLevel.RED:    "CRITICAL ALERT",
        AlertLevel.ORANGE: "HIGH ALERT",
        AlertLevel.YELLOW: "ADVISORY",
        AlertLevel.GREEN:  "INFO",
    }.get(alert_level, "ALERT")

    actions_html = "".join(
        f'<li style="margin-bottom:8px;font-size:14px;color:#334155;line-height:1.5;">{a}</li>'
        for a in (actions or [])
    )
    actions_block = (
        f"""
        <div style="margin-bottom:24px;">
          <p style="font-size:12px;font-weight:700;text-transform:uppercase;
                    letter-spacing:.08em;color:#64748b;margin:0 0 10px;">
            Recommended Actions
          </p>
          <ul style="margin:0;padding-left:18px;">{actions_html}</ul>
        </div>"""
        if actions else ""
    )

    dashboard_url = os.environ.get("DJANGO_BASE_URL", "#").rstrip("/") + "/dashboard/"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;
             font-family:'Segoe UI',Arial,sans-serif;">
  <div style="max-width:600px;margin:32px auto;background:#ffffff;
              border-radius:12px;overflow:hidden;
              box-shadow:0 4px 24px rgba(0,0,0,.10);">

    <!-- Header -->
    <div style="background:{colour};padding:28px 32px;">
      <p style="margin:0 0 4px;font-size:11px;font-weight:700;
                text-transform:uppercase;letter-spacing:.12em;
                color:rgba(255,255,255,.75);">
        ResilientEco Guardian · Agricultural Intelligence
      </p>
      <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;">
        {subject_prefix} — {location}
      </p>
    </div>

    <!-- Body -->
    <div style="padding:28px 32px;">
      <p style="font-size:15px;color:#1e293b;line-height:1.65;margin:0 0 24px;">
        {message or f"{alert_level.value} climate risk detected for {location}."}
      </p>

      <!-- Risk scores -->
      <div style="display:flex;gap:12px;margin-bottom:24px;">
        <div style="flex:1;background:#eff6ff;border-radius:8px;padding:16px;text-align:center;">
          <p style="margin:0;font-size:26px;font-weight:700;color:#2563eb;">{flood}%</p>
          <p style="margin:6px 0 0;font-size:11px;color:#64748b;
                    text-transform:uppercase;letter-spacing:.07em;">Flood</p>
        </div>
        <div style="flex:1;background:#fffbeb;border-radius:8px;padding:16px;text-align:center;">
          <p style="margin:0;font-size:26px;font-weight:700;color:#d97706;">{drought}%</p>
          <p style="margin:6px 0 0;font-size:11px;color:#64748b;
                    text-transform:uppercase;letter-spacing:.07em;">Drought</p>
        </div>
        <div style="flex:1;background:#fff1f2;border-radius:8px;padding:16px;text-align:center;">
          <p style="margin:0;font-size:26px;font-weight:700;color:#dc2626;">{heat}%</p>
          <p style="margin:6px 0 0;font-size:11px;color:#64748b;
                    text-transform:uppercase;letter-spacing:.07em;">Heatwave</p>
        </div>
      </div>

      {actions_block}

      <a href="{dashboard_url}"
         style="display:inline-block;padding:13px 28px;background:#14532d;
                color:#ffffff;border-radius:8px;font-size:14px;font-weight:600;
                text-decoration:none;letter-spacing:.02em;">
        View Full Advisory Dashboard
      </a>
    </div>

    <!-- Footer -->
    <div style="padding:16px 32px;background:#f8fafc;
                border-top:1px solid #e2e8f0;">
      <p style="margin:0;font-size:11px;color:#94a3b8;">
        ResilientEco Guardian · Automated alert ·
        {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
      </p>
    </div>
  </div>
</body></html>"""