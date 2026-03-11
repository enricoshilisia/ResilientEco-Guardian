"""
guardian/consumers.py

ResilientEco Guardian - WebSocket Consumer
Renders structured JSON agent output as clean visual cards.
"""

import json
import re
import logging
from html import escape
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from guardian.models import Organization

from guardian.services.report_generator_client import (
    call_report_generator,
    is_report_request,
    detect_report_type,
    render_report_as_chat_html,
    render_report_for_met_chat,
    resolve_org_report_domain,          # ← new import
)

logger = logging.getLogger(__name__)

KENYA_CITIES = {
    'nairobi':   (-1.2921, 36.8219),
    'mombasa':   (-4.0435, 39.6682),
    'taveta':    (-3.3980, 37.6830),
    'kisumu':    (-0.0917, 34.7680),
    'nakuru':    (-0.3031, 36.0800),
    'eldoret':   (0.5143,  35.2698),
    'kakamega':  (0.2827,  34.7519),
    'kitale':    (1.0157,  35.0062),
    'thika':     (-1.0332, 37.0690),
    'malindi':   (-3.2167, 40.1167),
    'kisii':     (-0.6817, 34.7667),
    'nyeri':     (-0.4167, 36.9500),
    'kikambala': (-3.8056, 39.8083),
}

AGENT_STYLES = {
    'monitor':     {'label': 'Monitor Agent',     'color': '#7c3aed'},
    'predict':     {'label': 'Predict Agent',     'color': '#4f46e5'},
    'decision':    {'label': 'Decision Agent',    'color': '#d97706'},
    'action':      {'label': 'Action Agent',      'color': '#059669'},
    'governance':  {'label': 'Governance Agent',  'color': '#dc2626'},
    'mcp_actions': {'label': 'Azure MCP Actions', 'color': '#0078d4'},
}


def _extract_location_from_query(message: str):
    """
    Resolve a location label and coordinates from free-text chat.
    Returns known coordinates for built-in cities, otherwise location name only.
    """
    default_city = "Nairobi"
    text = (message or "").lower()

    for city, (lat, lon) in KENYA_CITIES.items():
        if city in text:
            return city.title(), lat, lon

    match = re.search(r"\b(?:weather|forecast|risk)\s+(?:in|for)\s+([a-zA-Z\s-]+)", text)
    if match:
        raw = re.sub(r"[^a-zA-Z\s-]", "", match.group(1)).strip()
        if raw:
            name = " ".join(raw.split()).title()
            return name, None, None

    cleaned = re.sub(r"[^a-zA-Z\s-]", "", text).strip()
    if cleaned:
        words = [w for w in cleaned.split() if w]
        location_query_terms = {
            "weather", "forecast", "risk", "flood", "drought", "heatwave",
            "temperature", "rain", "climate", "today", "tomorrow",
            "in", "for", "what", "is", "the", "show", "me", "and", "or",
        }
        if len(words) <= 4 and not any(w in location_query_terms for w in words):
            return " ".join(words).title(), None, None

    return default_city, KENYA_CITIES["nairobi"][0], KENYA_CITIES["nairobi"][1]


def _render_runtime_metadata(results: dict) -> str:
    if not isinstance(results, dict):
        return ""

    intent           = escape(str(results.get("intent_classification", "unknown")))
    intent_confidence = escape(str(results.get("intent_confidence", "")))
    intent_source    = escape(str(results.get("intent_source", "")))
    graph            = escape(str(results.get("selected_graph", "unknown")))

    pipeline = results.get("pipeline") or []
    if isinstance(pipeline, list) and pipeline:
        pipeline_text = " -> ".join(escape(str(step)) for step in pipeline)
    else:
        pipeline_text = "not provided"

    routing_features = results.get("routing_features") or {}
    chips = []
    if isinstance(routing_features, dict):
        for key, value in routing_features.items():
            if value is None:
                continue
            chips.append(
                f'<span style="background:#e0f2fe;color:#075985;padding:2px 8px;'
                f'border-radius:99px;font-size:11px;">{escape(str(key))}: {escape(str(value))}</span>'
            )
    routing_html = "".join(chips) or '<span style="color:#9ca3af;font-size:11px;">No routing features</span>'

    task_ledger  = results.get("task_ledger") or []
    total_tasks  = len(task_ledger) if isinstance(task_ledger, list) else 0
    completed    = sum(1 for t in (task_ledger or []) if isinstance(t, dict) and t.get("status") == "completed")
    failed       = sum(1 for t in (task_ledger or []) if isinstance(t, dict) and t.get("status") == "failed")

    checkpoint = results.get("checkpoint_status") or {}
    checkpoint_html = ""
    if isinstance(checkpoint, dict) and checkpoint:
        requires      = checkpoint.get("requires_approval", False)
        approved      = checkpoint.get("approved", False)
        role          = escape(str(checkpoint.get("approval_role", "admin")))
        pending_action = escape(str(checkpoint.get("pending_action", "")))
        resume_step   = escape(str(checkpoint.get("resume_from_step", "")))
        badge_color   = "#ef4444" if requires and not approved else "#22c55e"
        badge_text    = "PENDING APPROVAL" if requires and not approved else "APPROVED / NOT REQUIRED"
        checkpoint_html = (
            f'<div style="margin-top:8px;font-size:11px;color:#6b7280;">CHECKPOINT</div>'
            f'<div style="margin-top:4px;display:flex;gap:6px;flex-wrap:wrap;">'
            f'<span style="background:{badge_color};color:white;padding:2px 8px;border-radius:99px;font-size:11px;">{badge_text}</span>'
            f'<span style="background:#f3f4f6;color:#374151;padding:2px 8px;border-radius:99px;font-size:11px;">role: {role}</span>'
            f'<span style="background:#f3f4f6;color:#374151;padding:2px 8px;border-radius:99px;font-size:11px;">next: {resume_step or "n/a"}</span>'
            f'</div>'
            f'<div style="font-size:12px;color:#374151;margin-top:4px;">{pending_action}</div>'
        )
    else:
        checkpoint_html = (
            '<div style="margin-top:8px;font-size:12px;color:#6b7280;">'
            '<strong>checkpoint_status:</strong> not required (non-critical run)'
            '</div>'
        )

    explainability   = results.get("explainability") or {}
    pipeline_config  = results.get("pipeline_config") or {}
    pipeline_config_html = ""
    if isinstance(pipeline_config, dict) and pipeline_config:
        pipeline_config_html = (
            '<div style="margin-top:8px;font-size:11px;color:#64748b;">PIPELINE CONFIG</div>'
            f'<div style="font-size:12px;color:#334155;">'
            f'{escape(str(pipeline_config.get("config_name", "global_graph")))}'
            f'@{escape(str(pipeline_config.get("config_version", "default")))} '
            f'({escape(str(pipeline_config.get("config_source", "default")))})'
            f'</div>'
        )
    why_graph  = escape(str(explainability.get("why_selected_graph", ""))) if isinstance(explainability, dict) else ""
    why_alert  = escape(str(explainability.get("why_alert_level", "")))    if isinstance(explainability, dict) else ""
    explain_html = ""
    if why_graph or why_alert:
        explain_html = (
            '<div style="margin-top:8px;font-size:11px;color:#64748b;">EXPLAINABILITY</div>'
            f'<div style="font-size:12px;color:#334155;">{why_graph}</div>'
            f'<div style="font-size:12px;color:#334155;">{why_alert}</div>'
        )

    return (
        '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:10px;">'
        '<div style="font-size:11px;color:#64748b;margin-bottom:6px;">RUNTIME ROUTING</div>'
        f'<div style="font-size:12px;margin-bottom:4px;"><strong>intent_classification:</strong> {intent}</div>'
        f'<div style="font-size:12px;margin-bottom:4px;"><strong>intent_confidence:</strong> {intent_confidence or "n/a"} ({intent_source or "unknown"})</div>'
        f'<div style="font-size:12px;margin-bottom:4px;"><strong>selected_graph:</strong> {graph}</div>'
        f'<div style="font-size:12px;margin-bottom:6px;"><strong>pipeline:</strong> {pipeline_text}</div>'
        f'<div style="font-size:11px;color:#64748b;margin-bottom:4px;">routing_features</div>'
        f'<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px;">{routing_html}</div>'
        f'<div style="font-size:12px;"><strong>task_ledger:</strong> {completed}/{total_tasks} completed, {failed} failed</div>'
        f'{checkpoint_html}'
        f'{pipeline_config_html}'
        f'{explain_html}'
        '</div>'
    )


def _risk_color(val):
    try:
        n = int(val)
        if n >= 70: return '#ef4444'
        if n >= 40: return '#f97316'
        return '#22c55e'
    except Exception:
        return '#6b7280'


def _alert_badge(level):
    colors = {'RED': '#ef4444', 'ORANGE': '#f97316', 'YELLOW': '#eab308', 'GREEN': '#22c55e'}
    c = colors.get(str(level).upper(), '#6b7280')
    return f'<span style="background:{c};color:white;padding:2px 10px;border-radius:99px;font-size:11px;font-weight:700;">{level}</span>'


def _bar(val):
    c = _risk_color(val)
    try:
        pct = max(0, min(100, int(val)))
    except Exception:
        pct = 0
    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:8px;margin:4px 0 2px;">'
        f'<div style="width:{pct}%;background:{c};height:8px;border-radius:4px;transition:width 0.5s;"></div>'
        f'</div>'
    )


def render_monitor(d):
    temp      = d.get('temperature_c', '—')
    precip    = d.get('precipitation_mm', '—')
    rain24    = d.get('rain_24h_mm', '—')
    humid     = d.get('humidity_pct', '—')
    dq        = d.get('data_quality_score', '—')
    anomalies = d.get('anomalies', [])
    signals   = d.get('alert_signals', [])
    anom_html = ''.join(
        f'<div style="color:#f97316;font-size:12px;margin:2px 0;">{a}</div>'
        for a in anomalies
    ) or '<div style="color:#9ca3af;font-size:12px;">None detected</div>'
    sig_html = ''.join(
        f'<div style="font-size:12px;margin:2px 0;">Risk {s.get("risk_level","?")}% — {s.get("status","")}</div>'
        for s in signals
    ) or '<div style="color:#9ca3af;font-size:12px;">No signals</div>'
    return f'''
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;">
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{temp}°C</div>
    <div style="font-size:10px;color:#6b7280;">Temp</div>
  </div>
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{precip}mm</div>
    <div style="font-size:10px;color:#6b7280;">Now</div>
  </div>
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{rain24}mm</div>
    <div style="font-size:10px;color:#6b7280;">24h Rain</div>
  </div>
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{humid}%</div>
    <div style="font-size:10px;color:#6b7280;">Humidity</div>
  </div>
</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">ANOMALIES</div>{anom_html}
<div style="font-size:11px;color:#6b7280;margin:8px 0 4px;">ALERT SIGNALS</div>{sig_html}
<div style="font-size:11px;color:#9ca3af;margin-top:8px;">Data quality: {dq}/100</div>'''


def render_predict(d):
    flood   = d.get('flood_risk', 0)
    drought = d.get('drought_risk', 0)
    heat    = d.get('heatwave_risk', 0)
    overall = d.get('overall_risk_level', '—').upper()
    conf    = d.get('confidence_pct', '—')
    primary = d.get('primary_risk', '—')
    reason  = d.get('reasoning', '')
    oc      = _risk_color(flood if overall in ('HIGH', 'CRITICAL') else drought)
    return f'''
<div style="margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px;"><span>Flood Risk</span><span style="color:{_risk_color(flood)};font-weight:700;">{flood}%</span></div>
  {_bar(flood)}
  <div style="display:flex;justify-content:space-between;font-size:12px;margin:6px 0 2px;"><span>Drought Risk</span><span style="color:{_risk_color(drought)};font-weight:700;">{drought}%</span></div>
  {_bar(drought)}
  <div style="display:flex;justify-content:space-between;font-size:12px;margin:6px 0 2px;"><span>Heatwave Risk</span><span style="color:{_risk_color(heat)};font-weight:700;">{heat}%</span></div>
  {_bar(heat)}
</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;">
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;"><span style="color:#6b7280;">Overall: </span><strong style="color:{oc};">{overall}</strong></div>
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;"><span style="color:#6b7280;">Primary: </span><strong>{primary}</strong></div>
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;"><span style="color:#6b7280;">Confidence: </span><strong>{conf}%</strong></div>
</div>
<div style="font-size:12px;color:#374151;background:#f9fafb;padding:8px;border-radius:6px;line-height:1.5;">{reason}</div>'''


def render_decision(d):
    level    = d.get('alert_level', 'GREEN')
    immed    = d.get('immediate_action_required', False)
    actions  = d.get('recommended_actions', [])
    groups   = d.get('notify_groups', [])
    priority = d.get('priority', '—')
    pop      = d.get('estimated_affected_population', '—')
    hrs      = d.get('response_timeline_hours', '—')
    act_html = ''.join(
        f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #f3f4f6;">{a}</div>'
        for a in actions
    )
    grp_html = ' '.join(
        f'<span style="background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:99px;font-size:11px;">{g}</span>'
        for g in groups
    )
    immed_badge = (
        '<span style="background:#ef4444;color:white;padding:2px 8px;border-radius:99px;font-size:11px;">YES</span>'
        if immed else
        '<span style="background:#22c55e;color:white;padding:2px 8px;border-radius:99px;font-size:11px;">NO</span>'
    )
    base = f'''
<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
  <div>{_alert_badge(level)}</div>
  <div style="font-size:12px;color:#6b7280;">Immediate action: {immed_badge}</div>
  <div style="font-size:12px;color:#6b7280;">Priority: <strong>{priority}</strong></div>
</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">RECOMMENDED ACTIONS</div>
<div style="margin-bottom:10px;">{act_html}</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:6px;">NOTIFY</div>
<div style="margin-bottom:10px;display:flex;flex-wrap:wrap;gap:4px;">{grp_html}</div>'''
    if isinstance(pop, int):
        base += f'''
<div style="display:flex;gap:8px;flex-wrap:wrap;">
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;">~{pop:,} affected</div>
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;">{hrs}h response window</div>
</div>'''
    return base


def render_action(d):
    alert_msg = d.get('alert_message', '')
    sms       = d.get('sms_message', '')
    rtype     = d.get('risk_type', '—')
    rlevel    = d.get('risk_level', 0)
    steps     = d.get('immediate_steps', [])
    resources = d.get('resources_needed', [])
    steps_html = ''.join(
        f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #f3f4f6;">{s}</div>'
        for s in steps
    )
    res_html = ' '.join(
        f'<span style="background:#dcfce7;color:#166534;padding:2px 8px;border-radius:99px;font-size:11px;">{r}</span>'
        for r in resources
    )
    return f'''
<div style="background:#fef3c7;border-radius:8px;padding:10px;margin-bottom:10px;font-size:13px;line-height:1.5;">
  {alert_msg}
</div>
<div style="background:#1e293b;color:#86efac;border-radius:8px;padding:8px 12px;font-family:monospace;font-size:12px;margin-bottom:10px;">
  SMS: {sms}
</div>
<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;">Type: <strong>{rtype}</strong></div>
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;">Level: <strong style="color:{_risk_color(rlevel)};">{rlevel}%</strong></div>
</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">IMMEDIATE STEPS</div>
<div style="margin-bottom:10px;">{steps_html}</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:6px;">RESOURCES</div>
<div style="display:flex;flex-wrap:wrap;gap:4px;">{res_html}</div>'''


def render_governance(d):
    approved = d.get('approved', False)
    issues   = d.get('issues', [])
    flags    = d.get('rai_flags', [])
    rec      = d.get('final_recommendation', '')
    conf     = d.get('confidence_in_chain', '—')
    sdgs     = d.get('sdg_alignment', [])
    ap_badge = (
        '<span style="background:#22c55e;color:white;padding:3px 12px;border-radius:99px;font-weight:700;">APPROVED</span>'
        if approved else
        '<span style="background:#ef4444;color:white;padding:3px 12px;border-radius:99px;font-weight:700;">REJECTED</span>'
    )
    issues_html = ''.join(
        f'<div style="color:#ef4444;font-size:12px;padding:2px 0;">{i}</div>'
        for i in issues
    ) or '<div style="color:#9ca3af;font-size:12px;">None</div>'
    flags_html = ''.join(
        f'<div style="color:#f97316;font-size:12px;padding:2px 0;">{f}</div>'
        for f in flags
    ) or '<div style="color:#9ca3af;font-size:12px;">None</div>'
    sdg_html = ' '.join(
        f'<span style="background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:99px;font-size:11px;">{s}</span>'
        for s in sdgs
    )
    return f'''
<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
  {ap_badge}
  <div style="font-size:12px;color:#6b7280;">Chain confidence: <strong>{conf}%</strong></div>
</div>
<div style="background:#f0fdf4;border-radius:8px;padding:10px;margin-bottom:10px;font-size:13px;line-height:1.5;">
  {rec}
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
  <div><div style="font-size:11px;color:#6b7280;margin-bottom:4px;">ISSUES</div>{issues_html}</div>
  <div><div style="font-size:11px;color:#6b7280;margin-bottom:4px;">RAI FLAGS</div>{flags_html}</div>
</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:6px;">UN SDG ALIGNMENT</div>
<div style="display:flex;flex-wrap:wrap;gap:4px;">{sdg_html}</div>'''


def parse_and_render(agent_key, payload):
    """Render agent payload (dict or text). Falls back to plain text for unstructured output."""
    if isinstance(payload, dict):
        if agent_key == 'monitor':    return render_monitor(payload)
        if agent_key == 'predict':    return render_predict(payload)
        if agent_key == 'decision':   return render_decision(payload)
        if agent_key == 'action':     return render_action(payload)
        if agent_key == 'governance': return render_governance(payload)
        return f'<pre style="font-size:12px;">{escape(json.dumps(payload, ensure_ascii=False, indent=2))}</pre>'

    text = str(payload or "")
    try:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            data = json.loads(match.group())
            if agent_key == 'monitor':    return render_monitor(data)
            if agent_key == 'predict':    return render_predict(data)
            if agent_key == 'decision':   return render_decision(data)
            if agent_key == 'action':     return render_action(data)
            if agent_key == 'governance': return render_governance(data)
    except Exception:
        pass

    # Fallback: basic markdown -> html
    lines = text.split('\n')
    html = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        line = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line)
        if line.startswith('- ') or line.startswith('* '):
            html.append(f'<div style="font-size:13px;padding:2px 0;">{line[2:]}</div>')
        else:
            html.append(f'<div style="font-size:13px;padding:2px 0;">{line}</div>')
    return ''.join(html)


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):

        # Try URL route kwargs first, then query string
        org_id = self.scope['url_route']['kwargs'].get('org_id')
        if not org_id:
            qs = dict(
                pair.split('=', 1)
                for pair in self.scope.get('query_string', b'').decode().split('&')
                if '=' in pair
            )
            org_id = qs.get('org_id')

        self.org = None
        if org_id:
            try:
                self.org = await sync_to_async(
                    Organization.objects.select_related().get
                )(id=org_id)
            except Exception:
                pass

        await self.accept()
        await self.send(text_data=json.dumps({
            'message': '<strong>ResilientEco Guardian</strong> — multi-agent pipeline ready · Azure AI Foundry · Azure MCP',
            'type': 'system'
        }))

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        data    = json.loads(text_data)
        message = data.get('message', '').lower()

        await self.send(text_data=json.dumps({
            'message': 'Initializing agent pipeline via Azure AI Foundry...',
            'type': 'thinking'
        }))

        # ── Resolve location ─────────────────────────────────────────
        location_name, lat, lon = _extract_location_from_query(message)
        if lat is None or lon is None:
            from .services.weather_service import geocode_location_name
            resolved = await sync_to_async(geocode_location_name)(location_name)
            if resolved and resolved.get("lat") is not None and resolved.get("lon") is not None:
                lat           = float(resolved["lat"])
                lon           = float(resolved["lon"])
                resolved_name = resolved.get("name") or location_name
                country       = resolved.get("country") or ""
                location_name = f"{resolved_name}, {country}" if country else resolved_name
                await self.send(text_data=json.dumps({
                    'message': (
                        f'<span style="font-size:12px;color:#64748b;">'
                        f'Location resolved to <strong>{location_name}</strong> ({lat:.4f}, {lon:.4f})'
                        f'</span>'
                    ),
                    'type': 'system'
                }))
            else:
                lat, lon = KENYA_CITIES["nairobi"]
                await self.send(text_data=json.dumps({
                    'message': (
                        '<span style="font-size:12px;color:#b45309;">'
                        'Could not geocode location — using Nairobi coordinates as fallback.'
                        '</span>'
                    ),
                    'type': 'system'
                }))

        # ── Report generation via Azure Function ─────────────────────
        if is_report_request(message):
            await self.send(text_data=json.dumps({
                'type':    'thinking',
                'message': 'Generating detailed report — fetching live weather data and running analysis...'
            }))

            from guardian.models import SavedLocation

            org = getattr(self, 'org', None)

            # ── Resolve org domain (subtype-aware) ────────────────────
            # resolve_org_report_domain checks org.org_subtype first,
            # then org.org_type, so "meteorological" / "disaster_relief" etc.
            # are all handled correctly without any hardcoded string comparisons.
            org_domain = resolve_org_report_domain(org)
            org_name   = getattr(org, 'name', 'ResilientEco') if org else 'ResilientEco'
            report_type = detect_report_type(message, org_domain)

            if org:
                org_locations_qs = await sync_to_async(list)(
                    SavedLocation.objects.filter(organization=org).values(
                        "name", "latitude", "longitude"
                    )
                )
                report_locations = [
                    {"name": loc["name"], "lat": loc["latitude"], "lon": loc["longitude"]}
                    for loc in org_locations_qs
                ]
            else:
                report_locations = []

            if not report_locations:
                report_locations = [{"name": location_name, "lat": lat, "lon": lon}]

            result = await call_report_generator(
                locations=report_locations,
                org_name=org_name,
                org_type=org_domain,        # ← domain string, not raw org.org_type
                report_type=report_type,
                fmt="both",
            )

            # Use met-styled renderer for meteorological orgs,
            # standard renderer for everything else.
            html = (
                render_report_for_met_chat(result)
                if org_domain == "meteorological"
                else render_report_as_chat_html(result)
            )

            await self.send(text_data=json.dumps({
                'type':         'report',
                'message':      html,
                'report':       result.get('report', {}),
                'pdf_b64':      result.get('pdf_base64', ''),
                'pdf_filename': result.get('pdf_filename', 'climate_report.pdf'),
            }))
            return  # Skip standard pipeline

        # ── Standard agent pipeline ───────────────────────────────────
        try:
            from .agents.core_agents import run_all_agents
            results = await sync_to_async(run_all_agents)(message, lat, lon, location_name)
        except Exception as e:
            logger.exception("Agent pipeline failed")
            await self.send(text_data=json.dumps({
                'message': f'Pipeline error: {str(e)}',
                'type': 'error'
            }))
            return

        chain      = results.get('agent_chain', [])
        session_id = results.get('session_id', 'N/A')

        for agent_key in ['monitor', 'predict', 'decision', 'action', 'governance']:
            output = results.get(agent_key)
            if not output:
                continue

            style = AGENT_STYLES[agent_key]
            step  = next((s for s in chain if s.get('agent') == agent_key), {})

            meta = ''
            if step:
                meta = (
                    f'<div style="font-size:10px;color:#9ca3af;margin-bottom:6px;font-family:monospace;">'
                    f'{step.get("model","")} &nbsp;·&nbsp; {step.get("source","")} &nbsp;·&nbsp; {step.get("latency_ms","")}ms'
                    f'</div>'
                )

            structured_key = f'{agent_key}_data'
            payload = results.get(structured_key) or output
            body    = parse_and_render(agent_key, payload)

            await self.send(text_data=json.dumps({
                'message': meta + body,
                'type':    agent_key,
                'label':   style['label'],
                'color':   style['color'],
            }))

        # Runtime routing panel
        runtime_html = _render_runtime_metadata(results)
        if runtime_html:
            await self.send(text_data=json.dumps({
                'message': runtime_html,
                'type':    'system',
                'label':   'Runtime Routing',
                'color':   '#0ea5e9',
            }))

        # MCP actions panel
        mcp_output = results.get('mcp_actions')
        if mcp_output:
            lines_html = []
            for line in mcp_output.strip().split('\n'):
                is_live = '[LIVE]' in line
                badge   = (
                    '<span style="background:#22c55e;color:white;padding:1px 6px;border-radius:4px;font-size:10px;font-family:monospace;">LIVE</span>'
                    if is_live else
                    '<span style="background:#94a3b8;color:white;padding:1px 6px;border-radius:4px;font-size:10px;font-family:monospace;">SIM</span>'
                )
                clean = line.replace('[LIVE]', '').replace('[SIM]', '').strip()
                lines_html.append(f'<div style="font-size:12px;padding:3px 0;">{badge} {clean}</div>')
            await self.send(text_data=json.dumps({
                'message': ''.join(lines_html),
                'type':    'mcp_actions',
                'label':   'Azure MCP Actions',
                'color':   '#0078d4',
            }))

        # Pipeline summary
        if chain:
            total_ms    = sum(s.get('latency_ms', 0) for s in chain)
            models_used = list(dict.fromkeys(s.get('model', '') for s in chain if s.get('model')))
            sources     = list(dict.fromkeys(s.get('source', '') for s in chain if s.get('source')))
            await self.send(text_data=json.dumps({
                'message': (
                    f'<strong>Pipeline Complete — {location_name}</strong><br>'
                    f'<span style="font-size:12px;color:#6b7280;">'
                    f'Session {session_id} &nbsp;·&nbsp; {len(chain)} agents &nbsp;·&nbsp; {total_ms}ms total &nbsp;·&nbsp; '
                    f'{", ".join(models_used)} via {", ".join(sources)}'
                    f'</span>'
                ),
                'type': 'complete',
            }))