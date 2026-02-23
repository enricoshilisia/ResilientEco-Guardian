"""
ResilientEco Guardian - WebSocket Consumer
Renders structured JSON agent output as clean visual cards.
"""

import json
import re
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

KENYA_CITIES = {
    'nairobi':  (-1.2921, 36.8219),
    'mombasa':  (-4.0435, 39.6682),
    'kisumu':   (-0.0917, 34.7680),
    'nakuru':   (-0.3031, 36.0800),
    'eldoret':  (0.5143,  35.2698),
    'kakamega': (0.2827,  34.7519),
    'kitale':   (1.0157,  35.0062),
    'thika':    (-1.0332, 37.0690),
    'malindi':  (-3.2167, 40.1167),
    'kisii':    (-0.6817, 34.7667),
    'nyeri':    (-0.4167, 36.9500),
}

AGENT_STYLES = {
    'monitor':     {'icon': '🔍', 'label': 'Monitor Agent',    'color': '#7c3aed'},
    'predict':     {'icon': '📊', 'label': 'Predict Agent',    'color': '#4f46e5'},
    'decision':    {'icon': '🧠', 'label': 'Decision Agent',   'color': '#d97706'},
    'action':      {'icon': '⚡', 'label': 'Action Agent',     'color': '#059669'},
    'governance':  {'icon': '⚖️', 'label': 'Governance Agent', 'color': '#dc2626'},
    'mcp_actions': {'icon': '☁️', 'label': 'Azure MCP Actions','color': '#0078d4'},
}


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
    temp   = d.get('temperature_c', '—')
    precip = d.get('precipitation_mm', '—')
    rain24 = d.get('rain_24h_mm', '—')
    humid  = d.get('humidity_pct', '—')
    dq     = d.get('data_quality_score', '—')
    anomalies = d.get('anomalies', [])
    signals   = d.get('alert_signals', [])
    anom_html = ''.join(f'<div style="color:#f97316;font-size:12px;margin:2px 0;">⚠️ {a}</div>' for a in anomalies) or '<div style="color:#9ca3af;font-size:12px;">None detected</div>'
    sig_html  = ''.join(
        f'<div style="font-size:12px;margin:2px 0;">🚨 Risk {s.get("risk_level","?")}% — {s.get("status","")}</div>'
        for s in signals
    ) or '<div style="color:#9ca3af;font-size:12px;">No signals</div>'
    return f'''
<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;">
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{temp}°C</div>
    <div style="font-size:10px;color:#6b7280;">🌡️ Temp</div>
  </div>
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{precip}mm</div>
    <div style="font-size:10px;color:#6b7280;">🌧️ Now</div>
  </div>
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{rain24}mm</div>
    <div style="font-size:10px;color:#6b7280;">☔ 24h</div>
  </div>
  <div style="background:#f3f4f6;border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:700;color:#667eea;">{humid}%</div>
    <div style="font-size:10px;color:#6b7280;">💧 Humid</div>
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
    oc      = _risk_color(flood if overall in ('HIGH','CRITICAL') else drought)
    return f'''
<div style="margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px;"><span>🌊 Flood Risk</span><span style="color:{_risk_color(flood)};font-weight:700;">{flood}%</span></div>
  {_bar(flood)}
  <div style="display:flex;justify-content:space-between;font-size:12px;margin:6px 0 2px;"><span>🏜️ Drought Risk</span><span style="color:{_risk_color(drought)};font-weight:700;">{drought}%</span></div>
  {_bar(drought)}
  <div style="display:flex;justify-content:space-between;font-size:12px;margin:6px 0 2px;"><span>🔥 Heatwave Risk</span><span style="color:{_risk_color(heat)};font-weight:700;">{heat}%</span></div>
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
    act_html = ''.join(f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #f3f4f6;">▸ {a}</div>' for a in actions)
    grp_html = ' '.join(f'<span style="background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:99px;font-size:11px;">{g}</span>' for g in groups)
    immed_badge = '<span style="background:#ef4444;color:white;padding:2px 8px;border-radius:99px;font-size:11px;">YES</span>' if immed else '<span style="background:#22c55e;color:white;padding:2px 8px;border-radius:99px;font-size:11px;">NO</span>'
    return f'''
<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
  <div>{_alert_badge(level)}</div>
  <div style="font-size:12px;color:#6b7280;">Immediate action: {immed_badge}</div>
  <div style="font-size:12px;color:#6b7280;">Priority: <strong>{priority}</strong></div>
</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">RECOMMENDED ACTIONS</div>
<div style="margin-bottom:10px;">{act_html}</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:6px;">NOTIFY</div>
<div style="margin-bottom:10px;display:flex;flex-wrap:wrap;gap:4px;">{grp_html}</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;">
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;">👥 ~{pop:,} affected</div>
  <div style="background:#f3f4f6;border-radius:8px;padding:6px 12px;font-size:12px;">⏱️ {hrs}h response window</div>
</div>''' if isinstance(pop, int) else f'''
<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
  <div>{_alert_badge(level)}</div>
  <div style="font-size:12px;color:#6b7280;">Immediate action: {immed_badge}</div>
</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:4px;">RECOMMENDED ACTIONS</div>
<div style="margin-bottom:10px;">{act_html}</div>
<div style="font-size:11px;color:#6b7280;margin-bottom:6px;">NOTIFY</div>
<div style="display:flex;flex-wrap:wrap;gap:4px;">{grp_html}</div>'''


def render_action(d):
    alert_msg = d.get('alert_message', '')
    sms       = d.get('sms_message', '')
    rtype     = d.get('risk_type', '—')
    rlevel    = d.get('risk_level', 0)
    steps     = d.get('immediate_steps', [])
    resources = d.get('resources_needed', [])
    steps_html = ''.join(f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #f3f4f6;">▸ {s}</div>' for s in steps)
    res_html   = ' '.join(f'<span style="background:#dcfce7;color:#166534;padding:2px 8px;border-radius:99px;font-size:11px;">{r}</span>' for r in resources)
    return f'''
<div style="background:#fef3c7;border-radius:8px;padding:10px;margin-bottom:10px;font-size:13px;line-height:1.5;">
  📢 {alert_msg}
</div>
<div style="background:#1e293b;color:#86efac;border-radius:8px;padding:8px 12px;font-family:monospace;font-size:12px;margin-bottom:10px;">
  📱 SMS: {sms}
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
    ap_badge = '<span style="background:#22c55e;color:white;padding:3px 12px;border-radius:99px;font-weight:700;">✅ APPROVED</span>' if approved else '<span style="background:#ef4444;color:white;padding:3px 12px;border-radius:99px;font-weight:700;">❌ REJECTED</span>'
    issues_html = ''.join(f'<div style="color:#ef4444;font-size:12px;padding:2px 0;">⚠️ {i}</div>' for i in issues) or '<div style="color:#9ca3af;font-size:12px;">None</div>'
    flags_html  = ''.join(f'<div style="color:#f97316;font-size:12px;padding:2px 0;">🚩 {f}</div>' for f in flags) or '<div style="color:#9ca3af;font-size:12px;">None</div>'
    sdg_html    = ' '.join(f'<span style="background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:99px;font-size:11px;">🌍 {s}</span>' for s in sdgs)
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


def parse_and_render(agent_key, text):
    """Extract JSON from agent output and render as card. Falls back to markdown."""
    try:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            data = json.loads(match.group())
            if agent_key == 'monitor':   return render_monitor(data)
            if agent_key == 'predict':   return render_predict(data)
            if agent_key == 'decision':  return render_decision(data)
            if agent_key == 'action':    return render_action(data)
            if agent_key == 'governance':return render_governance(data)
    except Exception:
        pass
    # Fallback: basic markdown → html
    lines = text.split('\n')
    html = []
    for line in lines:
        line = line.strip()
        if not line: continue
        line = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', line)
        if line.startswith('- ') or line.startswith('* '):
            html.append(f'<div style="font-size:13px;padding:2px 0;">▸ {line[2:]}</div>')
        else:
            html.append(f'<div style="font-size:13px;padding:2px 0;">{line}</div>')
    return ''.join(html)


class ChatConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        await self.accept()
        await self.send(text_data=json.dumps({
            'message': '<strong>✅ ResilientEco Guardian</strong> — 5-agent pipeline ready · Azure AI Foundry · Azure MCP',
            'type': 'system'
        }))

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        data    = json.loads(text_data)
        message = data.get('message', '').lower()

        await self.send(text_data=json.dumps({
            'message': '🔄 Initializing 5-agent pipeline via <strong>Azure AI Foundry</strong>...',
            'type': 'thinking'
        }))

        # Detect city
        detected = ('nairobi', (-1.2921, 36.8219))
        for city, coords in KENYA_CITIES.items():
            if city in message:
                detected = (city, coords)
                break
        city_name, (lat, lon) = detected

        # Run pipeline
        try:
            from .agents.core_agents import run_all_agents
            results = await sync_to_async(run_all_agents)(message, lat, lon, city_name.title())
        except Exception as e:
            logger.exception("Agent pipeline failed")
            await self.send(text_data=json.dumps({
                'message': f'⚠️ Pipeline error: {str(e)}',
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
                    f'🤖 {step.get("model","")} &nbsp;·&nbsp; {step.get("source","")} &nbsp;·&nbsp; {step.get("latency_ms","")}ms'
                    f'</div>'
                )

            body = parse_and_render(agent_key, output) if isinstance(output, str) else str(output)

            await self.send(text_data=json.dumps({
                'message': meta + body,
                'type': agent_key,
                'label': f'{style["icon"]} {style["label"]}',
                'color': style['color'],
            }))

        # MCP actions panel
        mcp_output = results.get('mcp_actions')
        if mcp_output:
            lines_html = []
            for line in mcp_output.strip().split('\n'):
                is_live = '[LIVE]' in line
                badge   = '<span style="background:#22c55e;color:white;padding:1px 6px;border-radius:4px;font-size:10px;font-family:monospace;">LIVE</span>' if is_live \
                     else '<span style="background:#94a3b8;color:white;padding:1px 6px;border-radius:4px;font-size:10px;font-family:monospace;">SIM</span>'
                clean = line.replace('[LIVE]','').replace('[SIM]','').strip()
                lines_html.append(f'<div style="font-size:12px;padding:3px 0;">{badge} {clean}</div>')
            await self.send(text_data=json.dumps({
                'message': '<strong>☁️ Azure MCP Actions</strong><br>' + ''.join(lines_html),
                'type': 'mcp_actions',
                'label': '☁️ Azure MCP Actions',
                'color': '#0078d4',
            }))

        # Summary
        if chain:
            total_ms    = sum(s.get('latency_ms', 0) for s in chain)
            models_used = list(dict.fromkeys(s.get('model','') for s in chain if s.get('model')))
            sources     = list(dict.fromkeys(s.get('source','') for s in chain if s.get('source')))
            await self.send(text_data=json.dumps({
                'message': (
                    f'✅ <strong>Pipeline Complete — {city_name.title()}</strong><br>'
                    f'<span style="font-size:12px;color:#6b7280;">'
                    f'Session {session_id} &nbsp;·&nbsp; {len(chain)} agents &nbsp;·&nbsp; {total_ms}ms total &nbsp;·&nbsp; '
                    f'{", ".join(models_used)} via {", ".join(sources)}'
                    f'</span>'
                ),
                'type': 'complete',
            }))