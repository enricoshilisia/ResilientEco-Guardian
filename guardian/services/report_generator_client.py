"""
guardian/services/report_generator_client.py
---------------------------------------------
Calls the ReportGenerator Azure Function and returns
structured data ready for chat rendering and PDF download.

Drop this file into guardian/services/
Then wire it up via guardian/views.py and guardian/consumers.py
"""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

REPORT_FUNCTION_URL = os.environ.get(
    "REPORT_FUNCTION_URL",
    "https://resilienteco-functions-gcfvhvdec3daa4dn.eastus-01.azurewebsites.net/api/ReportGenerator",
)
REPORT_FUNCTION_KEY = os.environ.get("REPORT_FUNCTION_KEY", "")


# ═══════════════════════════════════════════════════════════════
# MAIN CLIENT CALL
# ═══════════════════════════════════════════════════════════════

async def call_report_generator(
    locations: list,
    org_name: str,
    org_type: str = "agriculture",
    report_type: str = "agricultural",
    fmt: str = "both",
) -> dict:
    """
    Call the Azure Function ReportGenerator.
    Returns dict with keys: success, report, pdf_base64, pdf_filename, error
    """
    payload = {
        "locations":   locations,
        "org_name":    org_name,
        "org_type":    org_type,
        "report_type": report_type,
        "format":      fmt,
    }
    headers = {"Content-Type": "application/json"}
    if REPORT_FUNCTION_KEY:
        headers["x-functions-key"] = REPORT_FUNCTION_KEY

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                REPORT_FUNCTION_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    err = await resp.text()
                    logger.error(f"ReportGenerator function error {resp.status}: {err}")
                    return {"success": False, "error": f"Function returned {resp.status}"}
    except Exception as e:
        logger.error(f"ReportGenerator call failed: {e}")
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# REPORT DETECTION
# ═══════════════════════════════════════════════════════════════

REPORT_KEYWORDS = [
    "full report", "climate risk report", "generate report", "detailed report",
    "comprehensive report", "give me a report", "risk report", "full analysis",
    "comprehensive analysis", "full climate", "all zones", "all my zones",
    "all monitored", "planting report", "irrigation report", "forecast report",
    "48-hour", "48 hour", "hourly forecast", "hourly breakdown",
    "full meteorological", "national risk assessment", "full risk assessment",
]

def is_report_request(query: str) -> bool:
    """Detect if the user is asking for a detailed report rather than a quick answer."""
    q = query.lower()
    return any(kw in q for kw in REPORT_KEYWORDS)


def detect_report_type(query: str, org_type: str) -> str:
    q = query.lower()
    if org_type == "meteorological":
        return "meteorological"
    if any(w in q for w in ["crop", "plant", "irrigat", "harvest", "maize", "bean", "farm", "agricultural"]):
        return "agricultural"
    if any(w in q for w in ["flood", "drought", "heatwave", "weather", "forecast", "precipitation", "rainfall"]):
        return "full"
    return "agricultural" if org_type == "agriculture" else "meteorological"


# ═══════════════════════════════════════════════════════════════
# CHAT RENDERING
# ═══════════════════════════════════════════════════════════════

def render_report_as_chat_html(result: dict, pdf_filename: str = None) -> str:
    """
    Render the report JSON as rich HTML for display in the chat interface.
    This is what gets sent back through the WebSocket as the assistant message.
    """
    if not result.get("success"):
        return f'<div style="color:#dc2626;padding:12px;">⚠️ Report generation failed: {result.get("error","Unknown error")}</div>'

    report    = result.get("report", {})
    org_name  = result.get("org_name", "")
    gen_at    = result.get("generated_at", "")
    pdf_b64   = result.get("pdf_base64")
    pdf_fname = result.get("pdf_filename", pdf_filename or "climate_report.pdf")

    overall   = report.get("overall_alert_level", "GREEN")
    score     = report.get("overall_risk_score",  0)
    conf      = report.get("confidence",          0)
    summary   = report.get("executive_summary",   "")

    alert_colors = {
        "RED":    ("#dc2626", "#fff1f2", "#fecaca"),
        "ORANGE": ("#ea580c", "#fff7ed", "#fed7aa"),
        "YELLOW": ("#d97706", "#fffbeb", "#fde68a"),
        "GREEN":  ("#16a34a", "#f0fdf4", "#bbf7d0"),
    }
    ac, abg, aborder = alert_colors.get(overall, alert_colors["GREEN"])

    # PDF download button
    pdf_button = ""
    if pdf_b64:
        pdf_button = f"""
        <button onclick="downloadReportPDF('{pdf_fname}', this)"
                style="display:inline-flex;align-items:center;gap:6px;
                       padding:8px 16px;background:#1e293b;color:white;
                       border:none;border-radius:8px;font-size:12px;
                       font-weight:600;cursor:pointer;margin-top:8px;"
                data-pdf="{pdf_b64}">
            📄 Download Full PDF Report
        </button>"""

    # Zone cards
    zone_cards = ""
    for z in report.get("zones", []):
        zlvl    = z.get("alert_level", "GREEN")
        zc, zbg, zborder = alert_colors.get(zlvl, alert_colors["GREEN"])
        cur     = z.get("current_conditions", {})
        actions = z.get("immediate_actions", [])[:4]
        hourly  = z.get("hourly_forecast", [])[:6]

        actions_html = "".join(
            f'<div style="display:flex;gap:6px;margin-bottom:3px;font-size:11px;color:#334155;">'
            f'<span style="color:{zc};font-weight:700;">→</span>{a}</div>'
            for a in actions
        ) if actions else ""

        hourly_html = ""
        if hourly:
            hourly_html = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;">'
            for h in hourly:
                prob = h.get("precip_prob", 0)
                pcol = "#2563eb" if prob > 60 else "#64748b"
                hourly_html += (
                    f'<div style="flex:1;min-width:55px;background:#f8fafc;border:1px solid #e2e8f0;'
                    f'border-radius:6px;padding:5px 4px;text-align:center;">'
                    f'<div style="font-size:9px;color:#94a3b8;">{h.get("time","")}</div>'
                    f'<div style="font-size:12px;font-weight:700;color:#0f172a;">{h.get("temp","")}°</div>'
                    f'<div style="font-size:10px;color:{pcol};">{h.get("precip_mm","")}mm</div>'
                    f'<div style="font-size:9px;color:#94a3b8;">{prob}%</div>'
                    f'</div>'
                )
            hourly_html += "</div>"

        def risk_badge(val, label):
            if val >= 70:   c, bg = "#dc2626", "#fff1f2"
            elif val >= 50: c, bg = "#ea580c", "#fff7ed"
            elif val >= 30: c, bg = "#d97706", "#fffbeb"
            else:           c, bg = "#16a34a", "#f0fdf4"
            return (
                f'<div style="flex:1;text-align:center;padding:8px 4px;'
                f'background:{bg};border:1px solid {c}30;border-radius:6px;">'
                f'<div style="font-size:9px;font-weight:700;text-transform:uppercase;'
                f'color:#64748b;letter-spacing:.04em;margin-bottom:2px;">{label}</div>'
                f'<div style="font-size:18px;font-weight:700;color:{c};">{val}%</div>'
                f'</div>'
            )

        zone_cards += f"""
        <div style="border:1.5px solid {zborder};border-radius:10px;overflow:hidden;margin-bottom:10px;">
            <div style="background:{zc};padding:8px 12px;display:flex;justify-content:space-between;align-items:center;">
                <span style="color:white;font-weight:700;font-size:13px;">📍 {z.get('name','')}</span>
                <span style="color:white;font-size:11px;font-weight:600;background:rgba(0,0,0,.2);
                       padding:2px 8px;border-radius:99px;">{zlvl} · {z.get('confidence',0)}% conf</span>
            </div>
            <div style="padding:10px;background:{zbg};">
                <div style="display:flex;gap:6px;margin-bottom:8px;">
                    {risk_badge(z.get('flood_risk',0),'Flood')}
                    {risk_badge(z.get('drought_risk',0),'Drought')}
                    {risk_badge(z.get('heatwave_risk',0),'Heat')}
                </div>
                <div style="font-size:11px;color:#475569;margin-bottom:6px;">
                    🌡️ {cur.get('temperature','')}°C · 💧 {cur.get('humidity','')}% · 
                    🌧️ {cur.get('rain_24h','')}mm 24h · {cur.get('conditions','')}
                </div>
                {'<div style="font-size:11px;color:#334155;background:white;border:1px solid #e2e8f0;border-radius:6px;padding:8px;margin-bottom:6px;line-height:1.5;">' + z.get('forecast_48h','') + '</div>' if z.get('forecast_48h') else ''}
                {hourly_html}
                {('<div style="margin-top:6px;"><div style="font-size:10px;font-weight:700;text-transform:uppercase;color:#64748b;margin-bottom:3px;">Immediate Actions</div>' + actions_html + '</div>') if actions_html else ''}
            </div>
        </div>"""

    # Crop risk table
    crop_html = ""
    crops = report.get("crop_risk_matrix", [])
    if crops:
        rows = ""
        for i, row in enumerate(crops):
            bg = "#f8fafc" if i % 2 == 0 else "white"
            def chip(v):
                if v >= 70: c = "#dc2626"
                elif v >= 50: c = "#ea580c"
                elif v >= 30: c = "#d97706"
                else: c = "#16a34a"
                return f'<span style="font-weight:700;color:{c};">{v}%</span>'
            rows += (
                f'<tr style="background:{bg};">'
                f'<td style="padding:5px 8px;font-weight:600;font-size:11px;">{row.get("crop","")}</td>'
                f'<td style="padding:5px 8px;font-size:11px;color:#64748b;">{row.get("zone","")}</td>'
                f'<td style="padding:5px 8px;text-align:center;">{chip(row.get("flood_risk",0))}</td>'
                f'<td style="padding:5px 8px;text-align:center;">{chip(row.get("drought_risk",0))}</td>'
                f'<td style="padding:5px 8px;text-align:center;">{chip(row.get("heat_risk",0))}</td>'
                f'<td style="padding:5px 8px;font-size:10px;color:#475569;">{row.get("action","")}</td>'
                f'</tr>'
            )
        crop_html = f"""
        <div style="margin-top:12px;">
            <div style="font-size:12px;font-weight:700;color:#0f172a;margin-bottom:6px;">🌾 Crop Risk Matrix</div>
            <div style="overflow-x:auto;">
                <table style="width:100%;border-collapse:collapse;font-family:inherit;">
                    <thead>
                        <tr style="background:#f1f5f9;">
                            <th style="padding:6px 8px;font-size:10px;font-weight:700;text-transform:uppercase;
                                       letter-spacing:.05em;color:#64748b;text-align:left;">Crop</th>
                            <th style="padding:6px 8px;font-size:10px;font-weight:700;text-transform:uppercase;
                                       letter-spacing:.05em;color:#64748b;text-align:left;">Zone</th>
                            <th style="padding:6px 8px;font-size:10px;font-weight:700;text-transform:uppercase;
                                       letter-spacing:.05em;color:#64748b;text-align:center;">Flood</th>
                            <th style="padding:6px 8px;font-size:10px;font-weight:700;text-transform:uppercase;
                                       letter-spacing:.05em;color:#64748b;text-align:center;">Drought</th>
                            <th style="padding:6px 8px;font-size:10px;font-weight:700;text-transform:uppercase;
                                       letter-spacing:.05em;color:#64748b;text-align:center;">Heat</th>
                            <th style="padding:6px 8px;font-size:10px;font-weight:700;text-transform:uppercase;
                                       letter-spacing:.05em;color:#64748b;text-align:left;">Action</th>
                        </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>
        </div>"""

    # Action items
    a24  = report.get("action_items_24h",  [])
    a7   = report.get("action_items_7days", [])
    actions_html = ""
    if a24 or a7:
        a24_html = "".join(f'<div style="margin-bottom:3px;font-size:11px;color:#334155;">⚡ {a}</div>' for a in a24)
        a7_html  = "".join(f'<div style="margin-bottom:3px;font-size:11px;color:#334155;">📅 {a}</div>' for a in a7)
        actions_html = f"""
        <div style="margin-top:12px;display:grid;grid-template-columns:1fr 1fr;gap:10px;">
            <div style="background:#fff1f2;border:1px solid #fecaca;border-radius:8px;padding:10px;">
                <div style="font-size:11px;font-weight:700;color:#dc2626;margin-bottom:6px;">⚡ Next 24 Hours</div>
                {a24_html}
            </div>
            <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px;">
                <div style="font-size:11px;font-weight:700;color:#2563eb;margin-bottom:6px;">📅 Next 7 Days</div>
                {a7_html}
            </div>
        </div>"""

    # Planting window
    pw = report.get("planting_window", {})
    planting_html = ""
    if pw.get("current_season"):
        rec  = ", ".join(pw.get("recommended_crops", [])[:4])
        dly  = ", ".join(pw.get("crops_to_delay",    [])[:3])
        avoid= ", ".join(pw.get("crops_to_avoid",    [])[:3])
        planting_html = f"""
        <div style="margin-top:10px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px;">
            <div style="font-size:11px;font-weight:700;color:#15803d;margin-bottom:6px;">🌱 Planting Window — {pw['current_season']}</div>
            {f'<div style="font-size:11px;color:#166534;">✅ <b>Plant now:</b> {rec}</div>' if rec else ''}
            {f'<div style="font-size:11px;color:#92400e;">⏳ <b>Delay:</b> {dly}</div>' if dly else ''}
            {f'<div style="font-size:11px;color:#dc2626;">❌ <b>Avoid:</b> {avoid}</div>' if avoid else ''}
        </div>"""

    # Irrigation
    irr = report.get("irrigation_recommendations", [])[:4]
    irr_html = ""
    if irr:
        irr_html = (
            '<div style="margin-top:10px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px;">'
            '<div style="font-size:11px;font-weight:700;color:#1d4ed8;margin-bottom:4px;">💧 Irrigation Recommendations</div>'
            + "".join(f'<div style="font-size:11px;color:#334155;margin-bottom:2px;">• {r}</div>' for r in irr)
            + '</div>'
        )

    outlook = report.get("outlook_7day", "")
    outlook_html = (
        f'<div style="margin-top:10px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px;">'
        f'<div style="font-size:11px;font-weight:700;color:#334155;margin-bottom:4px;">📈 7-Day Outlook</div>'
        f'<div style="font-size:11px;color:#475569;line-height:1.5;">{outlook}</div>'
        f'</div>'
    ) if outlook else ""

    try:
        ts = datetime.fromisoformat(gen_at.replace("Z","")).strftime("%d %b %Y %H:%M UTC")
    except Exception:
        ts = gen_at

    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:100%;">

        <!-- Header -->
        <div style="background:linear-gradient(135deg,{ac},#1e293b);border-radius:12px;
                    padding:16px 18px;margin-bottom:12px;">
            <div style="color:white;font-size:15px;font-weight:700;margin-bottom:2px;">
                📊 Climate Risk Report — {org_name}
            </div>
            <div style="color:rgba(255,255,255,.6);font-size:11px;">Generated {ts}</div>
        </div>

        <!-- Overall status -->
        <div style="background:{abg};border:1.5px solid {aborder};border-radius:10px;
                    padding:12px 14px;margin-bottom:12px;
                    display:flex;align-items:center;gap:14px;">
            <div style="text-align:center;min-width:60px;">
                <div style="font-size:10px;font-weight:700;text-transform:uppercase;
                            letter-spacing:.05em;color:{ac};">Alert</div>
                <div style="font-size:22px;font-weight:700;color:{ac};">{overall}</div>
            </div>
            <div style="flex:1;">
                <div style="font-size:13px;font-weight:600;color:#0f172a;margin-bottom:4px;">{summary}</div>
                <div style="font-size:11px;color:#64748b;">
                    Risk score: <b style="color:{ac};">{score}%</b> · 
                    Confidence: <b>{conf}%</b> · 
                    Zones: <b>{len(report.get('zones',[]))}</b>
                </div>
            </div>
        </div>

        <!-- Zone cards -->
        <div style="margin-bottom:12px;">
            <div style="font-size:12px;font-weight:700;color:#0f172a;margin-bottom:6px;">Zone Analysis</div>
            {zone_cards}
        </div>

        {crop_html}
        {planting_html}
        {irr_html}
        {actions_html}
        {outlook_html}

        <!-- PDF download -->
        {pdf_button}

        <div style="margin-top:8px;font-size:10px;color:#94a3b8;">
            Powered by Azure AI Foundry · ResilientEco Guardian
        </div>
    </div>
    """

    return html


def render_report_for_met_chat(result: dict) -> str:
    """Same renderer but with meteorological styling."""
    # Met uses blue primary, ag uses green — swap the gradient
    html = render_report_as_chat_html(result)
    html = html.replace("135deg,#16a34a", "135deg,#1e3a8a")
    return html