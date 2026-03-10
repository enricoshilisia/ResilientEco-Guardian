"""
WebhookIngress/__init__.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Azure Function: WebhookIngress

Trigger: HTTP POST — external government disaster alert APIs call this
         endpoint when they issue new alerts.

Responsibilities:
  1. Validate the inbound webhook (HMAC-SHA256 signature or API key)
  2. Normalise the payload into a standard WebhookEvent regardless of
     which government API sent it (CAP-XML, GeoJSON, custom JSON)
  3. Persist the raw event to Cosmos DB for auditability
  4. Decide whether the event warrants an immediate agent run
  5. If yes → publish AgentRunRequest to Service Bus queue
  6. Return 200 quickly (government APIs retry on timeout)

Supported government alert formats:
  - CAP (Common Alerting Protocol) v1.2 — XML — used by many national
    meteorological services (KMD Kenya, South Africa SAWS, ECMWF)
  - GDACS GeoJSON — Global Disaster Alert and Coordination System
  - FEMA IPAWS / NWS Atom feed items — US National Weather Service
  - Generic JSON — for custom government integrations

Registration: Each government API partner is assigned a unique
webhook path suffix and secret. Configure in WEBHOOK_SOURCES env var.

Environment variables:
  WEBHOOK_SOURCES   — JSON mapping of source_id → { secret, format, org_ids }
  DJANGO_BASE_URL
  DJANGO_INTERNAL_TOKEN
  SERVICE_BUS_CONNECTION_STRING
  COSMOS_CONNECTION_STRING
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import azure.functions as func
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from shared.models import (
    AgentRunRequest,
    CosmosStore,
    ServiceBusPublisher,
    WebhookEvent,
    WebhookSource,
    make_session_id,
    safe_float,
)

logger = logging.getLogger(__name__)

_cosmos      = CosmosStore()
_service_bus = ServiceBusPublisher()

# CAP XML namespace
CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Receives inbound government disaster alert webhooks.
    Designed to return 200 in <500ms — government APIs will retry on failure.
    """
    start = time.time()

    # ── Identify source from path or header ───────────────────────────────────
    # Webhook URL pattern: POST /api/webhook/{source_id}
    # e.g. /api/webhook/kenya_kmd  or  /api/webhook/gdacs
    source_id = req.route_params.get("source_id", "unknown").lower().strip()

    # ── Load source config ────────────────────────────────────────────────────
    sources_cfg = _load_sources_config()
    source_cfg  = sources_cfg.get(source_id)

    if not source_cfg:
        logger.warning(f"[WebhookIngress] Unknown source_id: {source_id}")
        # Return 200 anyway — we don't want external systems retrying unknown routes
        return _ok({"status": "received", "note": "unregistered source"})

    # ── Validate signature / API key ──────────────────────────────────────────
    raw_body = req.get_body()
    if not _validate_request(req, raw_body, source_cfg):
        logger.warning(f"[WebhookIngress] Signature validation failed: source={source_id}")
        return func.HttpResponse(
            json.dumps({"error": "Forbidden"}),
            status_code=403,
            mimetype="application/json",
        )

    # ── Parse payload ─────────────────────────────────────────────────────────
    fmt = source_cfg.get("format", "json").lower()
    try:
        if fmt == "cap_xml":
            events = _parse_cap_xml(raw_body, source_id, source_cfg)
        elif fmt == "gdacs_geojson":
            events = _parse_gdacs_geojson(raw_body, source_id, source_cfg)
        elif fmt == "nws_atom":
            events = _parse_nws_atom(raw_body, source_id, source_cfg)
        else:
            events = _parse_generic_json(raw_body, source_id, source_cfg)
    except Exception as e:
        logger.error(f"[WebhookIngress] Parse failed source={source_id} fmt={fmt}: {e}")
        # Still return 200 — we logged the error, no point in retries
        return _ok({"status": "parse_error", "detail": str(e)})

    if not events:
        return _ok({"status": "no_actionable_events"})

    # ── Process each normalised event ─────────────────────────────────────────
    triggered = 0
    stored    = 0

    for event in events:
        # Persist to Cosmos for audit trail
        _cosmos.write_webhook_event(event)
        stored += 1

        # Notify Django so alert log is updated
        _notify_django(event)

        # Trigger agent run if severity warrants it
        if event.should_trigger_agent and event.lat is not None:
            for org_id in source_cfg.get("org_ids", []):
                event.org_id = org_id
                sid = make_session_id(org_id, event.location_name)
                run_req = event.to_agent_run_request(session_id=sid)
                run_req.callback_url = _build_callback_url(org_id, sid)
                _service_bus.publish_agent_run(run_req)
                triggered += 1
                logger.info(
                    f"[WebhookIngress] Triggered agent run "
                    f"org={org_id} loc={event.location_name} "
                    f"severity={event.severity} event_type={event.event_type}"
                )

    elapsed_ms = int((time.time() - start) * 1000)
    logger.info(
        f"[WebhookIngress] source={source_id} events={len(events)} "
        f"stored={stored} triggered={triggered} elapsed={elapsed_ms}ms"
    )

    return _ok({
        "status":    "processed",
        "events":    len(events),
        "triggered": triggered,
    })


# ══════════════════════════════════════════════════════════════════════════════
# PAYLOAD PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_cap_xml(raw: bytes, source_id: str, cfg: dict) -> list[WebhookEvent]:
    """
    Parse CAP (Common Alerting Protocol) v1.2 XML.
    Used by Kenya Meteorological Department, SAWS, ECMWF, WMO members.

    CAP structure:
      <alert>
        <identifier>…</identifier>
        <info>
          <event>Flood Warning</event>
          <severity>Extreme|Severe|Moderate|Minor|Unknown</severity>
          <area>
            <areaDesc>Nairobi County</areaDesc>
            <circle>lat,lon radius</circle>  or  <polygon>…</polygon>
          </area>
        </info>
      </alert>
    """
    events = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"Invalid CAP XML: {e}")

    # Handle both single alert and feed of alerts
    alerts = root.findall(f"{{{CAP_NS}}}alert") or ([root] if root.tag == f"{{{CAP_NS}}}alert" else [])

    for alert in alerts:
        alert_id   = _cap_text(alert, "identifier") or str(uuid.uuid4())
        sent       = _cap_text(alert, "sent") or datetime.now(timezone.utc).isoformat()
        status     = _cap_text(alert, "status", "").upper()

        # Skip test and exercise alerts from government systems
        if status in ("TEST", "EXERCISE", "DRAFT"):
            logger.info(f"[WebhookIngress] Skipping CAP status={status} id={alert_id}")
            continue

        for info in alert.findall(f"{{{CAP_NS}}}info"):
            event_type = _cap_text(info, "event", "unknown")
            severity   = _cap_text(info, "severity", "Unknown").lower()
            urgency    = _cap_text(info, "urgency", "Unknown").lower()
            headline   = _cap_text(info, "headline", "")
            description= _cap_text(info, "description", "")

            # Extract coordinates from area
            lat, lon, location_name = _cap_extract_location(info)

            normalised_severity = _normalise_cap_severity(severity, urgency)
            should_trigger = normalised_severity in ("high", "critical")

            events.append(WebhookEvent(
                event_id      = f"cap:{alert_id}:{source_id}",
                source        = WebhookSource.GOVERNMENT_API,
                org_id        = "",  # filled per org_id in main()
                raw_payload   = {"alert_id": alert_id, "sent": sent, "headline": headline},
                location_name = location_name or cfg.get("default_location", ""),
                lat           = lat,
                lon           = lon,
                severity      = normalised_severity,
                event_type    = _normalise_event_type(event_type),
                description   = headline or description or event_type,
                should_trigger_agent = should_trigger and bool(lat),
            ))

    return events


def _parse_gdacs_geojson(raw: bytes, source_id: str, cfg: dict) -> list[WebhookEvent]:
    """
    Parse GDACS (Global Disaster Alert and Coordination System) GeoJSON.
    GDACS sends FeatureCollection with properties: eventtype, alertlevel,
    episodealertlevel, country, fromdate, todate, coordinates.

    Alert levels: Green=0, Orange=1, Red=2
    Event types: EQ, TC, FL, DR, VO, WF (earthquake, cyclone, flood, drought, volcano, wildfire)
    """
    data = json.loads(raw)
    events = []

    features = data.get("features", [data] if data.get("type") == "Feature" else [])

    for feat in features:
        props = feat.get("properties", {})
        geo   = feat.get("geometry", {})

        event_type   = props.get("eventtype", "unknown")
        alert_colour = str(props.get("alertlevel", props.get("episodealertlevel", "Green"))).lower()
        country      = props.get("country", "")
        description  = props.get("htmldescription", props.get("name", ""))
        event_id     = str(props.get("eventid", props.get("episodeid", uuid.uuid4())))

        # Coordinates
        coords = geo.get("coordinates", [])
        lat = lon = None
        if coords and len(coords) >= 2:
            lon, lat = safe_float(coords[0]), safe_float(coords[1])

        # GDACS alert level → severity
        severity_map = {"green": "low", "orange": "medium", "red": "high"}
        severity = severity_map.get(alert_colour, "low")

        # Only flood (FL) and drought (DR) are immediately relevant to agriculture
        agri_relevant  = event_type.upper() in ("FL", "DR", "TC", "WF")
        should_trigger = agri_relevant and severity in ("medium", "high")

        events.append(WebhookEvent(
            event_id      = f"gdacs:{event_id}",
            source        = WebhookSource.GOVERNMENT_API,
            org_id        = "",
            raw_payload   = props,
            location_name = country or cfg.get("default_location", ""),
            lat           = lat,
            lon           = lon,
            severity      = severity,
            event_type    = _normalise_event_type(event_type),
            description   = str(description)[:500],
            should_trigger_agent = should_trigger and bool(lat),
        ))

    return events


def _parse_nws_atom(raw: bytes, source_id: str, cfg: dict) -> list[WebhookEvent]:
    """
    Parse NWS (US National Weather Service) Atom/GeoRSS feed items.
    Typically sent as individual <entry> elements or a full feed.
    """
    ATOM_NS   = "http://www.w3.org/2005/Atom"
    GEORSS_NS = "http://www.georss.org/georss"

    events = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"Invalid Atom XML: {e}")

    entries = root.findall(f"{{{ATOM_NS}}}entry")
    if not entries:
        # Single entry
        entries = [root] if root.tag == f"{{{ATOM_NS}}}entry" else []

    for entry in entries:
        title   = _xml_text(entry, f"{{{ATOM_NS}}}title", "")
        summary = _xml_text(entry, f"{{{ATOM_NS}}}summary", "")
        entry_id = _xml_text(entry, f"{{{ATOM_NS}}}id", str(uuid.uuid4()))

        # GeoRSS point
        point = _xml_text(entry, f"{{{GEORSS_NS}}}point", "")
        lat = lon = None
        if point:
            parts = point.strip().split()
            if len(parts) == 2:
                lat, lon = safe_float(parts[0]), safe_float(parts[1])

        # Guess severity from NWS title keywords
        title_lower = title.lower()
        if any(w in title_lower for w in ("emergency", "extreme", "tornado warning")):
            severity = "critical"
        elif any(w in title_lower for w in ("warning", "hurricane", "flash flood warning")):
            severity = "high"
        elif any(w in title_lower for w in ("watch", "advisory", "flood watch")):
            severity = "medium"
        else:
            severity = "low"

        event_type = _normalise_event_type(title)
        should_trigger = severity in ("high", "critical") and bool(lat)

        events.append(WebhookEvent(
            event_id      = f"nws:{hashlib.sha1(entry_id.encode()).hexdigest()[:10]}",
            source        = WebhookSource.GOVERNMENT_API,
            org_id        = "",
            raw_payload   = {"title": title, "summary": summary},
            location_name = cfg.get("default_location", ""),
            lat           = lat,
            lon           = lon,
            severity      = severity,
            event_type    = event_type,
            description   = title or summary,
            should_trigger_agent = should_trigger,
        ))

    return events


def _parse_generic_json(raw: bytes, source_id: str, cfg: dict) -> list[WebhookEvent]:
    """
    Generic JSON parser for custom government API integrations.
    Maps fields using the source config's field_map (configured per partner).

    Example field_map in WEBHOOK_SOURCES config:
      "field_map": {
        "event_id":   "id",
        "latitude":   "location.lat",
        "longitude":  "location.lon",
        "severity":   "risk_level",
        "event_type": "alert_category",
        "description":"message"
      }
    """
    data = json.loads(raw)
    field_map = cfg.get("field_map", {})

    # Support both single object and array
    items = data if isinstance(data, list) else [data]
    events = []

    for item in items:
        def get_field(key: str, default="") -> str:
            mapped = field_map.get(key, key)
            # Support dot-notation for nested fields: "location.lat"
            parts = mapped.split(".")
            val = item
            for part in parts:
                if isinstance(val, dict):
                    val = val.get(part, "")
                else:
                    val = ""
                    break
            return str(val) if val is not None else default

        event_id  = get_field("event_id") or str(uuid.uuid4())
        severity  = _normalise_severity_str(get_field("severity", "unknown"))
        lat_str   = get_field("latitude",  "")
        lon_str   = get_field("longitude", "")
        lat       = safe_float(lat_str) if lat_str else None
        lon       = safe_float(lon_str) if lon_str else None

        events.append(WebhookEvent(
            event_id      = f"{source_id}:{event_id}",
            source        = WebhookSource.GOVERNMENT_API,
            org_id        = "",
            raw_payload   = item,
            location_name = get_field("location_name") or cfg.get("default_location", ""),
            lat           = lat,
            lon           = lon,
            severity      = severity,
            event_type    = _normalise_event_type(get_field("event_type", "unknown")),
            description   = get_field("description", "")[:500],
            should_trigger_agent = severity in ("high", "critical") and bool(lat),
        ))

    return events


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def _validate_request(req: func.HttpRequest, raw_body: bytes, cfg: dict) -> bool:
    """
    Validate inbound webhook authenticity.
    Supports HMAC-SHA256 signature (preferred) or static API key.
    """
    auth_type = cfg.get("auth_type", "api_key").lower()

    if auth_type == "hmac_sha256":
        # Signature in header: X-Hub-Signature-256: sha256=<hex>
        # Standard used by GitHub, many government APIs
        secret    = cfg.get("secret", "").encode()
        sig_header= req.headers.get("X-Hub-Signature-256", "") or \
                    req.headers.get("X-Signature-SHA256", "")
        if not sig_header or not secret:
            return False
        expected = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig_header)

    elif auth_type == "api_key":
        # API key in header: X-API-Key or Authorization: Bearer <key>
        expected_key = cfg.get("api_key", "")
        if not expected_key:
            return True  # No key configured → accept (dev mode)
        provided = (
            req.headers.get("X-API-Key", "") or
            req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        return hmac.compare_digest(expected_key, provided)

    elif auth_type == "none":
        # Explicitly open (use only for government APIs on IP allowlist + VNet)
        return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# NORMALISATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalise_cap_severity(severity: str, urgency: str) -> str:
    """Map CAP severity+urgency → internal low/medium/high/critical."""
    s = severity.lower()
    u = urgency.lower()
    if s == "extreme" or (s == "severe" and u == "immediate"):
        return "critical"
    if s == "severe":
        return "high"
    if s == "moderate":
        return "medium"
    return "low"


def _normalise_severity_str(raw: str) -> str:
    raw = raw.lower()
    if raw in ("critical", "extreme", "emergency", "5", "red"):
        return "critical"
    if raw in ("high", "severe", "4", "orange"):
        return "high"
    if raw in ("medium", "moderate", "3", "yellow"):
        return "medium"
    return "low"


def _normalise_event_type(raw: str) -> str:
    raw = raw.upper()
    if any(w in raw for w in ("FL", "FLOOD", "FLASH")):
        return "flood"
    if any(w in raw for w in ("DR", "DROUGHT")):
        return "drought"
    if any(w in raw for w in ("TC", "CYCLONE", "HURRICANE", "TROPICAL")):
        return "cyclone"
    if any(w in raw for w in ("HEAT", "HW", "HIGH TEMP")):
        return "heatwave"
    if any(w in raw for w in ("WF", "WILD", "FIRE")):
        return "wildfire"
    if any(w in raw for w in ("EQ", "QUAKE", "SEISMIC")):
        return "earthquake"
    return "general_hazard"


def _cap_text(element, tag: str, default: str = "") -> str:
    el = element.find(f"{{{CAP_NS}}}{tag}")
    return (el.text or default) if el is not None else default


def _xml_text(element, tag: str, default: str = "") -> str:
    el = element.find(tag)
    return (el.text or default) if el is not None else default


def _cap_extract_location(info_element) -> tuple[Optional[float], Optional[float], str]:
    """Extract lat, lon, and area description from a CAP <info><area> element."""
    lat = lon = None
    name = ""
    for area in info_element.findall(f"{{{CAP_NS}}}area"):
        name = _cap_text(area, "areaDesc", "")
        # Try <circle>: "lat,lon radius"
        circle = _cap_text(area, "circle", "")
        if circle:
            parts = circle.split()
            if parts:
                coords = parts[0].split(",")
                if len(coords) == 2:
                    lat, lon = safe_float(coords[0]), safe_float(coords[1])
                    break
        # Try <polygon>: "lat1,lon1 lat2,lon2 …" — take centroid of first point
        polygon = _cap_text(area, "polygon", "")
        if polygon:
            first_pt = polygon.strip().split()[0].split(",")
            if len(first_pt) == 2:
                lat, lon = safe_float(first_pt[0]), safe_float(first_pt[1])
                break
        # Try <geocode> with value pairs
        for geocode in area.findall(f"{{{CAP_NS}}}geocode"):
            name_el  = geocode.find(f"{{{CAP_NS}}}valueName")
            value_el = geocode.find(f"{{{CAP_NS}}}value")
            if name_el is not None and value_el is not None:
                if name_el.text in ("LAT", "latitude"):
                    lat = safe_float(value_el.text or "0")
                if name_el.text in ("LON", "LONG", "longitude"):
                    lon = safe_float(value_el.text or "0")
    return lat, lon, name


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG + HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _load_sources_config() -> dict:
    """
    Load webhook source configs from WEBHOOK_SOURCES env var (JSON).

    Example WEBHOOK_SOURCES value:
    {
      "kenya_kmd": {
        "format":   "cap_xml",
        "auth_type":"hmac_sha256",
        "secret":   "your-shared-secret",
        "org_ids":  ["org_abc123", "org_def456"],
        "default_location": "Nairobi, Kenya"
      },
      "gdacs": {
        "format":   "gdacs_geojson",
        "auth_type":"api_key",
        "api_key":  "gdacs-api-key",
        "org_ids":  ["org_abc123"],
        "default_location": "Kenya"
      },
      "us_nws": {
        "format":   "nws_atom",
        "auth_type":"none",
        "org_ids":  ["org_us001"],
        "default_location": "United States"
      }
    }
    """
    raw = os.environ.get("WEBHOOK_SOURCES", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("[WebhookIngress] WEBHOOK_SOURCES is not valid JSON")
        return {}


def _notify_django(event: WebhookEvent) -> None:
    """POST the normalised event to Django so it appears in the audit log."""
    django_base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    token       = os.environ.get("DJANGO_INTERNAL_TOKEN", "")
    if not django_base:
        return
    try:
        requests.post(
            f"{django_base}/api/webhooks/internal/event/",
            json={
                "event_id":      event.event_id,
                "source":        event.source.value if isinstance(event.source, WebhookSource) else event.source,
                "org_id":        event.org_id,
                "location_name": event.location_name,
                "severity":      event.severity,
                "event_type":    event.event_type,
                "description":   event.description,
                "received_at":   event.received_at,
                "triggered_agent": event.should_trigger_agent,
            },
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            timeout=8,
        )
    except Exception as e:
        logger.warning(f"[WebhookIngress] Django notify failed: {e}")


def _build_callback_url(org_id: str, session_id: str) -> str:
    base = os.environ.get("DJANGO_BASE_URL", "").rstrip("/")
    return f"{base}/api/agent/callback/{org_id}/{session_id}/" if base else ""


def _ok(body: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body),
        status_code=200,
        mimetype="application/json",
    )